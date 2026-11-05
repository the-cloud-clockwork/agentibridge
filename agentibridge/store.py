"""Session store — Redis with file-fallback reads from ~/.claude/projects/.

Stores session metadata and transcript entries in Redis for fast access.
Falls back to reading directly from the raw JSONL files when Redis is
unavailable (no separate JSONL copy needed).

Phase 5 additions: memory files and history.
"""

import hashlib
import json
import os
from pathlib import Path
from time import time
from typing import List, Optional, Tuple

from agentibridge.catalog import (
    HistoryEntry,
    MemoryFile,
    parse_history,
    scan_memory_files,
)
from agentibridge.parser import (
    SessionEntry,
    SessionMeta,
    decode_project_path,
    parse_transcript_entries,
    parse_transcript_meta,
    scan_projects_dir,
)


_KEY_PREFIX: str = os.getenv("REDIS_KEY_PREFIX", "agentibridge")
_MAX_ENTRIES: int = int(os.getenv("AGENTIBRIDGE_MAX_ENTRIES", "500"))


def _rkey(suffix: str) -> str:
    """Build namespaced Redis key for session-bridge subsystem."""
    return f"{_KEY_PREFIX}:sb:{suffix}"


def _escape_redis_glob(s: str) -> str:
    """Escape Redis glob special characters in a string."""
    for ch in ("*", "?", "[", "]"):
        s = s.replace(ch, f"\\{ch}")
    return s


def _filepath_hash(filepath: str) -> str:
    """Short hash of filepath for position tracking keys."""
    return hashlib.md5(filepath.encode()).hexdigest()[:12]


class SessionStore:
    """Session transcript store with Redis + filesystem fallback."""

    def __init__(self) -> None:
        self._redis = None
        self._redis_checked = False
        _home = Path(os.getenv("CLAUDE_CODE_HOME_DIR", str(Path.home() / ".claude")))
        self._projects_dir = _home / "projects"
        self._history_file = _home / "history.jsonl"

    # ------------------------------------------------------------------
    # Redis connection (lazy, fail-safe)
    # ------------------------------------------------------------------

    def _get_redis(self):
        if self._redis_checked:
            return self._redis
        self._redis_checked = True
        try:
            from agentibridge.redis_client import get_redis

            self._redis = get_redis()
        except Exception:
            self._redis = None
        return self._redis

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upsert_session(self, meta: SessionMeta) -> None:
        """Create or update session metadata."""
        r = self._get_redis()
        if r is not None:
            self._redis_upsert_session(r, meta)

    def add_entries(self, session_id: str, entries: List[SessionEntry]) -> None:
        """Append entries to a session's transcript."""
        if not entries:
            return
        r = self._get_redis()
        if r is not None:
            self._redis_add_entries(r, session_id, entries)

    def get_session_meta(self, session_id: str) -> Optional[SessionMeta]:
        r = self._get_redis()
        if r is not None:
            return self._redis_get_meta(r, session_id)
        return self._file_get_meta(session_id)

    def get_session_entries(
        self,
        session_id: str,
        offset: int = 0,
        limit: int = 50,
    ) -> List[SessionEntry]:
        r = self._get_redis()
        if r is not None:
            return self._redis_get_entries(r, session_id, offset, limit)
        return self._file_get_entries(session_id, offset, limit)

    def list_session_ids(self) -> List[str]:
        """Return all session IDs from the index (lightweight, no metadata)."""
        r = self._get_redis()
        if r is not None:
            return r.zrevrange(_rkey("idx:all"), 0, -1)
        # File fallback: scan project dirs for session IDs
        from agentibridge.parser import scan_projects_dir

        return [sid for sid, _, _ in scan_projects_dir(self._projects_dir)]

    def list_sessions(
        self,
        project: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
        since_hours: int = 0,
    ) -> List[SessionMeta]:
        r = self._get_redis()
        if r is not None:
            return self._redis_list_sessions(r, project, limit, offset, since_hours)
        return self._file_list_sessions(project, limit, offset, since_hours)

    def search_sessions(
        self,
        query: str,
        project: Optional[str] = None,
        limit: int = 10,
    ) -> List[dict]:
        """Keyword search across session transcripts."""
        r = self._get_redis()
        if r is not None:
            return self._redis_search(r, query, project, limit)
        return self._file_search(query, project, limit)

    def count_entries(self, session_id: str) -> int:
        """Return the number of entries for a session without loading them all."""
        r = self._get_redis()
        if r is not None:
            return r.llen(_rkey(f"session:{session_id}:entries"))
        # File fallback: must parse
        for sid, project_encoded, filepath in scan_projects_dir(self._projects_dir):
            if sid == session_id:
                entries, _ = parse_transcript_entries(filepath)
                return len(entries)
        return 0

    def get_file_position(self, filepath: str) -> int:
        """Get stored byte offset for incremental reading."""
        r = self._get_redis()
        if r is not None:
            val = r.get(_rkey(f"pos:{_filepath_hash(filepath)}"))
            return int(val) if val else 0
        return self._file_get_position(filepath)

    def save_file_position(self, filepath: str, position: int) -> None:
        """Save byte offset for incremental reading."""
        r = self._get_redis()
        if r is not None:
            r.set(_rkey(f"pos:{_filepath_hash(filepath)}"), str(position))
        else:
            self._file_save_position(filepath, position)

    # ------------------------------------------------------------------
    # Redis implementation
    # ------------------------------------------------------------------

    def _redis_upsert_session(self, r, meta: SessionMeta) -> None:
        pipe = r.pipeline()
        meta_key = _rkey(f"session:{meta.session_id}:meta")

        # Store metadata as hash
        mapping = {
            "session_id": meta.session_id,
            "project_encoded": meta.project_encoded,
            "project_path": meta.project_path,
            "cwd": meta.cwd,
            "git_branch": meta.git_branch,
            "start_time": meta.start_time,
            "last_update": meta.last_update,
            "num_user_turns": str(meta.num_user_turns),
            "num_assistant_turns": str(meta.num_assistant_turns),
            "num_tool_calls": str(meta.num_tool_calls),
            "summary": meta.summary,
            "transcript_path": meta.transcript_path,
            "has_subagents": str(meta.has_subagents).lower(),
            "file_size_bytes": str(meta.file_size_bytes),
            "codename": meta.codename,
        }
        pipe.hset(meta_key, mapping=mapping)

        # Index by recency — sessions without timestamps sort to the bottom
        score = 0.0
        if meta.last_update:
            try:
                from datetime import datetime

                score = datetime.fromisoformat(meta.last_update.replace("Z", "+00:00")).timestamp()
            except (ValueError, AttributeError):
                score = 0.0

        pipe.zadd(_rkey("idx:all"), {meta.session_id: score})
        pipe.zadd(_rkey(f"idx:project:{meta.project_encoded}"), {meta.session_id: score})
        pipe.sadd(_rkey("idx:projects"), meta.project_encoded)
        pipe.execute()

    def _redis_add_entries(self, r, session_id: str, entries: List[SessionEntry]) -> None:
        entries_key = _rkey(f"session:{session_id}:entries")
        pipe = r.pipeline()
        for entry in entries:
            compact = json.dumps(
                {
                    "entry_type": entry.entry_type,
                    "timestamp": entry.timestamp,
                    "content": entry.content[:500],
                    "tool_names": entry.tool_names,
                    "uuid": entry.uuid,
                }
            )
            pipe.rpush(entries_key, compact)

        # Trim to max entries
        if _MAX_ENTRIES > 0:
            pipe.ltrim(entries_key, -_MAX_ENTRIES, -1)

        pipe.execute()

    def _redis_get_meta(self, r, session_id: str) -> Optional[SessionMeta]:
        data = r.hgetall(_rkey(f"session:{session_id}:meta"))
        if not data:
            return None
        return SessionMeta.from_dict(data)

    def _redis_get_entries(self, r, session_id: str, offset: int, limit: int) -> List[SessionEntry]:
        raw = r.lrange(_rkey(f"session:{session_id}:entries"), offset, offset + limit - 1)
        entries = []
        for item in raw:
            try:
                data = json.loads(item)
                entries.append(SessionEntry.from_dict(data))
            except (json.JSONDecodeError, TypeError):
                continue
        return entries

    def _redis_list_sessions(self, r, project, limit, offset, since_hours) -> List[SessionMeta]:
        if project:
            # Find matching projects from the deterministic project index SET
            all_projects = r.smembers(_rkey("idx:projects"))
            project_lower = project.lower()
            matching_projects = [
                p for p in all_projects if project_lower in p.lower() or project_lower in decode_project_path(p).lower()
            ]

            matching_ids = set()
            for proj_encoded in matching_projects:
                min_score = "-inf"
                if since_hours:
                    min_score = str(time() - since_hours * 3600)
                ids = r.zrevrangebyscore(_rkey(f"idx:project:{proj_encoded}"), "+inf", min_score)
                matching_ids.update(ids)

            # Sort by fetching scores from idx:all
            scored = []
            for sid in matching_ids:
                score = r.zscore(_rkey("idx:all"), sid)
                if score is not None:
                    scored.append((sid, score))
            scored.sort(key=lambda x: x[1], reverse=True)
            session_ids = [s[0] for s in scored[offset : offset + limit]]
        else:
            min_score = "-inf"
            if since_hours:
                min_score = str(time() - since_hours * 3600)
            session_ids = r.zrevrangebyscore(
                _rkey("idx:all"),
                "+inf",
                min_score,
                start=offset,
                num=limit,
            )

        results = []
        for sid in session_ids:
            meta = self._redis_get_meta(r, sid)
            if meta:
                results.append(meta)
        return results

    def _redis_search(self, r, query: str, project: Optional[str], limit: int) -> List[dict]:
        query_lower = query.lower()

        # Get candidate sessions (most recent 200)
        session_ids = r.zrevrange(_rkey("idx:all"), 0, 199)

        results = []
        for sid in session_ids:
            if project:
                meta = r.hgetall(_rkey(f"session:{sid}:meta"))
                if not meta:
                    continue
                proj = meta.get("project_encoded", "") + meta.get("project_path", "")
                if project.lower() not in proj.lower():
                    continue

            # Search through entries
            entries_raw = r.lrange(_rkey(f"session:{sid}:entries"), 0, -1)
            for item in entries_raw:
                try:
                    data = json.loads(item)
                except json.JSONDecodeError:
                    continue

                content = data.get("content", "")
                if query_lower in content.lower():
                    meta = r.hgetall(_rkey(f"session:{sid}:meta"))
                    results.append(
                        {
                            "session_id": sid,
                            "project_path": meta.get("project_path", "") if meta else "",
                            "entry_type": data.get("entry_type", ""),
                            "content_preview": content[:300],
                            "timestamp": data.get("timestamp", ""),
                        }
                    )
                    if len(results) >= limit:
                        return results
                    break  # One match per session is enough

        return results

    # ------------------------------------------------------------------
    # File fallback implementation
    # ------------------------------------------------------------------

    def _file_get_meta(self, session_id: str) -> Optional[SessionMeta]:
        """Find and parse metadata for a session from filesystem."""
        for sid, project_encoded, filepath in scan_projects_dir(self._projects_dir):
            if sid == session_id:
                return parse_transcript_meta(filepath, project_encoded)
        return None

    def _file_get_entries(self, session_id: str, offset: int, limit: int) -> List[SessionEntry]:
        """Read entries from raw JSONL file."""
        for sid, project_encoded, filepath in scan_projects_dir(self._projects_dir):
            if sid == session_id:
                entries, _ = parse_transcript_entries(filepath)
                # Filter to user/assistant text (skip tool_result already done in parser)
                return entries[offset : offset + limit]
        return []

    def _file_list_sessions(self, project, limit, offset, since_hours) -> List[SessionMeta]:
        """List sessions from filesystem (sorted by file mtime)."""
        all_files = scan_projects_dir(self._projects_dir)

        if project:
            project_lower = project.lower()
            all_files = [
                (sid, pe, fp)
                for sid, pe, fp in all_files
                if project_lower in pe.lower() or project_lower in decode_project_path(pe).lower()
            ]

        # Sort by file modification time (newest first)
        all_files.sort(key=lambda x: x[2].stat().st_mtime, reverse=True)

        if since_hours:
            cutoff = time() - since_hours * 3600
            all_files = [(s, p, f) for s, p, f in all_files if f.stat().st_mtime > cutoff]

        sliced = all_files[offset : offset + limit]
        results = []
        for sid, pe, fp in sliced:
            meta = parse_transcript_meta(fp, pe)
            if meta:
                results.append(meta)
        return results

    def _file_search(self, query: str, project: Optional[str], limit: int) -> List[dict]:
        """Keyword search across raw JSONL files (slow but functional)."""
        query_lower = query.lower()
        all_files = scan_projects_dir(self._projects_dir)

        if project:
            project_lower = project.lower()
            all_files = [
                (sid, pe, fp)
                for sid, pe, fp in all_files
                if project_lower in pe.lower() or project_lower in decode_project_path(pe).lower()
            ]

        # Sort by mtime (newest first)
        all_files.sort(key=lambda x: x[2].stat().st_mtime, reverse=True)

        results = []
        for sid, pe, fp in all_files:
            entries, _ = parse_transcript_entries(fp)
            for entry in entries:
                if query_lower in entry.content.lower():
                    results.append(
                        {
                            "session_id": sid,
                            "project_path": decode_project_path(pe),
                            "entry_type": entry.entry_type,
                            "content_preview": entry.content[:300],
                            "timestamp": entry.timestamp,
                        }
                    )
                    if len(results) >= limit:
                        return results
                    break  # One match per session

        return results

    # ------------------------------------------------------------------
    # File-based position tracking
    # ------------------------------------------------------------------

    _POS_DIR = Path(
        os.getenv(
            "AGENTIBRIDGE_POSITIONS_DIR",
            str(Path.home() / ".cache" / "agentibridge" / "positions"),
        )
    )

    def _file_get_position(self, filepath: str) -> int:
        pos_file = self._POS_DIR / f"{_filepath_hash(filepath)}.pos"
        if pos_file.exists():
            try:
                return int(pos_file.read_text().strip())
            except (ValueError, OSError):
                pass
        return 0

    def _file_save_position(self, filepath: str, position: int) -> None:
        try:
            self._POS_DIR.mkdir(parents=True, exist_ok=True)
            pos_file = self._POS_DIR / f"{_filepath_hash(filepath)}.pos"
            pos_file.write_text(str(position))
        except OSError:
            pass

    # ==================================================================
    # PHASE 5 — KNOWLEDGE CATALOG (Memory, Plans, History, Codename)
    # ==================================================================

    # ------------------------------------------------------------------
    # Memory files
    # ------------------------------------------------------------------

    def upsert_memory_file(self, mem: MemoryFile) -> None:
        """Create or update a memory file record."""
        r = self._get_redis()
        if r is None:
            return
        key = _rkey(f"memory:{mem.project_encoded}:{hashlib.md5(mem.filename.encode()).hexdigest()[:12]}")
        mapping = {
            "project_encoded": mem.project_encoded,
            "project_path": mem.project_path,
            "filename": mem.filename,
            "filepath": mem.filepath,
            "content": mem.content,
            "file_size_bytes": str(mem.file_size_bytes),
            "last_modified": mem.last_modified,
        }
        pipe = r.pipeline()
        pipe.hset(key, mapping=mapping)
        # Index by mtime
        try:
            from datetime import datetime

            score = datetime.fromisoformat(mem.last_modified).timestamp()
        except (ValueError, AttributeError):
            score = 0.0
        pipe.zadd(_rkey("idx:memory:all"), {key: score})
        pipe.execute()

    def list_memory_files(self, project: Optional[str] = None) -> List[MemoryFile]:
        """List all indexed memory files, optionally filtered by project."""
        r = self._get_redis()
        if r is not None:
            return self._redis_list_memory(r, project)
        return self._file_list_memory(project)

    def get_memory_file(self, project_encoded: str, filename: str) -> Optional[MemoryFile]:
        """Get a specific memory file by project and filename."""
        r = self._get_redis()
        if r is not None:
            key = _rkey(f"memory:{project_encoded}:{hashlib.md5(filename.encode()).hexdigest()[:12]}")
            data = r.hgetall(key)
            if not data:
                return None
            return MemoryFile.from_dict(data)
        return self._file_get_memory(project_encoded, filename)

    def _redis_list_memory(self, r, project: Optional[str]) -> List[MemoryFile]:
        keys = r.zrevrange(_rkey("idx:memory:all"), 0, -1)
        results = []
        project_lower = project.lower() if project else None
        for key in keys:
            data = r.hgetall(key)
            if not data:
                continue
            if project_lower:
                pe = data.get("project_encoded", "") + data.get("project_path", "")
                if project_lower not in pe.lower():
                    continue
            results.append(MemoryFile.from_dict(data))
        return results

    def _file_list_memory(self, project: Optional[str]) -> List[MemoryFile]:
        from agentibridge.config import AGENTIBRIDGE_MAX_MEMORY_CONTENT

        all_files = scan_memory_files(self._projects_dir, max_content=AGENTIBRIDGE_MAX_MEMORY_CONTENT)
        if project:
            project_lower = project.lower()
            all_files = [
                m
                for m in all_files
                if project_lower in m.project_encoded.lower() or project_lower in m.project_path.lower()
            ]
        return all_files

    def _file_get_memory(self, project_encoded: str, filename: str) -> Optional[MemoryFile]:
        from agentibridge.config import AGENTIBRIDGE_MAX_MEMORY_CONTENT

        memory_path = self._projects_dir / project_encoded / "memory" / filename
        if not memory_path.is_file():
            return None
        try:
            stat = memory_path.stat()
            content = memory_path.read_text(encoding="utf-8")[:AGENTIBRIDGE_MAX_MEMORY_CONTENT]
            from datetime import datetime, timezone

            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
            return MemoryFile(
                project_encoded=project_encoded,
                project_path=decode_project_path(project_encoded),
                filename=filename,
                filepath=str(memory_path),
                content=content,
                file_size_bytes=stat.st_size,
                last_modified=mtime,
            )
        except (OSError, UnicodeDecodeError):
            return None

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def add_history_entries(self, entries: List[HistoryEntry]) -> None:
        """Append history entries to the store."""
        if not entries:
            return
        r = self._get_redis()
        if r is None:
            return
        max_history = int(os.getenv("AGENTIBRIDGE_MAX_HISTORY_ENTRIES", "5000"))
        pipe = r.pipeline()
        key = _rkey("history:entries")
        for entry in entries:
            pipe.rpush(key, json.dumps(entry.to_dict()))
        if max_history > 0:
            pipe.ltrim(key, -max_history, -1)
        pipe.execute()

    def search_history(
        self,
        query: str = "",
        project: Optional[str] = None,
        session_id: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
        since: str = "",
    ) -> Tuple[List[HistoryEntry], int]:
        """Search/filter history entries."""
        r = self._get_redis()
        if r is not None:
            return self._redis_search_history(r, query, project, session_id, limit, offset, since)
        return self._file_search_history(query, project, session_id, limit, offset, since)

    def _redis_search_history(self, r, query, project, session_id, limit, offset, since):
        raw = r.lrange(_rkey("history:entries"), 0, -1)
        return self._filter_history(raw, query, project, session_id, limit, offset, since, from_redis=True)

    def _file_search_history(self, query, project, session_id, limit, offset, since):
        entries, _ = parse_history(self._history_file)
        raw = [json.dumps(e.to_dict()) for e in entries]
        return self._filter_history(raw, query, project, session_id, limit, offset, since, from_redis=True)

    def _filter_history(self, raw_items, query, project, session_id, limit, offset, since, from_redis=False):
        query_lower = query.lower() if query else ""
        project_lower = project.lower() if project else ""

        filtered = []
        for item in raw_items:
            try:
                data = json.loads(item) if isinstance(item, str) else item
            except json.JSONDecodeError:
                continue

            if query_lower and query_lower not in data.get("display", "").lower():
                continue
            if project_lower and project_lower not in data.get("project", "").lower():
                continue
            if session_id and data.get("session_id") != session_id:
                continue
            if since and data.get("timestamp", "") < since:
                continue

            filtered.append(HistoryEntry.from_dict(data))

        total = len(filtered)
        return filtered[offset : offset + limit], total
