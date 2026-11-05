---
title: Examples
parent: Getting Started
nav_order: 9
---

# See It In Action
{: .fs-9 .fw-700 }

Real screenshots from Claude Code CLI and claude.ai using AgentiBridge tools.
{: .fs-5 .text-grey-dk-100 .mb-6 }

---

## Claude Code CLI

### `list_sessions` — Browse all sessions

![CLI list_sessions output showing 5 sessions with project, summary, turns, and tool calls]({{ '/docs/media/examples/cli-list-sessions.jpg' | relative_url }}){: .d-block .mx-auto .mb-4 }

The `list_sessions` tool returns sessions across all projects with metadata — summaries, turn counts, tool call stats, and timestamps. Filter by project or time range.
{: .text-grey-dk-100 }

---

## claude.ai

### `list_sessions` — Session listing from the web

![claude.ai list_sessions showing 3 recent sessions with metadata]({{ '/docs/media/examples/claude-ai-list-sessions.jpg' | relative_url }}){: .d-block .mx-auto .mb-4 }

The same tools work from claude.ai via the remote HTTP transport. Connect once with OAuth 2.1, then query your sessions from any browser.
{: .text-grey-dk-100 }

### `list_memory_files` — Memory catalog

![claude.ai list_memory_files showing 10 memory files across projects]({{ '/docs/media/examples/claude-ai-memory-files.jpg' | relative_url }}){: .d-block .mx-auto .mb-4 }

List all memory files across your Claude Code projects — the curated knowledge your agents have built up over time.
{: .text-grey-dk-100 }

### Memory contents

![claude.ai displaying memory file contents with recent learnings]({{ '/docs/media/examples/claude-ai-memory-contents.jpg' | relative_url }}){: .d-block .mx-auto .mb-4 }

Read the actual contents of any memory file. Use this to review what your agents have learned, or inject past context into new conversations.
{: .text-grey-dk-100 }

---

{: .note }
> **More examples coming** — dispatch jobs, semantic search results, session restore, and more MCP clients. Have a screenshot to share? [Open an issue](https://github.com/The-Cloud-Clockwork/agentibridge/issues).
