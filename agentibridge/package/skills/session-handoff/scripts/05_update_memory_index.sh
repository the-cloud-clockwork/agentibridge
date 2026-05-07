#!/usr/bin/env bash
set -euo pipefail

# Purpose:  Insert or update a "## Session Handoffs" section at the top of
#           the target MEMORY.md file, with a pointer to the given handoff
#           file. Idempotent — re-running replaces the pointer line for the
#           same filename instead of duplicating it.
#
# Inputs:   $1 — absolute path to the memory/ directory
#           $2 — absolute path to the handoff file that lives inside it
# Outputs:  stdout — absolute path to the updated MEMORY.md
#           exit 0 — success
# Idempotent: Yes — same handoff filename produces the same index line.
# Dependencies: awk, grep, mv (coreutils).

if [ $# -lt 2 ]; then
  echo "ERROR: usage: $0 <memory_dir> <handoff_file>" >&2
  exit 1
fi

memdir="$1"
handoff="$2"

if [ ! -d "$memdir" ]; then
  echo "ERROR: memory dir does not exist: $memdir" >&2
  exit 1
fi
if [ ! -f "$handoff" ]; then
  echo "ERROR: handoff file does not exist: $handoff" >&2
  exit 1
fi

index="$memdir/MEMORY.md"
handoff_basename="$(basename "$handoff")"

# Extract the description from the handoff frontmatter to use as the tooltip.
# Description may span multiple lines (YAML folded scalar with `>`), so gather
# continuation lines until the next key or `---`.
description="$(awk '
  BEGIN { inside = 0; found = 0 }
  /^---[[:space:]]*$/ {
    if (inside == 0) { inside = 1; next }
    else { exit }
  }
  inside && /^description:/ {
    found = 1
    sub(/^description:[[:space:]]*>?[[:space:]]*/, "", $0)
    if ($0 != "") { printf "%s ", $0 }
    next
  }
  inside && found && /^[[:space:]]+[^ ]/ {
    sub(/^[[:space:]]+/, "", $0)
    printf "%s ", $0
    next
  }
  inside && found && /^[a-zA-Z_]+:/ { exit }
' "$handoff" | sed 's/[[:space:]]*$//')"

if [ -z "$description" ]; then
  description="Session handoff"
fi

# Extract date tag from filename (YYYY_MM_DD → YYYY-MM-DD)
date_tag="$(echo "$handoff_basename" | grep -oE '[0-9]{4}_[0-9]{2}_[0-9]{2}' | head -1 | tr '_' '-' || true)"
if [ -z "$date_tag" ]; then
  date_tag="$(date +%Y-%m-%d)"
fi

pointer_line="- [${handoff_basename}](${handoff_basename}) — **HANDOFF ${date_tag}** — ${description}"

# Case A — MEMORY.md does not exist: create it fresh.
if [ ! -f "$index" ]; then
  cat > "$index" <<EOF
# Memory Index

## Session Handoffs (read first when the operator asks "where did we leave off")
${pointer_line}
EOF
  echo "$index"
  exit 0
fi

# Case B — MEMORY.md exists. Upsert the pointer inside a "## Session Handoffs"
# section. If the section doesn't exist, insert it right after the first
# top-level `# ` heading. If neither exist, prepend the section at the top.
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

awk -v pointer="$pointer_line" -v fname="$handoff_basename" '
  BEGIN {
    in_handoff_section = 0
    handoff_section_seen = 0
    pointer_written = 0
    top_heading_line = -1
    line_count = 0
  }
  {
    lines[++line_count] = $0
    if (top_heading_line == -1 && $0 ~ /^# /) {
      top_heading_line = line_count
    }
  }
  END {
    # First pass: find the Session Handoffs section and capture its bounds
    section_start = -1
    section_end = -1
    for (i = 1; i <= line_count; i++) {
      if (lines[i] ~ /^## Session Handoffs/) {
        section_start = i
        for (j = i + 1; j <= line_count; j++) {
          if (lines[j] ~ /^## /) { section_end = j - 1; break }
        }
        if (section_end == -1) section_end = line_count
        break
      }
    }

    for (i = 1; i <= line_count; i++) {
      if (section_start > 0 && i == section_start) {
        # Print the section header
        print lines[i]
        # Print every existing line in the section EXCEPT any stale pointer
        # that references the same filename (to make it idempotent).
        for (j = i + 1; j <= section_end; j++) {
          if (lines[j] ~ ("\\[" fname "\\]")) continue
          print lines[j]
        }
        # Append the fresh pointer at the end of the section
        print pointer
        pointer_written = 1
        i = section_end
        continue
      }
      print lines[i]
      # If there is no Session Handoffs section and we just printed the top
      # heading, inject the section right after it.
      if (section_start < 0 && top_heading_line > 0 && i == top_heading_line) {
        print ""
        print "## Session Handoffs (read first when the operator asks \"where did we leave off\")"
        print pointer
        print ""
        pointer_written = 1
      }
    }

    # Final safety net: if nothing above injected the pointer (no top heading,
    # no existing section), prepend the section.
    if (!pointer_written) {
      # This path is rare but correct — re-emit everything with a header.
      # Print nothing here because we already printed the body above;
      # signal via exit code instead.
      exit 2
    }
  }
' "$index" > "$tmp" || {
  status=$?
  if [ "$status" = "2" ]; then
    # Fallback: prepend new section header + pointer to the existing file
    {
      echo "# Memory Index"
      echo ""
      echo "## Session Handoffs (read first when the operator asks \"where did we leave off\")"
      echo "$pointer_line"
      echo ""
      cat "$index"
    } > "$tmp"
  else
    echo "ERROR: awk failed with status $status on $index" >&2
    exit 1
  fi
}

mv "$tmp" "$index"
trap - EXIT

echo "$index"
