"""
Parser for Claude Code conversation history stored under ~/.claude/
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"


@dataclass
class ContentBlock:
    block_type: str  # text, thinking, tool_use, tool_result
    text: str = ""
    tool_name: str = ""
    tool_id: str = ""
    tool_input: dict = field(default_factory=dict)
    is_error: bool = False
    signature: str = ""


@dataclass
class Message:
    uuid: str
    parent_uuid: str | None
    msg_type: str  # user, assistant
    timestamp: str
    content_blocks: list[ContentBlock] = field(default_factory=list)
    raw_text: str = ""  # for user messages that are plain strings
    model: str = ""
    usage: dict = field(default_factory=dict)
    is_sidechain: bool = False
    session_id: str = ""
    cwd: str = ""


@dataclass
class SessionMeta:
    session_id: str
    project_name: str
    first_timestamp: str = ""
    last_timestamp: str = ""
    message_count: int = 0
    first_user_message: str = ""
    slug: str = ""
    model: str = ""
    subagent_count: int = 0


@dataclass
class Project:
    name: str
    display_name: str
    session_count: int = 0
    sessions: list[SessionMeta] = field(default_factory=list)


def decode_project_name(encoded: str) -> str:
    """Convert encoded project dir name back to a readable path."""
    return encoded.replace("-", "/", 1).replace("-", "/")


def get_display_name(encoded: str) -> str:
    """Get a short display name for a project."""
    path = decode_project_name(encoded)
    parts = path.strip("/").split("/")
    # Return last 2 meaningful parts
    meaningful = [p for p in parts if p not in ("home", "svobodadl", "Dev", "Projects")]
    if meaningful:
        return "/".join(meaningful[-2:])
    return parts[-1] if parts else encoded


def list_projects() -> list[Project]:
    """List all projects with session counts."""
    projects = []
    if not PROJECTS_DIR.exists():
        return projects

    for d in sorted(PROJECTS_DIR.iterdir()):
        if not d.is_dir():
            continue
        jsonl_files = list(d.glob("*.jsonl"))
        sessions = []
        for jf in jsonl_files:
            meta = _get_session_meta_fast(d.name, jf.stem)
            if meta:
                sessions.append(meta)
        sessions.sort(key=lambda s: s.last_timestamp or s.first_timestamp, reverse=True)

        proj = Project(
            name=d.name,
            display_name=get_display_name(d.name),
            session_count=len(jsonl_files),
            sessions=sessions,
        )
        projects.append(proj)

    projects.sort(key=lambda p: p.sessions[0].last_timestamp if p.sessions else "", reverse=True)
    return projects


def _get_session_meta_fast(project_name: str, session_id: str) -> SessionMeta | None:
    """Get session metadata by reading first and last lines of JSONL."""
    path = PROJECTS_DIR / project_name / f"{session_id}.jsonl"
    if not path.exists():
        return None

    meta = SessionMeta(session_id=session_id, project_name=project_name)

    try:
        with open(path, "r") as f:
            lines = f.readlines()
    except Exception:
        return meta

    meta.message_count = len(lines)

    # Extract metadata from lines
    first_ts = None
    last_ts = None
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        ts = obj.get("timestamp", "")
        if isinstance(ts, str) and ts:
            if first_ts is None:
                first_ts = ts
            last_ts = ts

        if obj.get("slug"):
            meta.slug = obj["slug"]

        msg_type = obj.get("type")
        if msg_type == "user" and not meta.first_user_message:
            content = obj.get("message", {}).get("content", "")
            if isinstance(content, str) and content.strip() and not content.strip().startswith("<"):
                meta.first_user_message = content.strip()[:120]
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text = item.get("text", "").strip()
                        if text and text != "[Request interrupted by user for tool use]":
                            meta.first_user_message = text[:120]
                            break

        if msg_type == "assistant":
            msg = obj.get("message", {})
            if msg.get("model"):
                meta.model = msg["model"]

    meta.first_timestamp = first_ts or ""
    meta.last_timestamp = last_ts or ""

    # Count subagents
    subagent_dir = PROJECTS_DIR / project_name / session_id / "subagents"
    if subagent_dir.exists():
        meta.subagent_count = len(list(subagent_dir.glob("agent-*.jsonl")))

    return meta


def parse_content_blocks(content: Any) -> tuple[list[ContentBlock], str]:
    """Parse message content into ContentBlocks. Returns (blocks, raw_text)."""
    if isinstance(content, str):
        return [], content

    blocks = []
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            btype = item.get("type", "")

            if btype == "text":
                blocks.append(ContentBlock(
                    block_type="text",
                    text=item.get("text", ""),
                ))
            elif btype == "thinking":
                blocks.append(ContentBlock(
                    block_type="thinking",
                    text=item.get("thinking", ""),
                    signature=item.get("signature", ""),
                ))
            elif btype == "tool_use":
                blocks.append(ContentBlock(
                    block_type="tool_use",
                    tool_name=item.get("name", ""),
                    tool_id=item.get("id", ""),
                    tool_input=item.get("input", {}),
                ))
            elif btype == "tool_result":
                content_val = item.get("content", "")
                if isinstance(content_val, list):
                    # Sometimes tool_result content is a list of blocks
                    content_val = "\n".join(
                        b.get("text", "") for b in content_val if isinstance(b, dict)
                    )
                blocks.append(ContentBlock(
                    block_type="tool_result",
                    tool_id=item.get("tool_use_id", ""),
                    text=str(content_val),
                    is_error=item.get("is_error", False),
                ))

    return blocks, ""


def _parse_jsonl_to_merged_messages(path: Path) -> list[Message]:
    """Parse a JSONL file into Message objects, merging assistant entries
    that share the same API message ID into single messages.

    Assistant responses are streamed as multiple JSONL entries with the same
    message.id — each carrying one content block. This function collects all
    blocks for each API message and produces one merged Message per response.
    """
    try:
        with open(path, "r") as f:
            lines = f.readlines()
    except Exception:
        return []

    # First pass: collect all tool_result blocks for linking later
    tool_result_blocks = {}
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "user":
            msg_data = obj.get("message", {})
            content = msg_data.get("content", "")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        tid = item.get("tool_use_id", "")
                        if tid:
                            content_val = item.get("content", "")
                            if isinstance(content_val, list):
                                content_val = "\n".join(
                                    b.get("text", "") for b in content_val if isinstance(b, dict)
                                )
                            tool_result_blocks[tid] = ContentBlock(
                                block_type="tool_result",
                                tool_id=tid,
                                text=str(content_val),
                                is_error=item.get("is_error", False),
                            )

    # Second pass: build messages, merging assistant entries by API message ID
    messages = []
    # Track assistant messages by their API message ID for merging
    assistant_by_api_id: dict[str, Message] = {}
    assistant_order: list[str] = []  # preserve insertion order

    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_type = obj.get("type")
        if msg_type not in ("user", "assistant"):
            continue

        msg_data = obj.get("message", {})
        content = msg_data.get("content", "")
        blocks, raw_text = parse_content_blocks(content)

        if msg_type == "user":
            # User messages with actual text input
            # Content can be a plain string OR an array with text blocks
            user_text = raw_text
            if not user_text and blocks:
                # Extract text from content array (e.g. [{"type": "text", "text": "..."}])
                text_parts = [b.text for b in blocks if b.block_type == "text" and b.text.strip()]
                if text_parts:
                    user_text = "\n".join(text_parts)

            # Filter out system-generated messages
            if user_text and not user_text.startswith("[Request interrupted by user") and not user_text.startswith("<"):
                messages.append(Message(
                    uuid=obj.get("uuid", ""),
                    parent_uuid=obj.get("parentUuid"),
                    msg_type="user",
                    timestamp=obj.get("timestamp", ""),
                    raw_text=user_text,
                    session_id=obj.get("sessionId", ""),
                    cwd=obj.get("cwd", ""),
                ))
            # User messages that are only tool results are handled via tool_result_blocks

        elif msg_type == "assistant":
            api_id = msg_data.get("id", "")
            if not api_id:
                # No API ID — treat as standalone
                if blocks:
                    messages.append(Message(
                        uuid=obj.get("uuid", ""),
                        parent_uuid=obj.get("parentUuid"),
                        msg_type="assistant",
                        timestamp=obj.get("timestamp", ""),
                        content_blocks=blocks,
                        model=msg_data.get("model", ""),
                        usage=msg_data.get("usage", {}),
                        is_sidechain=obj.get("isSidechain", False),
                        session_id=obj.get("sessionId", ""),
                        cwd=obj.get("cwd", ""),
                    ))
                continue

            if api_id not in assistant_by_api_id:
                # First entry for this API message
                msg = Message(
                    uuid=obj.get("uuid", ""),
                    parent_uuid=obj.get("parentUuid"),
                    msg_type="assistant",
                    timestamp=obj.get("timestamp", ""),
                    content_blocks=[],
                    model=msg_data.get("model", ""),
                    usage=msg_data.get("usage", {}),
                    is_sidechain=obj.get("isSidechain", False),
                    session_id=obj.get("sessionId", ""),
                    cwd=obj.get("cwd", ""),
                )
                assistant_by_api_id[api_id] = msg
                assistant_order.append(api_id)
                # Insert a placeholder in messages list to preserve ordering
                messages.append(msg)

            merged = assistant_by_api_id[api_id]
            # Append new content blocks
            merged.content_blocks.extend(blocks)
            # Update with latest metadata (last entry has final usage)
            if msg_data.get("usage"):
                merged.usage = msg_data["usage"]
            if msg_data.get("model"):
                merged.model = msg_data["model"]
            # Update timestamp to latest
            ts = obj.get("timestamp", "")
            if ts:
                merged.timestamp = ts

    return messages, tool_result_blocks


def get_session_messages(project_name: str, session_id: str,
                         offset: int = 0, limit: int = 0) -> list[Message]:
    """Parse a session JSONL file into Message objects.
    Merges streamed assistant entries into complete messages.
    """
    path = PROJECTS_DIR / project_name / f"{session_id}.jsonl"
    if not path.exists():
        return []

    messages, _ = _parse_jsonl_to_merged_messages(path)

    if offset:
        messages = messages[offset:]
    if limit:
        messages = messages[:limit]

    return messages


def _messages_to_turns(messages: list[Message], tool_results: dict) -> list[dict]:
    """Convert messages into conversation turns grouped as exchanges.

    Groups all assistant messages between two user messages into a single
    assistant turn. Each turn has:
    - text_blocks: all text the agent said (prominent display)
    - thinking_blocks: all thinking blocks (collapsible)
    - tool_calls: all tool invocations with their results (collapsible)
    """
    turns = []
    # Accumulator for current assistant turn
    current_assistant = None

    def flush_assistant():
        nonlocal current_assistant
        if current_assistant is None:
            return
        # Only add if there's actual content
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
                "usage": usage or {},
            }

    for msg in messages:
        if msg.msg_type == "user" and msg.raw_text:
            flush_assistant()
            turns.append({
                "type": "user",
                "text": msg.raw_text,
                "timestamp": msg.timestamp,
                "uuid": msg.uuid,
            })
        elif msg.msg_type == "assistant" and msg.content_blocks:
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
                        tr = tool_results[block.tool_id]
                        tool_call["tool_result"] = {
                            "text": tr.text,
                            "is_error": tr.is_error,
                        }
                    current_assistant["tool_calls"].append(tool_call)

            # Update model/usage if newer
            if msg.model:
                current_assistant["model"] = msg.model
            if msg.usage:
                current_assistant["usage"] = msg.usage

    flush_assistant()
    return turns


def get_conversation_turns(project_name: str, session_id: str,
                           offset: int = 0, limit: int = 0) -> list[dict]:
    """Get messages grouped into conversation turns for display.

    Returns a list of turn dicts, each representing either a user input
    or an assistant response with all content blocks merged from streaming.
    """
    path = PROJECTS_DIR / project_name / f"{session_id}.jsonl"
    if not path.exists():
        return []

    messages, tool_results = _parse_jsonl_to_merged_messages(path)
    turns = _messages_to_turns(messages, tool_results)

    if offset:
        turns = turns[offset:]
    if limit:
        turns = turns[:limit]

    return turns


def get_subagent_list(project_name: str, session_id: str) -> list[dict]:
    """List subagents for a session."""
    subagent_dir = PROJECTS_DIR / project_name / session_id / "subagents"
    if not subagent_dir.exists():
        return []

    agents = []
    for f in sorted(subagent_dir.glob("agent-*.jsonl")):
        agent_id = f.stem.replace("agent-", "")
        # Read first few lines to get description
        desc = ""
        msg_count = 0
        try:
            with open(f, "r") as fh:
                lines = fh.readlines()
                msg_count = len(lines)
                for line in lines[:5]:
                    obj = json.loads(line)
                    if obj.get("type") == "user":
                        content = obj.get("message", {}).get("content", "")
                        if isinstance(content, str):
                            desc = content[:100]
                            break
                        elif isinstance(content, list):
                            for item in content:
                                if isinstance(item, dict) and item.get("type") == "text":
                                    desc = item.get("text", "")[:100]
                                    break
                            if desc:
                                break
        except Exception:
            pass

        agents.append({
            "agent_id": agent_id,
            "description": desc,
            "message_count": msg_count,
        })
    return agents


def get_subagent_turns(project_name: str, session_id: str, agent_id: str) -> list[dict]:
    """Get conversation turns for a subagent."""
    path = PROJECTS_DIR / project_name / session_id / "subagents" / f"agent-{agent_id}.jsonl"
    if not path.exists():
        return []

    messages, tool_results = _parse_jsonl_to_merged_messages(path)
    return _messages_to_turns(messages, tool_results)


def get_stats() -> dict:
    """Read stats-cache.json."""
    path = CLAUDE_DIR / "stats-cache.json"
    if not path.exists():
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def search_sessions(query: str, max_results: int = 50) -> list[dict]:
    """Search across all sessions for matching text."""
    query_lower = query.lower()
    results = []

    if not PROJECTS_DIR.exists():
        return results

    for proj_dir in sorted(PROJECTS_DIR.iterdir()):
        if not proj_dir.is_dir():
            continue

        for jsonl_file in proj_dir.glob("*.jsonl"):
            session_id = jsonl_file.stem
            try:
                with open(jsonl_file, "r") as f:
                    for line_num, line in enumerate(f, 1):
                        if query_lower in line.lower():
                            try:
                                obj = json.loads(line)
                            except json.JSONDecodeError:
                                continue

                            msg_type = obj.get("type")
                            if msg_type not in ("user", "assistant"):
                                continue

                            msg_data = obj.get("message", {})
                            content = msg_data.get("content", "")

                            # Extract matching text snippet
                            snippet = ""
                            if isinstance(content, str):
                                snippet = content
                            elif isinstance(content, list):
                                for item in content:
                                    if isinstance(item, dict):
                                        text = item.get("text", "") or item.get("thinking", "") or item.get("content", "")
                                        if isinstance(text, str) and query_lower in text.lower():
                                            snippet = text
                                            break

                            if not snippet:
                                continue

                            # Find the query in the snippet and extract context
                            idx = snippet.lower().find(query_lower)
                            start = max(0, idx - 80)
                            end = min(len(snippet), idx + len(query) + 80)
                            context = snippet[start:end].strip()
                            if start > 0:
                                context = "..." + context
                            if end < len(snippet):
                                context = context + "..."

                            results.append({
                                "project": proj_dir.name,
                                "project_display": get_display_name(proj_dir.name),
                                "session_id": session_id,
                                "msg_type": msg_type,
                                "timestamp": obj.get("timestamp", ""),
                                "context": context,
                                "uuid": obj.get("uuid", ""),
                            })

                            if len(results) >= max_results:
                                return results
            except Exception:
                continue

    return results


def format_timestamp(ts: str) -> str:
    """Format an ISO timestamp for display."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y %H:%M")
    except Exception:
        return ts[:16]
