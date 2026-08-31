"""Install the packaged Chrome extension into a stable user data directory."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Final

from browser_mcp.config import discover_project_root

PAIRING_TOKEN_BYTES: Final = 32


@dataclass(frozen=True, slots=True)
class InstalledExtension:
    """Paths and secrets required by the local extension bridge."""

    directory: Path
    token: str
    build_id: str


class ExtensionBundle:
    """Materialize immutable package resources plus local pairing metadata."""

    def __init__(
        self,
        data_dir: Path,
        extension_dir: Path,
        base_port: int,
        pool_size: int,
        path: str,
    ) -> None:
        """Bind the installer to one application data directory and port pool."""
        self._data_dir = data_dir
        self._extension_dir = extension_dir
        self._base_port = base_port
        self._pool_size = pool_size
        self._path = path

    def ensure_installed(self) -> InstalledExtension:
        """Refresh extension sources while preserving the local pairing token."""
        extension_dir = self._extension_dir
        extension_dir.mkdir(parents=True, exist_ok=True)
        token = self._load_or_create_token()
        source_files = self._source_files()
        build_id = self._fingerprint(source_files)

        for relative_path, content in source_files:
            target = extension_dir / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(target, content)

        self._atomic_write(
            extension_dir / "build-info.js",
            f'export const BUNDLE_BUILD_ID = "{build_id}";\n'.encode(),
        )

        pairing = {
            "token": token,
            "build_id": build_id,
            "base_port": self._base_port,
            "pool_size": self._pool_size,
            "path": self._path,
        }
        pairing_path = extension_dir / "pairing.json"
        self._atomic_write(
            pairing_path,
            (json.dumps(pairing, indent=2, sort_keys=True) + "\n").encode(),
            mode=0o600,
        )
        return InstalledExtension(directory=extension_dir, token=token, build_id=build_id)

    @staticmethod
    def _atomic_write(target: Path, content: bytes, mode: int = 0o644) -> None:
        """Replace one generated file atomically so Chrome never observes partial JSON or code."""
        temporary = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            descriptor = os.open(temporary, flags, mode)
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
            os.replace(temporary, target)
            if os.name != "nt":
                target.chmod(mode)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_or_create_token(self) -> str:
        """Return a persistent user-only token shared with the unpacked extension."""
        token_path = self._data_dir / "pairing-token"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            token = secrets.token_urlsafe(PAIRING_TOKEN_BYTES)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            try:
                descriptor = os.open(token_path, flags, 0o600)
            except FileExistsError:
                token = token_path.read_text(encoding="utf-8").strip()
            else:
                with os.fdopen(descriptor, "w", encoding="utf-8") as token_file:
                    token_file.write(token + "\n")
        if not token:
            raise RuntimeError(f"pairing token is empty: {token_path}")
        return token

    @staticmethod
    def _source_files() -> list[tuple[Path, bytes]]:
        """Read every packaged extension resource in deterministic path order."""
        project_root = discover_project_root()
        if project_root is not None:
            source_dir = project_root / "extension"
            source_names = (
                "background.js",
                "background_tabs.js",
                "comment_sessions.js",
                "content_bridge.js",
                "content_inject.js",
                "douyin_content_bridge.js",
                "douyin_content_inject.js",
                "manifest.json",
                "options.html",
                "options.js",
            )
            if all((source_dir / name).is_file() for name in source_names):
                return [(Path(name), (source_dir / name).read_bytes()) for name in source_names]

        root = files("browser_mcp.extension")
        resources: list[tuple[Path, bytes]] = []
        for child in sorted(root.iterdir(), key=lambda item: item.name):
            if child.is_file() and child.name != "__init__.py":
                resources.append((Path(child.name), child.read_bytes()))
        return resources

    @staticmethod
    def _fingerprint(source_files: list[tuple[Path, bytes]]) -> str:
        """Build a stable SHA-256 fingerprint from resource paths and contents."""
        digest = hashlib.sha256()
        for relative_path, content in source_files:
            digest.update(relative_path.as_posix().encode())
            digest.update(b"\0")
            digest.update(content)
            digest.update(b"\0")
        return digest.hexdigest()[:16]
