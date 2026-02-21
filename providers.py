"""Data providers for multi-user viewer support.

Each provider knows how to obtain raw Claude data and ingest it into an
ArchiveBridge suitable for browsing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import uuid
import zipfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import bridge
from session_manager import VIEWERS_DIR, ViewerSession

from claude_archive.config import (
    BackendConfig,
    BackendsConfig,
    Config,
    GeneralConfig,
    SourceConfig,
)
from claude_archive.ingest.pipeline import run_ingest


# Maximum disk budget for all viewers (bytes) — leaves room for app + owner data
MAX_VIEWER_DISK = int(os.environ.get("MAX_VIEWER_DISK", 1_500_000_000))  # 1.5 Gi


@dataclass
class ProviderResult:
    """Outcome of a data provider's prepare() call."""
    session: ViewerSession
    error: str | None = None


class DataProvider(ABC):
    """Base class for pluggable data sources."""

    @abstractmethod
    def prepare(self, viewer_id: str) -> ProviderResult:
        """Ingest data and return a ready-to-use ViewerSession."""
        ...


class UploadProvider(DataProvider):
    """Handles .zip and .tar.gz uploads of ~/.claude/ directories."""

    def __init__(self, file_storage):
        """file_storage: a werkzeug FileStorage object from the upload."""
        self.file_storage = file_storage

    def prepare(self, viewer_id: str) -> ProviderResult:
        base = VIEWERS_DIR / viewer_id
        extract_dir = base / "extract"
        claude_dir = base / "claude"
        archive_dir = base / "archive"

        os.makedirs(extract_dir, exist_ok=True)
        os.makedirs(claude_dir, exist_ok=True)
        os.makedirs(archive_dir, exist_ok=True)

        filename = self.file_storage.filename or ""

        try:
            # Save and extract
            if filename.endswith(".zip"):
                saved = extract_dir / "upload.zip"
                self.file_storage.save(str(saved))
                with zipfile.ZipFile(saved, "r") as zf:
                    zf.extractall(extract_dir / "contents")
            elif filename.endswith((".tar.gz", ".tgz")):
                saved = extract_dir / "upload.tar.gz"
                self.file_storage.save(str(saved))
                with tarfile.open(saved, "r:gz") as tf:
                    tf.extractall(extract_dir / "contents", filter="data")
            else:
                return ProviderResult(session=None, error="Unsupported file type. Please upload a .zip or .tar.gz file.")

            # Find and relocate the Claude projects data
            _relocate_claude_data(extract_dir / "contents", claude_dir)

            # Clean up the raw extract to save disk
            shutil.rmtree(extract_dir, ignore_errors=True)

            # Run ingest with SQLite-only config
            config = Config(
                source=SourceConfig(claude_dir=str(claude_dir)),
                general=GeneralConfig(archive_dir=str(archive_dir)),
                backends=BackendsConfig(
                    jsonl=BackendConfig(enabled=False),
                    sqlite=BackendConfig(enabled=True),
                    tantivy=BackendConfig(enabled=False),
                ),
            )
            result = run_ingest(config=config, quiet=True)

            if result.sessions_ingested == 0 and result.sessions_scanned == 0:
                shutil.rmtree(base, ignore_errors=True)
                return ProviderResult(session=None, error="No Claude sessions found in the uploaded archive.")

            # Build the ArchiveBridge
            archive = bridge.ArchiveBridge(config=config)

            session = ViewerSession(
                viewer_id=viewer_id,
                archive=archive,
                claude_dir=claude_dir,
                archive_dir=archive_dir,
            )
            return ProviderResult(session=session)

        except (zipfile.BadZipFile, tarfile.TarError) as exc:
            shutil.rmtree(base, ignore_errors=True)
            return ProviderResult(session=None, error=f"Invalid archive file: {exc}")
        except Exception as exc:
            shutil.rmtree(base, ignore_errors=True)
            return ProviderResult(session=None, error=f"Failed to process upload: {exc}")


class GCSBucketProvider(DataProvider):
    """Syncs Claude data from a GCS bucket path via gcloud CLI."""

    def __init__(self, gcs_path: str):
        """gcs_path: gs://bucket/path to a .claude/ directory."""
        self.gcs_path = gcs_path.rstrip("/")

    def prepare(self, viewer_id: str) -> ProviderResult:
        base = VIEWERS_DIR / viewer_id
        claude_dir = base / "claude"
        archive_dir = base / "archive"

        os.makedirs(claude_dir, exist_ok=True)
        os.makedirs(archive_dir, exist_ok=True)

        try:
            # Sync from GCS
            cmd = [
                "gcloud", "storage", "rsync", "-r",
                self.gcs_path, str(claude_dir),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if proc.returncode != 0:
                shutil.rmtree(base, ignore_errors=True)
                return ProviderResult(session=None, error=f"GCS sync failed: {proc.stderr[:500]}")

            # Run ingest with SQLite-only config
            config = Config(
                source=SourceConfig(claude_dir=str(claude_dir)),
                general=GeneralConfig(archive_dir=str(archive_dir)),
                backends=BackendsConfig(
                    jsonl=BackendConfig(enabled=False),
                    sqlite=BackendConfig(enabled=True),
                    tantivy=BackendConfig(enabled=False),
                ),
            )
            result = run_ingest(config=config, quiet=True)

            if result.sessions_ingested == 0 and result.sessions_scanned == 0:
                shutil.rmtree(base, ignore_errors=True)
                return ProviderResult(session=None, error="No Claude sessions found at the specified GCS path.")

            archive = bridge.ArchiveBridge(config=config)

            session = ViewerSession(
                viewer_id=viewer_id,
                archive=archive,
                claude_dir=claude_dir,
                archive_dir=archive_dir,
            )
            return ProviderResult(session=session)

        except subprocess.TimeoutExpired:
            shutil.rmtree(base, ignore_errors=True)
            return ProviderResult(session=None, error="GCS sync timed out after 120 seconds.")
        except Exception as exc:
            shutil.rmtree(base, ignore_errors=True)
            return ProviderResult(session=None, error=f"Failed to sync from GCS: {exc}")


def _relocate_claude_data(src: Path, claude_dir: Path) -> None:
    """Find the projects/ directory inside an arbitrary zip structure and move it.

    Users may zip their entire ~/.claude/ or just the projects/ folder, possibly
    nested inside an extra directory layer.  We search for the canonical
    ``projects/`` directory and copy its contents into ``claude_dir/projects/``.
    """
    # Look for a "projects" directory at any nesting depth
    candidates = sorted(src.rglob("projects"), key=lambda p: len(p.parts))

    for candidate in candidates:
        if candidate.is_dir() and any(candidate.iterdir()):
            dest = claude_dir / "projects"
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(candidate, dest)
            return

    # Fallback: if no projects/ found, copy everything — the ingest pipeline
    # will discover whatever session files exist.
    for item in src.iterdir():
        dest = claude_dir / item.name
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)
