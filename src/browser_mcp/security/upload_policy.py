"""Local-file policy enforced before any path reaches a Chrome file input."""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Final

MAX_UPLOAD_FILES: Final = 10
MAX_UPLOAD_FILE_BYTES: Final = 64 * 1024 * 1024
MAX_UPLOAD_TOTAL_BYTES: Final = 128 * 1024 * 1024
# Credential stores an injected page must never be able to talk the agent into uploading.
SENSITIVE_DIRECTORY_NAMES: Final = frozenset(
    {".ssh", ".gnupg", ".aws", ".kube", ".docker", "gcloud", "Keychains"}
)
SENSITIVE_FILE_NAMES: Final = frozenset(
    {".env", ".netrc", ".npmrc", ".pypirc", "credentials", "id_rsa", "id_ed25519"}
)


class UploadPolicyError(ValueError):
    """Raised before Chrome may read one local path into a file input."""


class LocalFilePolicy:
    """Accept only readable regular files that stay inside bounded size limits."""

    def __init__(
        self,
        max_files: int = MAX_UPLOAD_FILES,
        max_file_bytes: int = MAX_UPLOAD_FILE_BYTES,
        max_total_bytes: int = MAX_UPLOAD_TOTAL_BYTES,
    ) -> None:
        """Create a policy with injectable limits so tests stay deterministic."""
        self._max_files = max_files
        self._max_file_bytes = max_file_bytes
        self._max_total_bytes = max_total_bytes

    def resolve(self, raw_paths: Sequence[str]) -> tuple[str, ...]:
        """Return absolute symlink-free files Chrome may read, or raise UploadPolicyError."""
        if not raw_paths:
            raise UploadPolicyError("upload requires at least one file path")
        if len(raw_paths) > self._max_files:
            raise UploadPolicyError(
                f"upload accepts at most {self._max_files} files, got {len(raw_paths)}"
            )
        resolved: list[str] = []
        total_bytes = 0
        for raw in raw_paths:
            path = self._resolve_one(raw)
            size = path.stat().st_size
            if size > self._max_file_bytes:
                raise UploadPolicyError(
                    f"file exceeds the {self._max_file_bytes} byte upload limit: {path}"
                )
            total_bytes += size
            if total_bytes > self._max_total_bytes:
                raise UploadPolicyError(
                    f"upload exceeds the {self._max_total_bytes} byte total limit"
                )
            resolved.append(str(path))
        return tuple(resolved)

    def _resolve_one(self, raw: str) -> Path:
        """Expand, resolve, and authorize one requested path before Chrome reads it."""
        candidate = str(raw).strip()
        if not candidate:
            raise UploadPolicyError("upload path must not be empty")
        # Resolve first: the sensitivity check has to see the real target of every symlink.
        path = Path(candidate).expanduser().resolve()
        if not path.exists():
            raise UploadPolicyError(f"upload path does not exist: {path}")
        if not path.is_file():
            raise UploadPolicyError(f"upload path is not a regular file: {path}")
        if not os.access(path, os.R_OK):
            raise UploadPolicyError(f"upload path is not readable: {path}")
        self._reject_sensitive(path)
        return path

    @staticmethod
    def _reject_sensitive(path: Path) -> None:
        """Refuse credential stores regardless of which page asked for the upload."""
        if SENSITIVE_DIRECTORY_NAMES.intersection(path.parts):
            raise UploadPolicyError(
                f"refusing to upload from a credential directory: {path}"
            )
        if path.name in SENSITIVE_FILE_NAMES:
            raise UploadPolicyError(f"refusing to upload a credential file: {path}")
