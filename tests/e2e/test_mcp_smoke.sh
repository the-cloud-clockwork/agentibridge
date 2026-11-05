#!/usr/bin/env bash
# E2E MCP Smoke Test — verifies all AgentiBridge MCP tools via claude CLI
#
# Usage:
#   ./tests/e2e/test_mcp_smoke.sh
#
# Prerequisites:
#   - claude CLI installed and on PATH
#   - .mcp.json in project root with agentibridge config
#   - agentibridge reachable (Docker Compose or local)
#
# Each test calls `claude -p` targeting one MCP tool and checks the result.
# Tests are retried once on error_during_execution (transient LLM proxy failures).

set -euo pipefail
cd "$(dirname "$0")/../.."

PASS=0
FAIL=0
SKIP=0
TOTAL=13
MAX_RETRIES=${SMOKE_TEST_RETRIES:-2}

CLAUDE_CMD="claude -p --dangerously-skip-permissions --output-format json --max-turns 3"
[[ -n "${CLAUDE_MODEL:-}" ]] && CLAUDE_CMD="${CLAUDE_CMD} --model ${CLAUDE_MODEL}"

# ── Prereq checks ──────────────────────────────────────────────────────────

if ! command -v claude &>/dev/null; then
  echo "ABORT: claude CLI not found on PATH"
  exit 1
fi

if ! command -v jq &>/dev/null; then
  echo "ABORT: jq not found on PATH"
  exit 1
fi

if [[ ! -f .mcp.json ]]; then
  echo "ABORT: .mcp.json not found in project root"
  exit 1
fi

# Extract health URL from .mcp.json (replace /mcp with /health)
MCP_URL=$(jq -r '.mcpServers["agentibridge"].url' .mcp.json)
HEALTH_URL="${MCP_URL%/mcp}/health"
API_KEY=$(jq -r '.mcpServers["agentibridge"].headers["X-API-Key"] // empty' .mcp.json)

echo "Checking agentibridge health at ${HEALTH_URL} ..."
HEALTH_ARGS=(-sf --max-time 10)
[[ -n "$API_KEY" ]] && HEALTH_ARGS+=(-H "X-API-Key: ${API_KEY}")

if ! curl "${HEALTH_ARGS[@]}" "$HEALTH_URL" >/dev/null 2>&1; then
  echo "ABORT: agentibridge not reachable at ${HEALTH_URL}"
  exit 1
fi
echo "Health check OK"
echo ""

# ── Test runner ─────────────────────────────────────────────────────────────

LAST_ERROR=""

_invoke_claude() {
  local prompt="$1" check_fn="$2"
  local raw result stderr_file

  stderr_file=$(mktemp)
  raw=$($CLAUDE_CMD "$prompt" 2>"$stderr_file") || true
  local stderr_out
  stderr_out=$(cat "$stderr_file" 2>/dev/null)
  rm -f "$stderr_file"

  # claude --output-format json wraps output in {"type":"result","result":"..."}
  result=$(echo "$raw" | jq -r '.result // .error // empty' 2>/dev/null) || result=""
  [[ -z "$result" ]] && result="$raw"

  # Stash error info for diagnostics
  local subtype
  subtype=$(echo "$raw" | jq -r '.subtype // empty' 2>/dev/null) || subtype=""
  LAST_ERROR="subtype=${subtype:-none} result=${result:0:200}"
  [[ -n "$stderr_out" ]] && LAST_ERROR="${LAST_ERROR} stderr=${stderr_out:0:200}"

  # Check for transient errors that should be retried
  if [[ "$subtype" == "error_during_execution" ]]; then
    return 2  # signal: retryable error
  fi

  # LiteLLM key cache can take time to warm — treat auth errors as retryable
  if echo "$result" | grep -qiE "(401|Authentication Error|Failed to authenticate)"; then
    return 2  # signal: retryable error
  fi

  if $check_fn "$result" >/dev/null 2>&1; then
    return 0  # pass
  fi

  return 1
}

run_test() {
  local num="$1" name="$2" prompt="$3" check_fn="$4"
  local attempt rc

  for attempt in $(seq 1 "$MAX_RETRIES"); do
    rc=0
    _invoke_claude "$prompt" "$check_fn" || rc=$?

    if [[ $rc -eq 0 ]]; then
      [[ $attempt -gt 1 ]] && echo "[${num}/${TOTAL}] PASS  ${name}  (retry ${attempt}/${MAX_RETRIES})" \
                            || echo "[${num}/${TOTAL}] PASS  ${name}"
      PASS=$((PASS + 1))
      return
    fi

    if [[ $rc -eq 2 && $attempt -lt $MAX_RETRIES ]]; then
      echo "[${num}/${TOTAL}] RETRY ${name}  (attempt ${attempt}/${MAX_RETRIES} — transient error)"
      sleep 2
      continue
    fi

    # Final failure — show diagnostics
    echo "[${num}/${TOTAL}] FAIL  ${name}"
    echo "  ${LAST_ERROR}"
    FAIL=$((FAIL + 1))
    return
  done
}

# ── Check functions ─────────────────────────────────────────────────────────

check_list_sessions() {
  local result="$1"
  echo "$result" | grep -qi "session" || return 1
  echo "$result" | grep -qE "(num_user_turns|num_turns|entries)" || return 1
}

check_list_sessions_filtered() {
  local result="$1"
  echo "$result" | grep -qi "session" || return 1
}

check_get_session() {
  local result="$1"
  echo "$result" | grep -qiE "(meta|entries|entry_count|transcript)" || return 1
}

check_search_sessions() {
  local result="$1"
  echo "$result" | grep -qiE "(match|result|found|session)" || return 1
}

check_get_session_actions() {
  local result="$1"
  echo "$result" | grep -qiE "(tool|action|success|count)" || return 1
}

check_collect_now() {
  local result="$1"
  echo "$result" | grep -qiE "(scan|collect|file|process)" || return 1
}

check_list_memory_files() {
  local result="$1"
  echo "$result" | grep -qiE "(memory|file|project)" || return 1
}

check_restore_session() {
  local result="$1"
  echo "$result" | grep -qiE "(context|char_count|session)" || return 1
}

check_search_history() {
  local result="$1"
  echo "$result" | grep -qiE "(history|entry|display|result)" || return 1
}

check_get_session_segment() {
  local result="$1"
  echo "$result" | grep -qiE "(entries|segment|message|transcript)" || return 1
}

check_search_semantic() {
  local result="$1"
  echo "$result" | grep -qiE "(result|match|score|session|semantic|success)" || return 1
}

check_generate_summary() {
  local result="$1"
  echo "$result" | grep -qiE "(summary|session|transcript)" || return 1
}

check_get_memory_file() {
  local result="$1"
  echo "$result" | grep -qiE "(content|memory|file)" || return 1
}

# ── Test cases ──────────────────────────────────────────────────────────────

echo "Running ${TOTAL} MCP smoke tests (max_retries=${MAX_RETRIES})..."
echo ""

run_test 1 "list_sessions" \
  "Use the list_sessions MCP tool with limit=3. Show the raw result." \
  check_list_sessions

run_test 2 "list_sessions (filtered)" \
  "Use the list_sessions MCP tool with project='agentic' and limit=3. Show the raw result." \
  check_list_sessions_filtered

run_test 3 "get_session" \
  "First call list_sessions with limit=1 to get a session_id, then call get_session with that session_id and last_n=5. Show the get_session result." \
  check_get_session

run_test 4 "search_sessions" \
  "Use the search_sessions MCP tool with query='test' and limit=3. Show the raw result." \
  check_search_sessions

run_test 5 "get_session_actions" \
  "First call list_sessions with limit=1 to get a session_id, then call get_session_actions with that session_id. Show the result." \
  check_get_session_actions

run_test 6 "collect_now" \
  "Use the collect_now MCP tool to trigger immediate collection. Show the raw result." \
  check_collect_now

run_test 7 "list_memory_files" \
  "Use the list_memory_files MCP tool. Show the raw result." \
  check_list_memory_files

run_test 8 "restore_session" \
  "First call list_sessions with limit=1 to get a session_id, then call restore_session with that session_id and last_n=5. Show the result." \
  check_restore_session

run_test 9 "search_history" \
  "Use the search_history MCP tool with query='test' and limit=3. Show the raw result." \
  check_search_history

run_test 10 "get_session_segment" \
  "First call list_sessions with limit=1 to get a session_id, then call get_session_segment with that session_id and last_n=3. Show the result." \
  check_get_session_segment

run_test 11 "get_memory_file" \
  "First call list_memory_files to get a file path, then call get_memory_file with that project and file_path. Show the result." \
  check_get_memory_file

# ── Phase 2: Semantic search (requires embeddings) ────────────────────────

CHUNK_COUNT=$(docker exec agentibridge-postgres psql -U agentibridge -tAc \
  "SELECT COUNT(*) FROM transcript_chunks" 2>/dev/null || echo "0")
CHUNK_COUNT=$(echo "$CHUNK_COUNT" | tr -d '[:space:]')
EMBEDDING_AVAILABLE=false
[[ "$CHUNK_COUNT" -gt 0 ]] && EMBEDDING_AVAILABLE=true

if [[ "$EMBEDDING_AVAILABLE" == "true" ]]; then
  echo ""
  echo "Embeddings available (${CHUNK_COUNT} chunks) — running Phase 2 tests"

  run_test 12 "search_semantic" \
    "Use the search_semantic MCP tool with query='Docker' and limit=3. Show the raw result." \
    check_search_semantic

  run_test 13 "generate_summary" \
    "First call list_sessions with limit=1 to get a session_id, then call generate_summary with that session_id. Show the result." \
    check_generate_summary
else
  echo ""
  echo "No embeddings available — skipping Phase 2 tests"
  echo "[12/${TOTAL}] SKIP  search_semantic  (no embeddings)"
  echo "[13/${TOTAL}] SKIP  generate_summary  (no embeddings)"
  SKIP=$((SKIP + 2))
  TOTAL=$((TOTAL - 2))
fi

# ── Summary ─────────────────────────────────────────────────────────────────

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "${PASS}/${TOTAL} passed"
[[ $SKIP -gt 0 ]] && echo "${SKIP} test(s) skipped"
[[ $FAIL -eq 0 ]] && echo "All tests passed!" || echo "${FAIL} test(s) failed"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

[[ $FAIL -eq 0 ]] && exit 0 || exit 1
