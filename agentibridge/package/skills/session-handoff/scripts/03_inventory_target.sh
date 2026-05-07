#!/usr/bin/env bash
set -euo pipefail

# Purpose:  Probe the target project for its handoff-relevant context files
#           (CLAUDE.md, README.md, MEMORY.md, memory/ dir contents) and emit
#           a compact JSON summary the AI step can use to decide what to read.
# Inputs:   $1 — absolute project path
#           $2 — absolute memory/ directory path (from 02_encode_memory_dir.sh)
# Outputs:  stdout — single-line JSON object with keys:
#             project_path, claude_md, claude_md_lines,
#             readme_md, readme_md_lines,
#             memory_dir, memory_files_count, memory_index_exists,
#             prior_handoffs (array of filenames, max 3)
#           exit 0 — probe complete (even if files are missing)
# Idempotent: Yes — read-only.

if [ $# -lt 2 ]; then
  echo "ERROR: usage: $0 <project_path> <memory_dir>" >&2
  exit 1
fi

project="$1"
memdir="$2"

claude_md="${project}/CLAUDE.md"
readme_md="${project}/README.md"
memory_index="${memdir}/MEMORY.md"

count_lines() {
  if [ -f "$1" ]; then wc -l < "$1" | tr -d ' '; else echo 0; fi
}

bool() {
  if [ -f "$1" ]; then echo true; else echo false; fi
}

claude_exists=$(bool "$claude_md")
readme_exists=$(bool "$readme_md")
claude_lines=$(count_lines "$claude_md")
readme_lines=$(count_lines "$readme_md")
memory_index_exists=$(bool "$memory_index")

memory_files_count=0
if [ -d "$memdir" ]; then
  memory_files_count=$(find "$memdir" -maxdepth 1 -type f -name '*.md' 2>/dev/null | wc -l | tr -d ' ')
fi

# Sample up to 3 prior handoff-style files for tone-matching
prior_handoffs="[]"
if [ -d "$memdir" ]; then
  mapfile -t prior < <(
    find "$memdir" -maxdepth 1 -type f \
      \( -name 'project_handoff_*.md' -o -name 'project_*_session_*.md' \) \
      2>/dev/null | head -3
  )
  if [ "${#prior[@]}" -gt 0 ]; then
    # Build JSON array of basenames
    prior_handoffs="["
    sep=""
    for f in "${prior[@]}"; do
      bn="$(basename "$f")"
      # Escape double quotes conservatively
      bn="${bn//\"/\\\"}"
      prior_handoffs="${prior_handoffs}${sep}\"${bn}\""
      sep=","
    done
    prior_handoffs="${prior_handoffs}]"
  fi
fi

# Emit JSON on a single line — downstream can jq or AI-parse
printf '{"project_path":"%s","claude_md":%s,"claude_md_lines":%s,"readme_md":%s,"readme_md_lines":%s,"memory_dir":"%s","memory_files_count":%s,"memory_index_exists":%s,"prior_handoffs":%s}\n' \
  "$project" \
  "$claude_exists" "$claude_lines" \
  "$readme_exists" "$readme_lines" \
  "$memdir" "$memory_files_count" "$memory_index_exists" \
  "$prior_handoffs"
