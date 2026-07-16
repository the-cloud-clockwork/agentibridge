#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# cloudfared.sh — Idempotent Cloudflare Tunnel setup for AgentiBridge
#
# Sets up a named Cloudflare Tunnel pointing at localhost:$PORT
# so remote MCP clients can reach the agentibridge over HTTPS.
#
# Safe to re-run: every step checks before acting.
#
# Usage:
#   chmod +x automation/cloudfared.sh
#   ./automation/cloudfared.sh
# ─────────────────────────────────────────────────────────────
set -euo pipefail

# ── Colors ───────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

ok()   { printf "${GREEN}✔${NC} %s\n" "$*"; }
info() { printf "${YELLOW}▶${NC} %s\n" "$*"; }
err()  { printf "${RED}✖${NC} %s\n" "$*" >&2; }
hdr()  { printf "\n${CYAN}${BOLD}── %s ──${NC}\n" "$*"; }

# ── Configuration ────────────────────────────────────────────
PORT="${AGENTIBRIDGE_PORT:-8100}"
CONFIG_DIR="${HOME}/.cloudflared"
CONFIG_FILE="${CONFIG_DIR}/config.yml"

# ═════════════════════════════════════════════════════════════
# 1. Install cloudflared
# ═════════════════════════════════════════════════════════════
hdr "1/10  cloudflared binary"

if command -v cloudflared &>/dev/null; then
    ok "cloudflared already installed ($(cloudflared --version 2>&1 | head -1))"
else
    info "Installing cloudflared…"

    OS="$(uname -s)"
    ARCH="$(uname -m)"

    case "$OS" in
        Linux)
            case "$ARCH" in
                x86_64)  BIN_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64" ;;
                aarch64) BIN_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64" ;;
                armv7l)  BIN_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm"   ;;
                *)       err "Unsupported Linux arch: $ARCH"; exit 1 ;;
            esac
            INSTALL_PATH="/usr/local/bin/cloudflared"
            info "Downloading cloudflared for Linux/$ARCH…"
            sudo curl -fsSL "$BIN_URL" -o "$INSTALL_PATH"
            sudo chmod +x "$INSTALL_PATH"
            ;;
        Darwin)
            if command -v brew &>/dev/null; then
                info "Installing via Homebrew…"
                brew install cloudflared
            else
                err "macOS detected but Homebrew not found. Install Homebrew first or install cloudflared manually."
                exit 1
            fi
            ;;
        *)
            err "Unsupported OS: $OS. Install cloudflared manually: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
            exit 1
            ;;
    esac

    if command -v cloudflared &>/dev/null; then
        ok "cloudflared installed ($(cloudflared --version 2>&1 | head -1))"
    else
        err "cloudflared installation failed"
        exit 1
    fi
fi

# ═════════════════════════════════════════════════════════════
# 2. Authenticate with Cloudflare
# ═════════════════════════════════════════════════════════════
hdr "2/10  Cloudflare authentication"

if cloudflared tunnel list &>/dev/null 2>&1; then
    ok "Already authenticated with Cloudflare"
else
    info "Opening browser for Cloudflare authentication…"
    cloudflared tunnel login
    ok "Authentication complete"
fi

# ═════════════════════════════════════════════════════════════
# 3. Prompt for tunnel name
# ═════════════════════════════════════════════════════════════
hdr "3/10  Tunnel name"

read -rp "Tunnel name [agentibridge]: " TUNNEL_NAME
TUNNEL_NAME="${TUNNEL_NAME:-agentibridge}"
ok "Using tunnel name: ${TUNNEL_NAME}"

# ═════════════════════════════════════════════════════════════
# 4. Create tunnel (idempotent)
# ═════════════════════════════════════════════════════════════
hdr "4/10  Create tunnel"

_tunnel_id_by_name() {
    # Extract tunnel ID by name from cloudflared JSON output.
    # Try strict filter first (exclude deleted), fall back to name-only match.
    local name="$1"
    local id

    id=$(cloudflared tunnel list -o json 2>/dev/null \
        | jq -r --arg n "$name" \
            '[.[] | select(.name==$n)] | [.[] | select(.deleted_at == null or .deleted_at == "")] | .[0].id // empty' \
        2>/dev/null)

    # Fallback: skip deleted_at filter entirely (some versions omit it)
    if [[ -z "$id" || "$id" == "null" ]]; then
        id=$(cloudflared tunnel list -o json 2>/dev/null \
            | jq -r --arg n "$name" '[.[] | select(.name==$n)] | .[0].id // empty' \
            2>/dev/null)
    fi

    [[ "$id" != "null" ]] && echo "$id"
}

TUNNEL_ID=$(_tunnel_id_by_name "$TUNNEL_NAME")

if [[ -n "$TUNNEL_ID" ]]; then
    ok "Tunnel '${TUNNEL_NAME}' already exists (ID: ${TUNNEL_ID})"
else
    info "Creating tunnel '${TUNNEL_NAME}'…"
    CREATE_OUTPUT=$(cloudflared tunnel create "$TUNNEL_NAME" 2>&1)
    echo "$CREATE_OUTPUT"

    # Try to read from tunnel list first
    TUNNEL_ID=$(_tunnel_id_by_name "$TUNNEL_NAME")

    # Fallback: parse "Created tunnel <name> with id <uuid>" from create output.
    # sed -E (portable, works with BSD grep/sed on macOS) instead of GNU-only grep -oP.
    if [[ -z "$TUNNEL_ID" ]]; then
        TUNNEL_ID=$(echo "$CREATE_OUTPUT" | sed -nE 's/.*with id ([0-9a-f-]+).*/\1/p' | head -1)
    fi

    if [[ -z "$TUNNEL_ID" ]]; then
        err "Failed to read tunnel ID after creation"
        err "Debug: run 'cloudflared tunnel list -o json | jq .' to inspect"
        exit 1
    fi
    ok "Tunnel created (ID: ${TUNNEL_ID})"
fi

# ═════════════════════════════════════════════════════════════
# 5. Prompt for subdomain
# ═════════════════════════════════════════════════════════════
hdr "5/10  Subdomain"

read -rp "Subdomain (required, e.g. mcp): " SUBDOMAIN
if [[ -z "$SUBDOMAIN" ]]; then
    err "Subdomain is required"
    exit 1
fi

# ═════════════════════════════════════════════════════════════
# 6. Prompt for domain
# ═════════════════════════════════════════════════════════════
hdr "6/10  Domain"

read -rp "Domain (required, e.g. nestorcolt.com): " DOMAIN
if [[ -z "$DOMAIN" ]]; then
    err "Domain is required"
    exit 1
fi

HOSTNAME="${SUBDOMAIN}.${DOMAIN}"
ok "Full hostname: ${HOSTNAME}"

# ═════════════════════════════════════════════════════════════
# 7. DNS route (idempotent — cloudflared warns if exists)
# ═════════════════════════════════════════════════════════════
hdr "7/10  DNS route"

info "Routing ${HOSTNAME} → tunnel '${TUNNEL_NAME}'…"
if cloudflared tunnel route dns "$TUNNEL_NAME" "$HOSTNAME" 2>&1; then
    ok "DNS route for ${HOSTNAME} is set"
else
    # cloudflared exits 0 even when CNAME exists, but just in case
    ok "DNS route for ${HOSTNAME} already exists (or was just created)"
fi

# ═════════════════════════════════════════════════════════════
# 8. Write config.yml
# ═════════════════════════════════════════════════════════════
hdr "8/10  Config file"

CREDS_FILE="${CONFIG_DIR}/${TUNNEL_ID}.json"

DESIRED_CONFIG="tunnel: ${TUNNEL_ID}
credentials-file: ${CREDS_FILE}

ingress:
  - hostname: ${HOSTNAME}
    service: http://localhost:${PORT}
  - service: http_status:404
"

mkdir -p "$CONFIG_DIR"

if [[ -f "$CONFIG_FILE" ]]; then
    EXISTING_CONFIG=$(<"$CONFIG_FILE")
    if [[ "$EXISTING_CONFIG" == "$DESIRED_CONFIG" ]]; then
        ok "Config file already up to date: ${CONFIG_FILE}"
    else
        BACKUP="${CONFIG_FILE}.bak.$(date +%Y%m%d%H%M%S)"
        cp "$CONFIG_FILE" "$BACKUP"
        info "Existing config backed up to ${BACKUP}"
        printf '%s' "$DESIRED_CONFIG" > "$CONFIG_FILE"
        ok "Config file updated: ${CONFIG_FILE}"
    fi
else
    printf '%s' "$DESIRED_CONFIG" > "$CONFIG_FILE"
    ok "Config file written: ${CONFIG_FILE}"
fi

printf "${CYAN}%s${NC}\n" "--- ${CONFIG_FILE} ---"
cat "$CONFIG_FILE"
printf "${CYAN}%s${NC}\n" "---"

# ═════════════════════════════════════════════════════════════
# 9. Optional systemd service
# ═════════════════════════════════════════════════════════════
hdr "9/10  Systemd service (optional)"

if [[ "$(uname -s)" != "Linux" ]]; then
    info "Skipping systemd (not Linux)"
else
    SYSTEMD_ACTIVE=false
    if systemctl is-enabled cloudflared &>/dev/null 2>&1; then
        SYSTEMD_ACTIVE=true
    fi

    # Resolve full path — sudo uses a restricted PATH that may not include cloudflared
    CLOUDFLARED_BIN=$(command -v cloudflared)

    if $SYSTEMD_ACTIVE; then
        ok "cloudflared systemd service already enabled"
        info "Restarting to pick up any config changes…"
        sudo systemctl restart cloudflared
        ok "Service restarted"
    else
        read -rp "Install as systemd service? [y/N]: " INSTALL_SERVICE
        INSTALL_SERVICE="${INSTALL_SERVICE:-N}"
        if [[ "$INSTALL_SERVICE" =~ ^[Yy]$ ]]; then
            info "Installing systemd service…"
            # sudo runs as root, so ~ resolves to /root — pass explicit config path
            sudo "$CLOUDFLARED_BIN" --config "$CONFIG_FILE" service install
            sudo systemctl enable --now cloudflared
            ok "Systemd service installed and started"
        else
            info "Skipping systemd install"
            info "You can run the tunnel manually with:"
            printf "  ${BOLD}cloudflared tunnel run %s${NC}\n" "$TUNNEL_NAME"
        fi
    fi
fi

# ═════════════════════════════════════════════════════════════
# 10. Validate
# ═════════════════════════════════════════════════════════════
hdr "10/10  Validation"

info "Checking if tunnel is reachable at https://${HOSTNAME}/health …"
info "(Make sure agentibridge is running on localhost:${PORT})"

# Give the tunnel a moment to come up if systemd just started it
sleep 2

if curl -sf --max-time 10 "https://${HOSTNAME}/health" >/dev/null 2>&1; then
    HEALTH_RESPONSE=$(curl -sf --max-time 10 "https://${HOSTNAME}/health")
    ok "Health check passed: ${HEALTH_RESPONSE}"
else
    info "Health check did not succeed (this is expected if agentibridge isn't running yet)"
    info "Start the bridge first, then verify with:"
    printf "  ${BOLD}curl https://%s/health${NC}\n" "$HOSTNAME"
fi

# ── Summary ──────────────────────────────────────────────────
hdr "Setup complete"
printf "  Tunnel name:  ${BOLD}%s${NC}\n" "$TUNNEL_NAME"
printf "  Tunnel ID:    ${BOLD}%s${NC}\n" "$TUNNEL_ID"
printf "  Hostname:     ${BOLD}https://%s${NC}\n" "$HOSTNAME"
printf "  Local target: ${BOLD}http://localhost:%s${NC}\n" "$PORT"
printf "  Config file:  ${BOLD}%s${NC}\n" "$CONFIG_FILE"
printf "\n"
printf "  ${CYAN}Next steps:${NC}\n"
printf "  1. docker compose up --build -d     # start agentibridge\n"
printf "  2. curl https://%s/health   # verify\n" "$HOSTNAME"
printf "\n"
