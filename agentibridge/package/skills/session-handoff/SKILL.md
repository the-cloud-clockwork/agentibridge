---
name: session-handoff
description: >
  Cross-project session handoff via MCP dispatch (primary) or memory file (fallback).
  Creates a resumable Claude session in the target project seeded with context from
  the current conversation. If the home-bridge MCP is unavailable, falls back to
  writing a dated memory file into the target project's auto-memory directory.
  Use when the user says "hand off to <repo>", "save handoff for <repo>",
  "make <repo> aware of what we did", or "so next session knows where we left off".
argument-hint: "<target-repo-path> [headline hint]"
---

# session-handoff — Cross-Project Session Handoff

Hand off context from the current session to another project. Two paths:

1. **MCP path (primary):** Call `home-bridge` `handoff` tool → spawns a Claude
   session in the target project seeded with the handoff content. Returns a
   `session_id` + `resume_command` the operator can use anytime.
2. **Memory-file path (fallback):** Write a dated memory file into the target
   project's auto-memory directory + index in MEMORY.md. Used when `home-bridge`
   MCP is not available in the current session.

**When NOT to use**: this is for cross-project context handoff. If you
just want to save notes inside the CURRENT project's memory, use normal
auto-memory writes — not this skill.

## Script Resolution

```bash
SKILL_DIR="$(dirname "$(readlink -f ~/.claude/skills/session-handoff/SKILL.md 2>/dev/null)" 2>/dev/null)"
if [ -z "$SKILL_DIR" ] || [ ! -d "$SKILL_DIR" ]; then
  SKILL_DIR="$HOME/.claude/skills/session-handoff"
fi
```

---

## Step 1 — Parse arguments and resolve the target project

The first argument is the **target repo path** (absolute or relative).
Everything after it is an optional one-line headline hint.

<!-- DETERMINISTIC: validate the target path and encode the auto-memory directory name -->
```bash
TARGET_PATH=$("$SKILL_DIR"/scripts/01_resolve_target.sh "$1")
MEMORY_DIR=$("$SKILL_DIR"/scripts/02_encode_memory_dir.sh "$TARGET_PATH")
```

If the target path is not a directory, stop and tell the user.

---

## Step 2 — Inventory the target project (read-only)

<!-- DETERMINISTIC: gather target context files -->
```bash
"$SKILL_DIR"/scripts/03_inventory_target.sh "$TARGET_PATH" "$MEMORY_DIR"
```

Read `CLAUDE.md` fully if present. Read the first 50 lines of `MEMORY.md`
if present so your new entry matches the index style. Sample at most two
prior handoff files to match tone.

---

## Step 3 — Synthesize the handoff (AI-JUDGMENT)

Use the CURRENT conversation as the source of truth. Write the body in
this structure — every section is required unless noted:

1. **Headline** — 3-5 bullets covering the biggest durable outcomes.
   If the operator passed a headline hint as arg 2, fold it in.

2. **What shipped today** — ordered list of concrete changes with
   commit SHAs where available. Include workarounds encountered.

3. **Current state one-liner per axis** — pick axes that matter:
   inference, observability, pipeline, CI, infra, data, etc.

4. **New roadmap (priorities)** — numbered list. Flag **POSTPONED**
   items with reason.

5. **Known debt** (optional) — pre-existing bugs that bit this
   session but are NOT the handoff project's fault.

6. **Likely operator next asks** — 3-5 items. Pre-compute answers.

7. **How to apply** — directives to future-you about how to USE this
   context: what files to read, what NOT to do without asking.

**Writing rules:**
- Date with today's date in `YYYY-MM-DD` form.
- Use absolute paths for every file/commit/resource reference.
- No placeholder text. Every bullet must be specific.
- Tone matches the target project's CLAUDE.md.

---

## Step 4 — Dispatch: MCP primary path

Try the `home-bridge` MCP `handoff` tool first. This is the preferred path.

### 4A — Compose the handoff prompt

Format the synthesized content from Step 3 into a prompt for the receiving
session. Structure it as:

```
You are receiving a cross-project handoff from [source-project-name].
Session date: YYYY-MM-DD.

[Full synthesized handoff content from Step 3]

INSTRUCTION: Ingest this context. Do not take any action. Respond with
a one-line acknowledgment confirming you have the handoff loaded.
The operator will resume this session when ready.
```

### 4B — Call the MCP tool

Call the `home-bridge` `handoff` MCP tool with:

- `prompt`: the composed handoff prompt from 4A
- `project_path`: `TARGET_PATH` (the resolved absolute path)
- `model`: `"sonnet"` (lightweight — just ingesting context)
- `max_turns`: `1`
- `timeout`: `120`

### 4C — Parse response

Extract from the response:
- `session_id` or session identifier
- Any `resume_command` or construct one: `cd <TARGET_PATH> && claude --resume <session_id>`

If the call **succeeds** → go to Step 5 (MCP report).

If the call **fails** (tool not found, MCP server not loaded, connection
error, timeout, or `success: false` in response) → fall through to Step 4-fallback.

---

## Step 4-fallback — Memory file (when MCP is unavailable)

This is the existing memory-file pipeline. Used when `home-bridge` is not
available or the MCP call fails.

### 4F-A — Write the handoff file

<!-- DETERMINISTIC: compose the filename -->
```bash
HANDOFF_FILE="$MEMORY_DIR/project_handoff_from_$(basename "$PWD")_$(date +%Y_%m_%d).md"
```

Write the file with YAML frontmatter:

```yaml
---
name: Handoff from <source-project-name> — YYYY-MM-DD
description: >
  One-line description. Include "read this when the operator asks
  where did we leave off" for retrieval.
type: project
---
```

Then the full synthesized content from Step 3.

### 4F-B — Validate frontmatter

<!-- DETERMINISTIC: parse the frontmatter and confirm required fields -->
```bash
"$SKILL_DIR"/scripts/04_validate_frontmatter.sh "$HANDOFF_FILE"
```

### 4F-C — Index in MEMORY.md

<!-- DETERMINISTIC: create or update MEMORY.md -->
```bash
"$SKILL_DIR"/scripts/05_update_memory_index.sh "$MEMORY_DIR" "$HANDOFF_FILE"
```

### 4F-D — Verify

<!-- DETERMINISTIC: confirm both files exist -->
```bash
"$SKILL_DIR"/scripts/06_verify_and_report.sh "$HANDOFF_FILE" "$MEMORY_DIR/MEMORY.md"
```

Go to Step 5 (fallback report).

---

## Step 5 — Report to operator

### If MCP path succeeded:

```
## Handoff dispatched to <target-project-name>

**Path:** MCP (home-bridge)
**Session ID:** <session_id>
**Resume command:**
  cd <TARGET_PATH> && claude --resume <session_id>

Headline:
  • <first bullet>
  • <second bullet>
  • <third bullet>

Next likely asks (pre-answered in the handoff):
  1. <first likely ask>
  2. <second likely ask>
  3. <third likely ask>
```

### If fallback path was used:

```
## Handoff saved to <target-project-name>

**Path:** Memory file (home-bridge unavailable)
**Memory file:** <absolute path to handoff file>
**Indexed in:** <absolute path to MEMORY.md>

The next Claude Code session opened in <target-path> will see the
handoff at the top of its memory index.

Headline:
  • <first bullet>
  • <second bullet>
  • <third bullet>

Next likely asks (pre-answered in the handoff):
  1. <first likely ask>
  2. <second likely ask>
  3. <third likely ask>
```

---

## Failure modes

- **Target path doesn't exist** → stop in Step 1, tell the user.
- **Target has no auto-memory directory** → Step 1 creates it (fallback path only).
- **MCP tool not available** → silent fallback to memory-file path.
- **MCP call returns `success: false`** → log the error, fall through to memory-file path.
- **Current session has nothing to hand off** → refuse in Step 3.
- **Operator runs this twice in one day for the same target** →
  MCP path: creates a new session (both are resumable).
  Fallback path: overwrites the filename (same date), updates pointer.

---

## Extracted Scripts (fallback path only)

| Script | Purpose | Idempotent | Validated |
|---|---|---|---|
| `01_resolve_target.sh` | Validate target repo path, return absolute path | Yes | Syntax-checked |
| `02_encode_memory_dir.sh` | Encode path → auto-memory dir name, create if missing | Yes | Syntax-checked |
| `03_inventory_target.sh` | Probe CLAUDE.md, README.md, MEMORY.md — return JSON | Yes | Syntax-checked |
| `04_validate_frontmatter.sh` | Confirm YAML frontmatter has required fields | Yes | Syntax-checked |
| `05_update_memory_index.sh` | Insert/update Session Handoffs section in MEMORY.md | Yes | Syntax-checked |
| `06_verify_and_report.sh` | Confirm file presence, print paths, fail loudly on missing | Yes | Syntax-checked |
