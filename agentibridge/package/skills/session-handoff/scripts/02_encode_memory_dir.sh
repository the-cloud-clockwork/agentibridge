#!/usr/bin/env bash
set -euo pipefail

# Purpose:  Encode a project absolute path into the Claude Code auto-memory
#           directory name convention and ensure the memory/ subdir exists.
#           Convention: /home/user/dev/foo/bar → -home-user-dev-foo-bar
#           Full path: ~/.claude/projects/-home-user-dev-foo-bar/memory/
# Inputs:   $1 — absolute project path (from 01_resolve_target.sh)
# Outputs:  stdout — absolute path to the memory/ directory
#           exit 0 — created or already present
# Idempotent: Yes — mkdir -p.

if [ $# -lt 1 ]; then
  echo "ERROR: absolute project path required" >&2
  exit 1
fi

abs="$1"
case "$abs" in
  /*) ;;
  *) echo "ERROR: path must be absolute: $abs" >&2; exit 1 ;;
esac

# Replace all "/" with "-". The leading slash becomes a leading dash, which
# matches the Claude Code auto-memory directory naming scheme.
encoded="${abs//\//-}"

memory_dir="$HOME/.claude/projects/${encoded}/memory"

mkdir -p "$memory_dir"

# Verify it actually exists and is writable
if [ ! -d "$memory_dir" ]; then
  echo "ERROR: failed to create memory directory: $memory_dir" >&2
  exit 1
fi
if [ ! -w "$memory_dir" ]; then
  echo "ERROR: memory directory is not writable: $memory_dir" >&2
  exit 1
fi

echo "$memory_dir"
