"""Background transcript collector.

Scans ~/.claude/projects/ for JSONL transcript files and incrementally
indexes new entries into the SessionStore. Runs as a daemon thread
inside the MCP server process.

Phase 5 additions: memory files and history scanning.
"""

import os
import sys
import threading
from pathlib import Path
from time import time
from typing import Optional

from agentibridge.logging import log
from agentibridge.parser import (
    parse_transcript_entries,
    parse_transcript_meta,
    scan_projects_dir,
)
from agentibridge.store import SessionStore


class SessionCollector:
    """Incremental transcript collector with background polling."""

    def __init__(self, store: SessionStore, embedder=None) -> None:
        self._store = store
        self._embedder = embedder
        self._interval = int(os.getenv("AGENTIBRIDGE_POLL_INTERVAL", "60"))
        _home = Path(os.getenv("CLAUDE_CODE_HOME_DIR", str(Path.home() / ".claude")))
        self._projects_dir = _home / "projects"
        self._history_file = _home / "history.jsonl"
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start background polling thread."""
        if self._thread is not None and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="session-collector",
            daemon=True,
        )
        self._thread.start()
        print(f"Collector started (interval={self._interval}s)", file=sys.stderr)

    def stop(self) -> None:
        """Signal the background thread to stop."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def collect_once(self) -> dict:
        """Run a single collection cycle. Returns stats."""
        start = time()
        files_scanned = 0
        sessions_updated = 0
        entries_added = 0
        updated_session_ids: list[str] = []

        try:
            all_files = scan_projects_dir(self._projects_dir)
            files_scanned = len(all_files)

            for session_id, project_encoded, filepath in all_files:
                try:
                    result = self._scan_file(session_id, project_encoded, filepath)
                    if result["updated"]:
                        sessions_updated += 1
                        entries_added += result["entries_added"]
                        updated_session_ids.append(session_id)
                except Exception as e:
                    log(
                        "Collector: file scan error",
                        {
                            "file": str(filepath),
                            "error": str(e),
                        },
                    )
                    continue

        except Exception as e:
            log("Collector: scan error", {"error": str(e)})

        # Phase 5: scan memory files and history
        memory_count = self._scan_memory_files()
        history_count = self._scan_history()

        # Phase 2: embed updated sessions + backfill un-embedded
        embedded_count = 0
        if self._embedder:
            if updated_session_ids:
                embedded_count = self._embed_sessions(updated_session_ids)
            embedded_count += self._backfill_embeddings(
                exclude=set(updated_session_ids),
            )

        duration_ms = int((time() - start) * 1000)

        return {
            "files_scanned": files_scanned,
            "sessions_updated": sessions_updated,
            "entries_added": entries_added,
            "memory_files_indexed": memory_count,
            "history_entries_added": history_count,
            "sessions_embedded": embedded_count,
            "duration_ms": duration_ms,
        }

    def _poll_loop(self) -> None:
        """Background thread: collect on interval."""
        # Initial collection
        try:
            stats = self.collect_once()
            embedded = stats.get("sessions_embedded", 0)
            embed_msg = f", {embedded} embedded" if embedded else ""
            print(
                f"Initial collection: {stats['files_scanned']} files, "
                f"{stats['sessions_updated']} updated, "
                f"{stats['entries_added']} entries{embed_msg}, "
                f"{stats['duration_ms']}ms",
                file=sys.stderr,
            )
        except Exception as e:
            print(f"Initial collection failed: {e}", file=sys.stderr)

        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._interval)
            if self._stop_event.is_set():
                break
            try:
                self.collect_once()
            except Exception as e:
                log("Collector: poll error", {"error": str(e)})

    def _scan_file(self, session_id: str, project_encoded: str, filepath: Path) -> dict:
        """Incrementally index one transcript file."""
        file_size = filepath.stat().st_size
        stored_offset = self._store.get_file_position(str(filepath))

        if file_size <= stored_offset:
            return {"updated": False, "entries_added": 0}

        # Parse new entries from offset
        entries, new_offset = parse_transcript_entries(filepath, offset=stored_offset)

        if not entries and new_offset <= stored_offset:
            return {"updated": False, "entries_added": 0}

        # Update session metadata — always pass entries to avoid re-reading the file
        meta = parse_transcript_meta(filepath, project_encoded, entries)
        if meta:
            self._store.upsert_session(meta)

        # Store new entries
        if entries:
            self._store.add_entries(session_id, entries)

        # Save position
        self._store.save_file_position(str(filepath), new_offset)

        return {"updated": True, "entries_added": len(entries)}

    # ------------------------------------------------------------------
    # Phase 2: Embedding pass
    # ------------------------------------------------------------------

    _BACKFILL_BATCH = 10  # max sessions to backfill per cycle

    def _embed_sessions(self, session_ids: list[str]) -> int:
        """Embed recently-updated sessions. Returns count of sessions embedded."""
        if not self._embedder.is_available():
            return 0
        count = 0
        for sid in session_ids:
            try:
                chunks = self._embedder.embed_session(sid)
                if chunks > 0:
                    count += 1
            except Exception as e:
                log("Collector: embedding error", {"session_id": sid, "error": str(e)})
                continue
        return count

    def _backfill_embeddings(self, exclude: set[str] | None = None) -> int:
        """Embed sessions in Redis that have no chunks in Postgres yet.

        Processes up to _BACKFILL_BATCH per cycle to avoid overwhelming the
        LLM API. Returns count of sessions embedded.
        """
        if not self._embedder.is_available():
            return 0

        try:
            # Get all session IDs from Redis
            all_ids = self._store.list_session_ids()
            if not all_ids:
                return 0

            # Get session IDs that already have embeddings
            embedded_ids = self._get_embedded_session_ids()

            # Find un-embedded sessions, excluding ones just processed
            exclude = exclude or set()
            candidates = [sid for sid in all_ids if sid not in embedded_ids and sid not in exclude]
            if not candidates:
                return 0

            batch = candidates[: self._BACKFILL_BATCH]
            return self._embed_sessions(batch)
        except Exception as e:
            log("Collector: backfill error", {"error": str(e)})
            return 0

    def _get_embedded_session_ids(self) -> set[str]:
        """Return set of session IDs that already have chunks in Postgres."""
        try:
            from agentibridge.pg_client import get_pg

            pool = get_pg()
            if pool is None:
                return set()
            with pool.connection() as conn:
                rows = conn.execute("SELECT DISTINCT session_id FROM transcript_chunks").fetchall()
                return {row[0] for row in rows}
        except Exception:
            return set()

    # ------------------------------------------------------------------
    # Phase 5: Knowledge catalog scan passes
    # ------------------------------------------------------------------

    def _scan_memory_files(self) -> int:
        """Scan and index memory files from all projects."""
        try:
            from agentibridge.catalog import scan_memory_files
            from agentibridge.config import AGENTIBRIDGE_MAX_MEMORY_CONTENT

            files = scan_memory_files(self._projects_dir, max_content=AGENTIBRIDGE_MAX_MEMORY_CONTENT)
            for mem in files:
                self._store.upsert_memory_file(mem)
            return len(files)
        except Exception as e:
            log("Collector: memory scan error", {"error": str(e)})
            return 0

    def _scan_history(self) -> int:
        """Incrementally scan and index history.jsonl."""
        try:
            from agentibridge.catalog import parse_history

            pos_key = str(self._history_file)
            offset = self._store.get_file_position(pos_key)
            entries, new_offset = parse_history(self._history_file, offset=offset)

            if entries:
                self._store.add_history_entries(entries)
                self._store.save_file_position(pos_key, new_offset)

            return len(entries)
        except Exception as e:
            log("Collector: history scan error", {"error": str(e)})
            return 0
