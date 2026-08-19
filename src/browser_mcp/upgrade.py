"""Safe, agent-friendly inspection and upgrade workflow for source checkouts."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Protocol

from browser_mcp import __version__
from browser_mcp.config import discover_project_root


class UpgradeError(RuntimeError):
    """Raised when an installation cannot be checked or upgraded safely."""


class CommandRunner(Protocol):
    """Run one argv-only command in a known source checkout."""

    def __call__(self, args: tuple[str, ...], cwd: Path) -> str:
        """Return stripped stdout or raise ``UpgradeError``."""
        ...


@dataclass(frozen=True, slots=True)
class InstallationMetadata:
    """Local installation facts safe to expose through ``browser_status``."""

    server_version: str
    install_mode: Literal["source", "package"]
    project_root: str | None
    source_commit: str | None
    upgrade_check_command: str | None
    upgrade_apply_command: str | None
    restart_instruction: str


@dataclass(frozen=True, slots=True)
class SourceUpgradeState:
    """One network-refreshed comparison between a checkout and its upstream."""

    project_root: str
    branch: str
    upstream: str
    local_commit: str
    remote_commit: str
    ahead: int
    behind: int
    state: Literal["up_to_date", "behind", "ahead", "diverged"]
    update_available: bool
    dirty: bool


@dataclass(frozen=True, slots=True)
class UpgradeReport:
    """Stable JSON-serializable result returned by the upgrade CLI."""

    ok: bool
    action: Literal["check", "apply"]
    server_version: str
    install_mode: Literal["source", "package"]
    project_root: str | None
    branch: str | None
    upstream: str | None
    local_commit: str | None
    remote_commit: str | None
    ahead: int | None
    behind: int | None
    state: str
    update_available: bool | None
    dirty: bool | None
    changed: bool
    dependencies_synced: bool
    restart_required: bool
    extension_reload_automatic: bool
    detail: str

    def to_json(self) -> str:
        """Serialize the report deterministically for an Agent or shell user."""
        return json.dumps(asdict(self), ensure_ascii=False, indent=2, sort_keys=True)


class SubprocessCommandRunner:
    """Production argv-only subprocess adapter with bounded diagnostic output."""

    def __call__(self, args: tuple[str, ...], cwd: Path) -> str:
        """Run one command without a shell and translate failures into domain errors."""
        try:
            result = subprocess.run(
                args,
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            raise UpgradeError(f"unable to run {args[0]}: {error}") from error
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip()
            if len(message) > 2_000:
                message = message[-2_000:]
            raise UpgradeError(f"{' '.join(args)} failed: {message or result.returncode}")
        return result.stdout.strip()


def installation_metadata(
    project_root: Path | None = None,
    runner: CommandRunner | None = None,
) -> InstallationMetadata:
    """Describe the current install and provide exact, absolute upgrade commands."""
    root = project_root if project_root is not None else discover_project_root()
    restart_instruction = (
        "After applying an update, restart Codex once. The restarted MCP server refreshes "
        "and reloads the Chrome extension automatically."
    )
    if root is None:
        return InstallationMetadata(
            server_version=__version__,
            install_mode="package",
            project_root=None,
            source_commit=None,
            upgrade_check_command=None,
            upgrade_apply_command=None,
            restart_instruction=(
                "This is a packaged install; upgrade it with the package manager that installed "
                "browser-mcp, then restart Codex once."
            ),
        )

    resolved = root.resolve()
    command_runner = runner or SubprocessCommandRunner()
    try:
        source_commit = command_runner(("git", "rev-parse", "--short", "HEAD"), resolved)
    except UpgradeError:
        source_commit = None
    base = ("uv", "--directory", str(resolved), "run", "browser-mcp", "upgrade")
    return InstallationMetadata(
        server_version=__version__,
        install_mode="source",
        project_root=str(resolved),
        source_commit=source_commit,
        upgrade_check_command=_format_command((*base, "--check", "--json")),
        upgrade_apply_command=_format_command((*base, "--apply", "--json")),
        restart_instruction=restart_instruction,
    )


def inspect_source_upgrade(
    project_root: Path,
    runner: CommandRunner | None = None,
) -> SourceUpgradeState:
    """Fetch the configured upstream and compare it without changing the worktree."""
    root = project_root.resolve()
    command_runner = runner or SubprocessCommandRunner()
    _require_source_checkout(root)
    dirty = bool(
        command_runner(
            ("git", "status", "--porcelain=v1", "--untracked-files=normal"),
            root,
        )
    )
    branch = command_runner(("git", "symbolic-ref", "--quiet", "--short", "HEAD"), root)
    upstream = command_runner(
        ("git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"),
        root,
    )
    if not branch or "/" not in upstream:
        raise UpgradeError("the source checkout needs an attached branch with an upstream")
    remote, _ = upstream.split("/", 1)
    merge_ref = command_runner(("git", "config", "--get", f"branch.{branch}.merge"), root)
    if not merge_ref.startswith("refs/heads/"):
        raise UpgradeError(f"unsupported upstream merge ref: {merge_ref or '<empty>'}")

    local_commit = command_runner(("git", "rev-parse", "HEAD"), root)
    command_runner(("git", "fetch", "--quiet", "--no-tags", remote, merge_ref), root)
    remote_commit = command_runner(("git", "rev-parse", "FETCH_HEAD"), root)
    counts = command_runner(
        ("git", "rev-list", "--left-right", "--count", "HEAD...FETCH_HEAD"),
        root,
    ).split()
    if len(counts) != 2:
        raise UpgradeError(f"unexpected git rev-list output: {' '.join(counts)}")
    try:
        ahead, behind = (int(value) for value in counts)
    except ValueError as error:
        raise UpgradeError(f"unexpected git rev-list counts: {' '.join(counts)}") from error

    state: Literal["up_to_date", "behind", "ahead", "diverged"]
    if ahead and behind:
        state = "diverged"
    elif behind:
        state = "behind"
    elif ahead:
        state = "ahead"
    else:
        state = "up_to_date"
    return SourceUpgradeState(
        project_root=str(root),
        branch=branch,
        upstream=upstream,
        local_commit=local_commit,
        remote_commit=remote_commit,
        ahead=ahead,
        behind=behind,
        state=state,
        update_available=behind > 0,
        dirty=dirty,
    )


def check_upgrade(
    project_root: Path | None = None,
    runner: CommandRunner | None = None,
) -> UpgradeReport:
    """Return a machine-readable update check without changing tracked files."""
    root = project_root if project_root is not None else discover_project_root()
    if root is None:
        return _package_report("check")
    state = inspect_source_upgrade(root, runner)
    detail = {
        "up_to_date": "Browser MCP is up to date.",
        "behind": f"{state.behind} upstream commit(s) are available.",
        "ahead": f"The checkout is {state.ahead} commit(s) ahead of upstream.",
        "diverged": "The checkout and upstream have diverged; automatic upgrade is blocked.",
    }[state.state]
    if state.dirty:
        detail += " The worktree is dirty, so --apply will refuse to continue."
    return _report_from_state(state, "check", detail=detail)


def apply_upgrade(
    project_root: Path | None = None,
    runner: CommandRunner | None = None,
) -> UpgradeReport:
    """Fast-forward a clean source checkout and synchronize locked dependencies."""
    root = project_root if project_root is not None else discover_project_root()
    if root is None:
        return _package_report("apply")
    command_runner = runner or SubprocessCommandRunner()
    state = inspect_source_upgrade(root, command_runner)
    if state.dirty:
        raise UpgradeError("refusing to upgrade a dirty worktree; commit or stash changes first")
    if state.state == "diverged":
        raise UpgradeError("refusing to upgrade a diverged branch; reconcile it manually first")

    resolved = root.resolve()
    remote, _ = state.upstream.split("/", 1)
    merge_ref = command_runner(
        ("git", "config", "--get", f"branch.{state.branch}.merge"),
        resolved,
    )
    if state.behind:
        command_runner(("git", "pull", "--ff-only", remote, merge_ref), resolved)
    command_runner(("uv", "sync", "--frozen"), resolved)
    current_commit = command_runner(("git", "rev-parse", "HEAD"), resolved)
    changed = current_commit != state.local_commit
    final_state = SourceUpgradeState(
        project_root=state.project_root,
        branch=state.branch,
        upstream=state.upstream,
        local_commit=current_commit,
        remote_commit=current_commit if changed else state.remote_commit,
        ahead=state.ahead if not changed else 0,
        behind=0 if changed else state.behind,
        state="ahead" if state.state == "ahead" else "up_to_date",
        update_available=False if changed else state.update_available,
        dirty=False,
    )
    detail = (
        "Upgrade applied. Restart Codex once; the Chrome extension will reload automatically."
        if changed
        else "No source update was needed; locked dependencies are synchronized."
    )
    return _report_from_state(
        final_state,
        "apply",
        changed=changed,
        dependencies_synced=True,
        restart_required=changed,
        detail=detail,
    )


def render_upgrade_report(report: UpgradeReport, *, as_json: bool) -> str:
    """Render a stable JSON result or a compact human-readable summary."""
    if as_json:
        return report.to_json()
    lines = [report.detail]
    if report.project_root:
        lines.append(f"Project: {report.project_root}")
    if report.local_commit:
        lines.append(f"Commit: {report.local_commit[:12]}")
    lines.append(f"State: {report.state}")
    return "\n".join(lines)


def error_report(action: Literal["check", "apply"], error: Exception) -> UpgradeReport:
    """Convert a safe upgrade failure into the same stable report envelope."""
    metadata = installation_metadata()
    return UpgradeReport(
        ok=False,
        action=action,
        server_version=metadata.server_version,
        install_mode=metadata.install_mode,
        project_root=metadata.project_root,
        branch=None,
        upstream=None,
        local_commit=metadata.source_commit,
        remote_commit=None,
        ahead=None,
        behind=None,
        state="blocked",
        update_available=None,
        dirty=None,
        changed=False,
        dependencies_synced=False,
        restart_required=False,
        extension_reload_automatic=True,
        detail=str(error),
    )


def _report_from_state(
    state: SourceUpgradeState,
    action: Literal["check", "apply"],
    *,
    changed: bool = False,
    dependencies_synced: bool = False,
    restart_required: bool = False,
    detail: str,
) -> UpgradeReport:
    """Map source state into the public CLI report."""
    return UpgradeReport(
        ok=True,
        action=action,
        server_version=__version__,
        install_mode="source",
        project_root=state.project_root,
        branch=state.branch,
        upstream=state.upstream,
        local_commit=state.local_commit,
        remote_commit=state.remote_commit,
        ahead=state.ahead,
        behind=state.behind,
        state=state.state,
        update_available=state.update_available,
        dirty=state.dirty,
        changed=changed,
        dependencies_synced=dependencies_synced,
        restart_required=restart_required,
        extension_reload_automatic=True,
        detail=detail,
    )


def _package_report(action: Literal["check", "apply"]) -> UpgradeReport:
    """Explain why a package-managed installation cannot mutate itself safely."""
    return UpgradeReport(
        ok=False,
        action=action,
        server_version=__version__,
        install_mode="package",
        project_root=None,
        branch=None,
        upstream=None,
        local_commit=None,
        remote_commit=None,
        ahead=None,
        behind=None,
        state="unsupported",
        update_available=None,
        dirty=None,
        changed=False,
        dependencies_synced=False,
        restart_required=False,
        extension_reload_automatic=True,
        detail=(
            "This is a packaged installation. Upgrade browser-mcp with the package manager "
            "that installed it, then restart Codex once."
        ),
    )


def _require_source_checkout(project_root: Path) -> None:
    """Reject arbitrary directories before any Git network operation."""
    if not (project_root / ".git").exists():
        raise UpgradeError(f"not a Git checkout: {project_root}")
    if not (project_root / "pyproject.toml").is_file():
        raise UpgradeError(f"Browser MCP pyproject.toml not found: {project_root}")
    if not (project_root / "src" / "browser_mcp").is_dir():
        raise UpgradeError(f"Browser MCP source package not found: {project_root}")


def _format_command(args: Sequence[str]) -> str:
    """Format one argv sequence for the current operating system."""
    values = [str(value) for value in args]
    if os.name == "nt":
        return subprocess.list2cmdline(values)
    return shlex.join(values)
