"""Transcript JSONL parser for raw Claude Code CLI transcripts.

Handles the format found in ~/.claude/projects/{project}/{session}.jsonl.
Pure functions, no state.

Entry types in raw transcripts:
  - user       — human messages (content: string or block list)
  - assistant  — Claude responses (content: block list with text/tool_use/thinking)
  - summary    — session summaries
  - system     — system messages
  - progress, queue-operation, file-history-snapshot — internal (skip)
"""

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional, Tuple


# Types to index (all others are skipped)
_INDEX_TYPES = {"user", "assistant", "summary", "system"}

# Types to skip entirely
_SKIP_TYPES = {"progress", "queue-operation", "file-history-snapshot"}


@dataclass
class SessionMeta:
    session_id: str
    project_encoded: str
    project_path: str
    cwd: str
    git_branch: str
    start_time: str
    last_update: str
    num_user_turns: int
    num_assistant_turns: int
    num_tool_calls: int
    summary: str
    transcript_path: str
    has_subagents: bool
    file_size_bytes: int
    codename: str = ""  # slug field from JSONL entries

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SessionMeta":
        data = dict(data)
        for int_field in ("num_user_turns", "num_assistant_turns", "num_tool_calls", "file_size_bytes"):
            if int_field in data and isinstance(data[int_field], str):
                data[int_field] = int(data[int_field])
        if isinstance(data.get("has_subagents"), str):
            data["has_subagents"] = data["has_subagents"].lower() == "true"
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})  # NOSONAR


@dataclass
class SessionEntry:
    entry_type: str
    timestamp: str
    content: str
    tool_names: List[str] = field(default_factory=list)
    uuid: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SessionEntry":
        data = dict(data)
        if isinstance(data.get("tool_names"), str):
            data["tool_names"] = json.loads(data["tool_names"])
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})  # NOSONAR


def decode_project_path(encoded: str) -> str:
    """Decode Claude's path-encoded project directory name.

    '-home-iamroot-dev-agenticore' -> '/home/iamroot/dev/agenticore'

    Note: lossy for double-dash worktree dirs (best-effort). Also assumes a
    case-sensitive filesystem for the `/`<->`-` round trip; on case-insensitive
    APFS (macOS default), two project dirs differing only by case collide.
    """
    if not encoded:
        return encoded
    if encoded.startswith("-"):
        return "/" + encoded[1:].replace("-", "/")
    return encoded.replace("-", "/")


def scan_projects_dir(base_dir: Optional[Path] = None) -> List[Tuple[str, str, Path]]:
    """Scan ~/.claude/projects/ for all session JSONL files.

    Returns list of (session_id, project_encoded, filepath) tuples.
    Only includes main session files (not subagent files).
    """
    if base_dir is None:
        base_dir = Path(os.getenv("CLAUDE_CODE_HOME_DIR", str(Path.home() / ".claude"))) / "projects"
    else:
        base_dir = Path(base_dir)

    if not base_dir.exists():
        return []

    results = []
    for project_dir in base_dir.iterdir():
        if not project_dir.is_dir():
            continue
        project_encoded = project_dir.name
        for jsonl_file in project_dir.glob("*.jsonl"):
            # Skip files inside session subdirectories (subagents)
            if jsonl_file.parent != project_dir:
                continue
            session_id = jsonl_file.stem
            results.append((session_id, project_encoded, jsonl_file))

    return results


def extract_user_content(message: dict) -> Tuple[str, bool]:
    """Extract text from a user message.

    Returns (text, is_tool_result).

    Handles:
      - String content: message.content is a plain string
      - Block list with text: [{type: "text", text: "..."}]
      - Block list with tool_result: [{type: "tool_result", ...}] -> skip
      - Char array: ["I", "m", "p", ...] -> join
    """
    content = message.get("content")

    if content is None:
        return "", False

    if isinstance(content, str):
        return content, False

    if isinstance(content, list):
        if not content:
            return "", False

        first = content[0]

        # Char array: list of individual characters
        if isinstance(first, str):
            # Could be char array or list of strings
            if all(isinstance(c, str) and len(c) <= 1 for c in content[:20]):
                return "".join(content), False
            return " ".join(content), False

        # Block list
        if isinstance(first, dict):
            # Check for tool_result (internal, not human)
            if first.get("type") == "tool_result":
                return "", True

            texts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if text:
                        texts.append(text)
            return "\n".join(texts), False

    return "", False


def extract_assistant_content(message: dict) -> Tuple[str, List[str]]:
    """Extract text and tool names from an assistant message.

    Returns (text, tool_names).

    Handles block list with: text, tool_use, thinking (skip thinking).
    """
    content = message.get("content")
    if not isinstance(content, list):
        return "", []

    texts = []
    tool_names = []

    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type", "")

        if block_type == "text":
            text = block.get("text", "")
            if text:
                texts.append(text)
        elif block_type == "tool_use":
            name = block.get("name", "unknown")
            tool_names.append(name)

    return "\n".join(texts), tool_names


def parse_transcript_entries(
    filepath: Path,
    offset: int = 0,
) -> Tuple[List[SessionEntry], int]:
    """Parse transcript entries from byte offset.

    Returns (entries, new_byte_offset).
    Filters out non-indexable types and tool_result user messages.
    """
    filepath = Path(filepath)
    if not filepath.exists():
        return [], offset

    file_size = filepath.stat().st_size
    if file_size <= offset:
        return [], offset

    entries = []
    new_offset = offset

    with open(filepath, "r", encoding="utf-8") as f:
        if offset > 0:
            f.seek(offset)
            # Skip partial line if we seeked into the middle of one
            remainder = f.readline()
            if offset > 0 and remainder:
                new_offset = offset + len(remainder.encode("utf-8"))

        for line in f:
            line_bytes = len(line.encode("utf-8"))
            new_offset += line_bytes if offset == 0 else line_bytes

            line = line.strip()
            if not line:
                continue

            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not isinstance(entry, dict):
                continue

            entry_type = entry.get("type", "")
            if entry_type in _SKIP_TYPES or entry_type not in _INDEX_TYPES:
                continue

            timestamp = entry.get("timestamp", "")
            uuid = entry.get("uuid", "")
            message = entry.get("message", {})

            if entry_type == "user":
                if not isinstance(message, dict):
                    continue
                text, is_tool_result = extract_user_content(message)
                if is_tool_result or not text:
                    continue
                entries.append(
                    SessionEntry(
                        entry_type="user",
                        timestamp=timestamp,
                        content=text[:2000],
                        uuid=uuid,
                    )
                )

            elif entry_type == "assistant":
                if not isinstance(message, dict):
                    continue
                text, tool_names = extract_assistant_content(message)
                if not text and not tool_names:
                    continue
                entries.append(
                    SessionEntry(
                        entry_type="assistant",
                        timestamp=timestamp,
                        content=text[:2000],
                        tool_names=tool_names,
                        uuid=uuid,
                    )
                )

            elif entry_type == "summary":
                summary_text = entry.get("summary", "")
                if summary_text:
                    entries.append(
                        SessionEntry(
                            entry_type="summary",
                            timestamp=timestamp,
                            content=summary_text[:2000],
                            uuid=uuid,
                        )
                    )

            elif entry_type == "system":
                if isinstance(message, dict):
                    content = message.get("content", "")
                    if isinstance(content, str) and content:
                        entries.append(
                            SessionEntry(
                                entry_type="system",
                                timestamp=timestamp,
                                content=content[:2000],
                                uuid=uuid,
                            )
                        )

    # Fix offset for initial full-file reads
    if offset == 0:
        new_offset = file_size

    return entries, new_offset


def parse_transcript_meta(
    filepath: Path,
    project_encoded: str,
    entries: Optional[List[SessionEntry]] = None,
) -> Optional[SessionMeta]:
    """Build session metadata from a transcript file.

    If entries are provided, uses them directly. Otherwise reads first/last
    lines for quick metadata extraction.
    """
    filepath = Path(filepath)
    if not filepath.exists():
        return None

    session_id = filepath.stem
    file_size = filepath.stat().st_size

    # Check for subagent directories
    session_subdir = filepath.parent / session_id
    has_subagents = session_subdir.exists() and (session_subdir / "subagents").exists()

    # Extract metadata from entries or file
    cwd = ""
    git_branch = ""
    start_time = ""
    last_update = ""
    num_user = 0
    num_assistant = 0
    num_tools = 0
    summary = ""

    if entries:
        _extract_meta_from_entries(
            entries,
            locals_dict := {
                "cwd": cwd,
                "git_branch": git_branch,
                "start_time": start_time,
                "last_update": last_update,
                "num_user": num_user,
                "num_assistant": num_assistant,
                "num_tools": num_tools,
                "summary": summary,
            },
        )
    else:
        # Quick parse: read first few relevant entries from file
        locals_dict = _quick_parse_meta(filepath)

    cwd = locals_dict.get("cwd", "")
    git_branch = locals_dict.get("git_branch", "")
    start_time = locals_dict.get("start_time", "")
    last_update = locals_dict.get("last_update", "")
    num_user = locals_dict.get("num_user", 0)
    num_assistant = locals_dict.get("num_assistant", 0)
    num_tools = locals_dict.get("num_tools", 0)
    summary = locals_dict.get("summary", "")

    codename = locals_dict.get("codename", "")

    return SessionMeta(
        session_id=session_id,
        project_encoded=project_encoded,
        project_path=decode_project_path(project_encoded),
        cwd=cwd,
        git_branch=git_branch,
        start_time=start_time,
        last_update=last_update,
        num_user_turns=num_user,
        num_assistant_turns=num_assistant,
        num_tool_calls=num_tools,
        summary=summary,
        transcript_path=str(filepath),
        has_subagents=has_subagents,
        file_size_bytes=file_size,
        codename=codename,
    )


def _extract_meta_from_entries(entries: List[SessionEntry], out: dict) -> None:
    """Extract metadata fields from parsed entries into out dict."""
    for entry in entries:
        if entry.entry_type == "user":
            out["num_user"] = out.get("num_user", 0) + 1
            if not out.get("summary"):
                out["summary"] = entry.content[:200]
        elif entry.entry_type == "assistant":
            out["num_assistant"] = out.get("num_assistant", 0) + 1
            out["num_tools"] = out.get("num_tools", 0) + len(entry.tool_names)
        elif entry.entry_type == "summary":
            out["summary"] = entry.content[:200]

        if entry.timestamp:
            if not out.get("start_time"):
                out["start_time"] = entry.timestamp
            out["last_update"] = entry.timestamp


def _quick_parse_meta(filepath: Path) -> dict:
    """Quick metadata extraction by scanning the file.

    Reads until we have enough data for metadata (first user entry +
    counts of types). Scans the whole file for counts but extracts
    content only from first entries.
    """
    result = {
        "cwd": "",
        "git_branch": "",
        "start_time": "",
        "last_update": "",
        "num_user": 0,
        "num_assistant": 0,
        "num_tools": 0,
        "summary": "",
        "codename": "",
    }

    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not isinstance(entry, dict):
                continue

            entry_type = entry.get("type", "")
            timestamp = entry.get("timestamp", "")

            if not result["codename"]:
                slug = entry.get("slug", "")
                if slug:
                    result["codename"] = slug

            if entry_type not in _INDEX_TYPES:
                continue

            if timestamp:
                if not result["start_time"]:
                    result["start_time"] = timestamp
                result["last_update"] = timestamp

            if entry_type == "user":
                result["num_user"] += 1
                # Extract cwd/branch from first user entry
                if not result["cwd"]:
                    result["cwd"] = entry.get("cwd", "")
                    result["git_branch"] = entry.get("gitBranch", "")
                # First user message as summary fallback
                if not result["summary"]:
                    msg = entry.get("message", {})
                    if isinstance(msg, dict):
                        text, is_tr = extract_user_content(msg)
                        if not is_tr and text:
                            result["summary"] = text[:200]

            elif entry_type == "assistant":
                result["num_assistant"] += 1
                msg = entry.get("message", {})
                if isinstance(msg, dict):
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                result["num_tools"] += 1

            elif entry_type == "summary":
                result["summary"] = entry.get("summary", "")[:200]

    return result
