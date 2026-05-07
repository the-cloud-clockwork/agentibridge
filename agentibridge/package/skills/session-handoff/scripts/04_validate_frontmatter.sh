#!/usr/bin/env bash
set -euo pipefail

# Purpose:  Validate that a handoff memory file begins with the required YAML
#           frontmatter (name, description, type). Fail loudly if not.
# Inputs:   $1 — absolute path to the handoff file
# Outputs:  stdout — "ok" on success
#           exit 0 — valid; exit 1 — missing file / malformed / missing fields
# Idempotent: Yes — read-only.
# Dependencies: awk, grep (coreutils).

if [ $# -lt 1 ]; then
  echo "ERROR: handoff file path required" >&2
  exit 1
fi

f="$1"

if [ ! -f "$f" ]; then
  echo "ERROR: file does not exist: $f" >&2
  exit 1
fi

first_line="$(head -1 "$f")"
if [ "$first_line" != "---" ]; then
  echo "ERROR: $f does not begin with YAML frontmatter delimiter '---'" >&2
  exit 1
fi

# Extract the frontmatter block (between first two --- lines)
frontmatter="$(awk '
  BEGIN { inside = 0; count = 0 }
  /^---[[:space:]]*$/ {
    count++
    if (count == 1) { inside = 1; next }
    if (count == 2) { inside = 0; exit }
  }
  inside { print }
' "$f")"

if [ -z "$frontmatter" ]; then
  echo "ERROR: $f has no closing '---' frontmatter delimiter" >&2
  exit 1
fi

missing=""
for field in name description type; do
  if ! echo "$frontmatter" | grep -qE "^${field}:"; then
    missing="${missing} ${field}"
  fi
done

if [ -n "$missing" ]; then
  echo "ERROR: $f frontmatter is missing required field(s):${missing}" >&2
  exit 1
fi

# Also require that `type:` is one of the known auto-memory types
type_val="$(echo "$frontmatter" | awk -F: '/^type:/ { gsub(/^[ \t]+|[ \t]+$/, "", $2); print $2; exit }')"
case "$type_val" in
  user|feedback|project|reference) ;;
  *)
    echo "ERROR: $f frontmatter 'type:' is '$type_val', must be one of user|feedback|project|reference" >&2
    exit 1
    ;;
esac

echo "ok"
