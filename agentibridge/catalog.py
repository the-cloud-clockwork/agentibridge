"""Knowledge catalog — Memory files and History.

Pure data models and filesystem scanning functions for these two
knowledge categories. No Redis, no store — just dataclasses and reads.
"""

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple

from agentibridge.parser import decode_project_path


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class MemoryFile:
    project_encoded: str  # "-home-iamroot-dev-agentibridge"
    project_path: str  # "/home/iamroot/dev/agentibridge"
    filename: str  # "MEMORY.md"
    filepath: str  # absolute path
    content: str  # full markdown (truncated at max_content)
    file_size_bytes: int
    last_modified: str  # ISO timestamp from mtime

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryFile":
        data = dict(data)
        if isinstance(data.get("file_size_bytes"), str):
            data["file_size_bytes"] = int(data["file_size_bytes"])
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})  # NOSONAR


@dataclass
class HistoryEntry:
    display: str  # User prompt text
    timestamp: str  # ISO timestamp (converted from epoch ms)
    project: str  # Full project path
    session_id: str  # Session UUID

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "HistoryEntry":
        data = dict(data)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})  # NOSONAR


# ---------------------------------------------------------------------------
# Scanning functions
# ---------------------------------------------------------------------------


def scan_memory_files(base_dir: Path, max_content: int = 51200) -> List[MemoryFile]:
    """Scan {base_dir}/*/memory/*.md for memory files.

    Args:
        base_dir: The projects base directory (e.g. ~/.claude/projects/)
        max_content: Maximum bytes of content to read per file

    Returns:
        List of MemoryFile objects with content loaded.
    """
    base_dir = Path(base_dir)
    if not base_dir.exists():
        return []

    results: List[MemoryFile] = []
    for project_dir in sorted(base_dir.iterdir()):
        if not project_dir.is_dir():
            continue

        memory_dir = project_dir / "memory"
        if not memory_dir.is_dir():
            continue

        project_encoded = project_dir.name
        project_path = decode_project_path(project_encoded)

        for md_file in sorted(memory_dir.glob("*.md")):
            if not md_file.is_file():
                continue
            try:
                stat = md_file.stat()
                content = md_file.read_text(encoding="utf-8")[:max_content]
                mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
                results.append(
                    MemoryFile(
                        project_encoded=project_encoded,
                        project_path=project_path,
                        filename=md_file.name,
                        filepath=str(md_file),
                        content=content,
                        file_size_bytes=stat.st_size,
                        last_modified=mtime,
                    )
                )
            except (OSError, UnicodeDecodeError):
                continue

    return results


def parse_history(
    history_file: Path,
    offset: int = 0,
) -> Tuple[List[HistoryEntry], int]:
    """Parse history.jsonl incrementally from byte offset.

    Each line is a JSON object with: display, timestamp (epoch ms),
    projectPath, sessionId.

    Args:
        history_file: Path to ~/.claude/history.jsonl
        offset: Byte offset to resume from

    Returns:
        (entries, new_byte_offset)
    """
    history_file = Path(history_file)
    if not history_file.exists():
        return [], offset

    file_size = history_file.stat().st_size
    if file_size <= offset:
        return [], offset

    entries: List[HistoryEntry] = []
    new_offset = offset

    with open(history_file, "r", encoding="utf-8") as f:
        if offset > 0:
            f.seek(offset)
            # Check if we're mid-line by peeking at byte before offset
            at_boundary = True
            try:
                with open(history_file, "rb") as fb:
                    if offset > 0:
                        fb.seek(offset - 1)
                        at_boundary = fb.read(1) == b"\n"
            except OSError:
                at_boundary = False
            if not at_boundary:
                # Skip partial line remainder
                remainder = f.readline()
                new_offset = offset + len(remainder.encode("utf-8"))

        for line in f:
            line_bytes = len(line.encode("utf-8"))
            new_offset += line_bytes

            line = line.strip()
            if not line:
                continue

            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not isinstance(obj, dict):
                continue

            display = obj.get("display", "")
            if not display:
                continue

            # Convert epoch-ms timestamp to ISO 8601
            ts_raw = obj.get("timestamp")
            if isinstance(ts_raw, (int, float)) and ts_raw > 0:
                ts_iso = datetime.fromtimestamp(ts_raw / 1000, tz=timezone.utc).isoformat()
            elif isinstance(ts_raw, str):
                ts_iso = ts_raw
            else:
                ts_iso = ""

            entries.append(
                HistoryEntry(
                    display=display,
                    timestamp=ts_iso,
                    project=obj.get("projectPath", ""),
                    session_id=obj.get("sessionId", ""),
                )
            )

    # Fix offset for initial full-file reads
    if offset == 0:
        new_offset = file_size

    return entries, new_offset
