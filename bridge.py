"""Bridge between claude_archive storage backends and the history browser UI.

Converts archive Message objects into the turn-based format that templates expect.
Wraps StorageManager to provide project listings, session turns, search, and stats.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from claude_archive.config import Config, load_config
from claude_archive.models import Message, Session
from claude_archive.storage.manager import StorageManager


@dataclass
class ProjectInfo:
    """Project with sessions for sidebar display."""
    name: str  # project_encoded (used in URLs)
    display_name: str
    host_id: str
    session_count: int = 0
    sessions: list[SessionInfo] = field(default_factory=list)


@dataclass
class SessionInfo:
    """Session metadata for listing."""
    session_id: str
    project_name: str  # project_encoded
    first_timestamp: str = ""
    last_timestamp: str = ""
    message_count: int = 0
    first_user_message: str = ""
    slug: str = ""
    model: str = ""
    models_used: list[str] = field(default_factory=list)
    subagent_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    host_id: str = ""


class ArchiveBridge:
    """Provides the same data interface as parser.py but backed by claude_archive."""

    def __init__(self, config: Config | None = None):
        self.config = config or load_config()
        self.manager = StorageManager(self.config)
        self.manager.initialize()

    def close(self):
        self.manager.close()

    def list_hosts(self) -> list[dict]:
        """List all archived hosts."""
        hosts = self.manager.get_hosts()
        return [
            {
                "host_id": h.host_id,
                "hostname": h.hostname,
                "session_count": h.session_count,
                "project_count": h.project_count,
                "last_ingested": h.last_ingested,
            }
            for h in hosts
        ]

    def list_projects(self, host_id: str | None = None) -> list[ProjectInfo]:
        """List all projects with their sessions, for the sidebar."""
        sessions = self.manager.get_sessions(host_id=host_id, limit=5000)

        # Group sessions by project
        projects_map: dict[str, ProjectInfo] = {}
        for s in sessions:
            key = s.project_encoded
            if key not in projects_map:
                projects_map[key] = ProjectInfo(
                    name=s.project_encoded,
                    display_name=_project_display(s.project_path),
                    host_id=s.host_id,
                )
            proj = projects_map[key]
            proj.session_count += 1
            proj.sessions.append(SessionInfo(
                session_id=s.session_id,
                project_name=s.project_encoded,
                first_timestamp=s.first_timestamp,
                last_timestamp=s.last_timestamp,
                message_count=s.message_count,
                first_user_message=s.first_user_message,
                slug=s.slug,
                model=", ".join(s.models_used[:2]) if s.models_used else "",
                models_used=s.models_used,
                subagent_count=s.subagent_count,
                total_input_tokens=s.total_input_tokens,
                total_output_tokens=s.total_output_tokens,
                host_id=s.host_id,
            ))

        # Sort sessions within each project by last_timestamp descending
        for proj in projects_map.values():
            proj.sessions.sort(
                key=lambda s: s.last_timestamp or s.first_timestamp,
                reverse=True,
            )

        # Sort projects by most recent session
        result = sorted(
            projects_map.values(),
            key=lambda p: p.sessions[0].last_timestamp if p.sessions else "",
            reverse=True,
        )
        return result

    def get_session_meta(self, session_id: str) -> SessionInfo | None:
        """Get session metadata."""
        s = self.manager.get_session(session_id)
        if s is None:
            # Try prefix match
            sessions = self.manager.get_sessions(limit=5000)
            matches = [sess for sess in sessions if sess.session_id.startswith(session_id)]
            if len(matches) == 1:
                s = matches[0]
            else:
                return None

        return SessionInfo(
            session_id=s.session_id,
            project_name=s.project_encoded,
            first_timestamp=s.first_timestamp,
            last_timestamp=s.last_timestamp,
            message_count=s.message_count,
            first_user_message=s.first_user_message,
            slug=s.slug,
            model=", ".join(s.models_used[:2]) if s.models_used else "",
            models_used=s.models_used,
            subagent_count=s.subagent_count,
            total_input_tokens=s.total_input_tokens,
            total_output_tokens=s.total_output_tokens,
            host_id=s.host_id,
        )

    def get_conversation_turns(
        self, session_id: str, offset: int = 0, limit: int = 0
    ) -> list[dict]:
        """Get messages grouped into conversation turns for display.

        Returns turn dicts compatible with the existing templates:
        - User turns: {type, text, timestamp, uuid}
        - Assistant turns: {type, text_blocks, thinking_blocks, tool_calls, timestamp, uuid, model, usage}
        """
        messages = self.manager.get_messages(session_id)

        # Build tool_result lookup from user messages
        tool_results: dict[str, dict] = {}
        for msg in messages:
            if msg.message_type == "user":
                for block in msg.content_blocks:
                    if block.block_type == "tool_result" and block.tool_id:
                        tool_results[block.tool_id] = {
                            "text": block.text,
                            "is_error": block.is_error,
                        }

        turns = _messages_to_turns(messages, tool_results)

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
    ) -> list[dict]:
        """Full-text search via Tantivy (with SQLite FTS5 fallback)."""
        results = self.manager.search(
            query, host_id=host_id, project=project, limit=limit, fuzzy=fuzzy,
        )
        # Map to the format templates expect
        return [
            {
                "project": r.get("session_id", "")[:8],  # not used directly
                "project_display": r.get("project_display", r.get("project_path", "")),
                "session_id": r.get("session_id", ""),
                "msg_type": r.get("message_type", ""),
                "timestamp": r.get("timestamp", ""),
                "context": r.get("snippet", ""),
                "uuid": r.get("uuid", ""),
                "model": r.get("model", ""),
                "host_id": r.get("host_id", ""),
                "score": r.get("score", 0.0),
            }
            for r in results
        ]

    def get_stats(
        self,
        host_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        group_by: str | None = None,
    ) -> dict:
        """Get usage analytics from the archive."""
        return self.manager.get_stats(
            host_id=host_id, since=since, until=until, group_by=group_by,
        )


def _messages_to_turns(messages: list[Message], tool_results: dict) -> list[dict]:
    """Convert messages into conversation turns grouped as exchanges.

    Groups all assistant messages between two user messages into a single
    assistant turn. Each turn has:
    - text_blocks: all text the agent said
    - thinking_blocks: all thinking blocks
    - tool_calls: all tool invocations with their results
    """
    turns = []
    current_assistant = None

    def flush_assistant():
        nonlocal current_assistant
        if current_assistant is None:
            return
        has_text = bool(current_assistant["text_blocks"])
        has_tools = bool(current_assistant["tool_calls"])
        has_thinking = bool(current_assistant["thinking_blocks"])
        if has_text or has_tools or has_thinking:
            turns.append(current_assistant)
        current_assistant = None

    def ensure_assistant(timestamp, uuid, model, usage):
        nonlocal current_assistant
        if current_assistant is None:
            current_assistant = {
                "type": "assistant",
                "text_blocks": [],
                "thinking_blocks": [],
                "tool_calls": [],
                "timestamp": timestamp,
                "uuid": uuid,
                "model": model or "",
                "usage": {
                    "input_tokens": usage.input_tokens if usage else 0,
                    "output_tokens": usage.output_tokens if usage else 0,
                },
            }

    for msg in messages:
        if msg.message_type == "user":
            # Extract user text
            user_text = msg.raw_text
            if not user_text:
                text_parts = [b.text for b in msg.content_blocks
                             if b.block_type == "text" and b.text.strip()]
                if text_parts:
                    user_text = "\n".join(text_parts)

            # Filter out system messages and tool-result-only messages
            if (user_text and not user_text.startswith("[Request interrupted by user")
                    and not user_text.startswith("<")):
                flush_assistant()
                turns.append({
                    "type": "user",
                    "text": user_text,
                    "timestamp": msg.timestamp,
                    "uuid": msg.uuid,
                })

        elif msg.message_type == "assistant" and msg.content_blocks:
            ensure_assistant(msg.timestamp, msg.uuid, msg.model, msg.usage)

            for block in msg.content_blocks:
                if block.block_type == "text" and block.text.strip():
                    current_assistant["text_blocks"].append(block.text)
                elif block.block_type == "thinking" and block.text.strip():
                    current_assistant["thinking_blocks"].append(block.text)
                elif block.block_type == "tool_use":
                    tool_call = {
                        "tool_name": block.tool_name,
                        "tool_id": block.tool_id,
                        "tool_input": block.tool_input,
                        "tool_result": None,
                    }
                    if block.tool_id in tool_results:
                        tool_call["tool_result"] = tool_results[block.tool_id]
                    current_assistant["tool_calls"].append(tool_call)

            # Update model/usage if newer
            if msg.model:
                current_assistant["model"] = msg.model
            if msg.usage:
                current_assistant["usage"] = {
                    "input_tokens": msg.usage.input_tokens,
                    "output_tokens": msg.usage.output_tokens,
                }

    flush_assistant()
    return turns


def _project_display(project_path: str) -> str:
    """Get short display name from project path."""
    if not project_path:
        return ""
    parts = project_path.strip("/").split("/")
    skip = {"home", "root", "Users"}
    meaningful = [p for p in parts if p not in skip]
    if len(meaningful) >= 2:
        return "/".join(meaningful[-2:])
    return meaningful[-1] if meaningful else parts[-1]


def format_timestamp(ts: str) -> str:
    """Format an ISO timestamp for display."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y %H:%M")
    except Exception:
        return ts[:16]
