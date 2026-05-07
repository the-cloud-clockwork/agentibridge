#!/usr/bin/env bash
set -euo pipefail

# Purpose:  Validate the target repo path and echo its absolute path.
# Inputs:   $1 — target repo path (absolute or relative, ~ expanded)
# Outputs:  stdout — absolute resolved path
#           exit 0 — valid directory; exit 1 — invalid / not a dir
# Idempotent: Yes — read-only.

if [ $# -lt 1 ]; then
  echo "ERROR: target path argument required" >&2
  exit 1
fi

raw="$1"
# Expand leading ~ manually (bash does it already for unquoted, but be safe)
case "$raw" in
  "~") raw="$HOME" ;;
  "~/"*) raw="$HOME/${raw#~/}" ;;
esac

if [ ! -d "$raw" ]; then
  echo "ERROR: target path is not a directory: $raw" >&2
  exit 1
fi

# readlink -f resolves symlinks and gives absolute path
abs="$(readlink -f "$raw")"
if [ -z "$abs" ]; then
  echo "ERROR: could not resolve absolute path for: $raw" >&2
  exit 1
fi

echo "$abs"
