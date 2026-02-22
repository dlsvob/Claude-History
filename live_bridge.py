"""Live bridge that reads directly from ~/.claude/ JSONL files.

No ingest or SQLite required. Session metadata is cached in memory with
mtime-based invalidation so the browser always reflects the latest state.
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from pathlib import Path

from claude_archive.ingest.parser import parse_session
from claude_archive.ingest.scanner import scan_claude_dir, ScannedSession

from bridge import (
    ProjectInfo,
    SessionInfo,
    _messages_to_turns,
    _project_display,
    _INTERACTIVE_TOOLS,
)


# Files larger than this are scanned in a background thread to avoid
# blocking page loads.  The session still appears in the listing with
# placeholder metadata until the background scan finishes.
_LARGE_FILE_THRESHOLD = 50 * 1024 * 1024  # 50 MB


class LiveBridge:
    """Read-only bridge serving directly from ~/.claude/ JSONL files."""

    def __init__(self, claude_dir: Path | str):
        self.claude_dir = Path(claude_dir)
        # Cache: session_id -> (file_mtime, SessionInfo)
        self._meta_cache: dict[str, tuple[float, SessionInfo]] = {}
        # Cache: session_id -> ScannedSession (for full parsing later)
        self._scanned_cache: dict[str, ScannedSession] = {}
        self._lock = threading.Lock()
        self._bg_pending: set[str] = set()  # session IDs being parsed in bg

    def close(self):
        pass

    # --- Public API (same shape as ArchiveBridge) ---

    def list_hosts(self) -> list[dict]:
        return [{
            "host_id": "local",
            "hostname": "local",
            "session_count": len(self._meta_cache),
            "project_count": len({
                m.project_name for _, m in self._meta_cache.values()
            }),
            "last_ingested": "",
        }]

    def list_projects(self, host_id: str | None = None) -> list[ProjectInfo]:
        self._refresh_metadata()

        projects_map: dict[str, ProjectInfo] = {}
        with self._lock:
            for _, meta in self._meta_cache.values():
                key = meta.project_name
                if key not in projects_map:
                    sc = self._scanned_cache.get(meta.session_id)
                    display = _project_display(sc.project_path) if sc else key
                    projects_map[key] = ProjectInfo(
                        name=key,
                        display_name=display,
                        host_id="local",
                    )
                proj = projects_map[key]
                proj.session_count += 1
                proj.sessions.append(meta)

        for proj in projects_map.values():
            proj.sessions.sort(
                key=lambda s: s.last_timestamp or s.first_timestamp,
                reverse=True,
            )

        return sorted(
            projects_map.values(),
            key=lambda p: p.sessions[0].last_timestamp if p.sessions else "",
            reverse=True,
        )

    def get_session_meta(self, session_id: str) -> SessionInfo | None:
        self._refresh_metadata()

        with self._lock:
            entry = self._meta_cache.get(session_id)
            if entry:
                return entry[1]
            # Prefix match
            matches = [
                v[1] for sid, v in self._meta_cache.items()
                if sid.startswith(session_id)
            ]
            return matches[0] if len(matches) == 1 else None

    def get_conversation_turns(
        self, session_id: str, offset: int = 0, limit: int = 0,
    ) -> list[dict]:
        # Resolve prefix
        with self._lock:
            sc = self._scanned_cache.get(session_id)
            if sc is None:
                matches = [
                    v for sid, v in self._scanned_cache.items()
                    if sid.startswith(session_id)
                ]
                sc = matches[0] if len(matches) == 1 else None
        if sc is None:
            return []

        session = parse_session(
            session_id=sc.session_id,
            host_id="local",
            project_path=sc.project_path,
            project_encoded=sc.project_encoded,
            jsonl_path=sc.jsonl_path,
            source_file_size=sc.file_size,
            source_file_mtime=sc.file_mtime,
            subagent_files=sc.subagent_files,
        )

        # Build tool_result and tool_id_to_info lookups (same as ArchiveBridge)
        tool_results: dict[str, dict] = {}
        tool_id_to_info: dict[str, dict] = {}
        for msg in session.messages:
            if msg.message_type == "user":
                for block in msg.content_blocks:
                    if block.block_type == "tool_result" and block.tool_id:
                        tool_results[block.tool_id] = {
                            "text": block.text,
                            "is_error": block.is_error,
                        }
            elif msg.message_type == "assistant":
                for block in msg.content_blocks:
                    if block.block_type == "tool_use" and block.tool_id:
                        tool_id_to_info[block.tool_id] = {
                            "tool_name": block.tool_name,
                            "tool_input": block.tool_input,
                        }

        turns = _messages_to_turns(session.messages, tool_results, tool_id_to_info)

        if offset:
            turns = turns[offset:]
        if limit:
            turns = turns[:limit]
        return turns

    def search(
        self,
        query: str,
        host_id: str | None = None,
        project: str | None = None,
        limit: int = 50,
        fuzzy: bool = False,
        message_type: str | None = None,
    ) -> list[dict]:
        """Basic text search through JSONL files."""
        if not query:
            return []

        self._refresh_metadata()
        query_lower = query.lower()
        results: list[dict] = []

        with self._lock:
            scanned_items = list(self._scanned_cache.values())

        for sc in scanned_items:
            if project and sc.project_encoded != project:
                continue
            try:
                with open(sc.jsonl_path) as f:
                    for line in f:
                        if query_lower not in line.lower():
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        msg_type = obj.get("type")
                        if msg_type not in ("user", "assistant"):
                            continue
                        if message_type and msg_type != message_type:
                            continue

                        # Extract text from the message for snippet
                        msg_data = obj.get("message", {})
                        content = msg_data.get("content", "")
                        text = content if isinstance(content, str) else ""
                        if not text and isinstance(content, list):
                            for item in content:
                                if isinstance(item, dict):
                                    if item.get("type") == "text":
                                        text = item.get("text", "")
                                        if text:
                                            break
                                    elif item.get("type") == "tool_result":
                                        cv = item.get("content", "")
                                        if isinstance(cv, str) and query_lower in cv.lower():
                                            text = cv
                                            break
                                    elif item.get("type") == "tool_use":
                                        inp = json.dumps(item.get("input", {}))
                                        if query_lower in inp.lower():
                                            text = inp
                                            break
                        # Fallback: search the raw line for a snippet
                        if not text or query_lower not in text.lower():
                            text = line

                        # Build snippet around match
                        idx = text.lower().find(query_lower)
                        if idx >= 0:
                            start = max(0, idx - 60)
                            end = min(len(text), idx + len(query) + 60)
                            snippet = ("..." if start > 0 else "") + text[start:end] + ("..." if end < len(text) else "")
                        else:
                            snippet = text[:150]

                        with self._lock:
                            meta = self._meta_cache.get(sc.session_id)
                        display = _project_display(sc.project_path)

                        results.append({
                            "project_display": display,
                            "session_id": sc.session_id,
                            "msg_type": msg_type,
                            "timestamp": obj.get("timestamp", ""),
                            "context": snippet,
                            "uuid": obj.get("uuid", ""),
                            "model": msg_data.get("model", ""),
                            "host_id": "local",
                            "score": 1.0,
                        })
                        if len(results) >= limit:
                            return results
            except OSError:
                continue
            if len(results) >= limit:
                break

        return results

    def get_stats(
        self,
        host_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        group_by: str | None = None,
    ) -> dict:
        self._refresh_metadata()

        with self._lock:
            metas = [m for _, m in self._meta_cache.values()]

        if since:
            metas = [m for m in metas if (m.last_timestamp or "") >= since]
        if until:
            metas = [m for m in metas if (m.first_timestamp or "") <= until]

        total_input = sum(m.total_input_tokens for m in metas)
        total_output = sum(m.total_output_tokens for m in metas)
        projects = {m.project_name for m in metas}
        models = Counter()
        for m in metas:
            for model in m.models_used:
                models[model] += 1

        return {
            "session_count": len(metas),
            "project_count": len(projects),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "models": dict(models.most_common()),
        }

    # --- Internal ---

    def _refresh_metadata(self):
        """Scan ~/.claude/ and update metadata cache for new/changed files."""
        scanned = scan_claude_dir(self.claude_dir)

        with self._lock:
            # Detect removed sessions
            current_ids = {s.session_id for s in scanned}
            removed = set(self._scanned_cache.keys()) - current_ids
            for sid in removed:
                self._scanned_cache.pop(sid, None)
                self._meta_cache.pop(sid, None)

            # Update scanned cache and find sessions needing re-parse
            to_parse: list[ScannedSession] = []
            to_parse_bg: list[ScannedSession] = []
            for sc in scanned:
                self._scanned_cache[sc.session_id] = sc
                cached = self._meta_cache.get(sc.session_id)
                if cached is None or cached[0] != sc.file_mtime:
                    if sc.file_size > _LARGE_FILE_THRESHOLD and sc.session_id not in self._bg_pending:
                        to_parse_bg.append(sc)
                    else:
                        to_parse.append(sc)

        # Parse small/medium files synchronously
        for sc in to_parse:
            meta = _extract_metadata_fast(sc)
            with self._lock:
                self._meta_cache[sc.session_id] = (sc.file_mtime, meta)

        # Defer large files to background thread
        for sc in to_parse_bg:
            self._parse_in_background(sc)

    def _parse_in_background(self, sc: ScannedSession):
        """Extract metadata for a large file in a background thread.

        Inserts a placeholder immediately so the session appears in listings.
        """
        with self._lock:
            self._bg_pending.add(sc.session_id)
            # Insert placeholder so session shows up immediately
            if sc.session_id not in self._meta_cache:
                self._meta_cache[sc.session_id] = (sc.file_mtime, SessionInfo(
                    session_id=sc.session_id,
                    project_name=sc.project_encoded,
                    first_user_message="(loading...)",
                    host_id="local",
                ))

        def _do_parse():
            meta = _extract_metadata_fast(sc)
            with self._lock:
                self._meta_cache[sc.session_id] = (sc.file_mtime, meta)
                self._bg_pending.discard(sc.session_id)

        threading.Thread(target=_do_parse, daemon=True).start()


def _extract_metadata_fast(sc: ScannedSession) -> SessionInfo:
    """Lightweight metadata extraction — streams JSONL without building
    full Message objects.  Much faster than parse_session for large files.
    """
    first_ts = ""
    last_ts = ""
    first_user_msg = ""
    models: set[str] = set()
    user_count = 0
    assistant_count = 0
    total_input = 0
    total_output = 0

    try:
        with open(sc.jsonl_path) as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts = obj.get("timestamp", "")
                if ts:
                    if not first_ts:
                        first_ts = ts
                    last_ts = ts

                msg_type = obj.get("type")
                if msg_type == "user":
                    user_count += 1
                    if not first_user_msg:
                        msg = obj.get("message", {})
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            text = content.strip()
                        elif isinstance(content, list):
                            text = ""
                            for item in content:
                                if isinstance(item, dict) and item.get("type") == "text":
                                    text = item.get("text", "").strip()
                                    break
                        else:
                            text = ""
                        if text and not text.startswith("<") and not text.startswith("[Request interrupted"):
                            first_user_msg = text[:120]

                elif msg_type == "assistant":
                    assistant_count += 1
                    msg = obj.get("message", {})
                    model = msg.get("model", "")
                    if model:
                        models.add(model)
                    usage = msg.get("usage", {})
                    total_input += usage.get("input_tokens", 0)
                    total_output += usage.get("output_tokens", 0)
    except OSError:
        pass

    return SessionInfo(
        session_id=sc.session_id,
        project_name=sc.project_encoded,
        first_timestamp=first_ts,
        last_timestamp=last_ts,
        message_count=user_count + assistant_count,
        first_user_message=first_user_msg,
        slug="",
        model=", ".join(sorted(models)[:2]) if models else "",
        models_used=sorted(models),
        subagent_count=len(sc.subagent_files),
        total_input_tokens=total_input,
        total_output_tokens=total_output,
        host_id="local",
    )
