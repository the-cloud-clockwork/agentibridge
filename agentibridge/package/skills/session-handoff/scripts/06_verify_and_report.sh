#!/usr/bin/env bash
set -euo pipefail

# Purpose:  Final verification — confirm both the handoff file and the
#           MEMORY.md index exist, that MEMORY.md actually references the
#           handoff by its basename, and print a short summary for the
#           caller. Fails loudly if anything is missing so the AI step
#           won't tell the operator "done" when it isn't.
# Inputs:   $1 — absolute path to the handoff file
#           $2 — absolute path to MEMORY.md
# Outputs:  stdout — multi-line summary (handoff path, index path, match count)
#           exit 0 — verified; exit 1 — missing or unreferenced
# Idempotent: Yes — read-only.

if [ $# -lt 2 ]; then
  echo "ERROR: usage: $0 <handoff_file> <memory_index>" >&2
  exit 1
fi

handoff="$1"
index="$2"

if [ ! -f "$handoff" ]; then
  echo "ERROR: handoff file is missing: $handoff" >&2
  exit 1
fi
if [ ! -f "$index" ]; then
  echo "ERROR: memory index is missing: $index" >&2
  exit 1
fi

handoff_basename="$(basename "$handoff")"
# Count references to the handoff filename in the index file. Must be >= 1.
refs=$(grep -c "\[${handoff_basename}\]" "$index" || true)
if [ "${refs:-0}" -lt 1 ]; then
  echo "ERROR: $index does not reference $handoff_basename" >&2
  exit 1
fi

handoff_bytes=$(wc -c < "$handoff" | tr -d ' ')
handoff_lines=$(wc -l < "$handoff" | tr -d ' ')

cat <<EOF
handoff_file=$handoff
handoff_bytes=$handoff_bytes
handoff_lines=$handoff_lines
memory_index=$index
index_refs=$refs
status=ok
EOF
