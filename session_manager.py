"""Session manager for multi-user viewer support.

Maps viewer_id UUIDs to live ArchiveBridge instances with expiry/cleanup.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import bridge


# Default session TTL: 1 hour
DEFAULT_TTL = 3600

# Base directory for viewer data
VIEWERS_DIR = Path("/tmp/claude-viewers")


@dataclass
class ViewerSession:
    """Holds state for a single viewer's uploaded data."""
    viewer_id: str
    archive: bridge.ArchiveBridge
    claude_dir: Path       # extracted .claude/ data
    archive_dir: Path      # SQLite DB location
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)

    def touch(self):
        self.last_accessed = time.time()


class SessionManager:
    """Thread-safe registry of viewer sessions with automatic expiry."""

    def __init__(self, ttl: int | None = None):
        self.ttl = ttl or int(os.environ.get("SESSION_TTL", DEFAULT_TTL))
        self._sessions: dict[str, ViewerSession] = {}
        self._lock = threading.Lock()

    def register(self, session: ViewerSession) -> None:
        with self._lock:
            self._sessions[session.viewer_id] = session

    def get(self, viewer_id: str) -> ViewerSession | None:
        with self._lock:
            session = self._sessions.get(viewer_id)
            if session:
                session.touch()
            return session

    def remove(self, viewer_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(viewer_id, None)
        if session:
            self._destroy(session)

    def cleanup_expired(self) -> int:
        """Remove sessions that have exceeded the TTL. Returns count removed."""
        now = time.time()
        expired: list[ViewerSession] = []
        with self._lock:
            for vid, session in list(self._sessions.items()):
                if now - session.last_accessed > self.ttl:
                    expired.append(self._sessions.pop(vid))
        for session in expired:
            self._destroy(session)
        return len(expired)

    def total_disk_bytes(self) -> int:
        """Total disk usage across all viewer directories."""
        if not VIEWERS_DIR.exists():
            return 0
        total = 0
        for path in VIEWERS_DIR.iterdir():
            if path.is_dir():
                for f in path.rglob("*"):
                    if f.is_file():
                        total += f.stat().st_size
        return total

    def active_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    @staticmethod
    def _destroy(session: ViewerSession) -> None:
        """Close archive and delete viewer data from disk."""
        try:
            session.archive.close()
        except Exception:
            pass
        base = VIEWERS_DIR / session.viewer_id
        if base.exists():
            shutil.rmtree(base, ignore_errors=True)
