"""Tests for the safe, agent-friendly Browser MCP upgrade workflow."""

from __future__ import annotations

from pathlib import Path

import pytest

from browser_mcp.upgrade import (
    UpgradeError,
    apply_upgrade,
    check_upgrade,
    installation_metadata,
)


class FakeUpgradeRunner:
    """Deterministic Git and uv command adapter for upgrade unit tests."""

    def __init__(
        self,
        *,
        local_commit: str = "a" * 40,
        remote_commit: str = "b" * 40,
        ahead: int = 0,
        behind: int = 2,
        dirty: bool = False,
    ) -> None:
        """Configure one synthetic checkout state."""
        self.local_commit = local_commit
        self.remote_commit = remote_commit
        self.ahead = ahead
        self.behind = behind
        self.dirty = dirty
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: tuple[str, ...], cwd: Path) -> str:
        """Return command output and apply the expected pull side effect."""
        del cwd
        self.calls.append(args)
        if args[:3] == ("git", "status", "--porcelain=v1"):
            return " M local-change.py" if self.dirty else ""
        if args[:4] == ("git", "symbolic-ref", "--quiet", "--short"):
            return "main"
        if args[:3] == ("git", "rev-parse", "--abbrev-ref"):
            return "origin/main"
        if args[:3] == ("git", "config", "--get"):
            return "refs/heads/main"
        if args == ("git", "rev-parse", "HEAD"):
            return self.local_commit
        if args == ("git", "rev-parse", "FETCH_HEAD"):
            return self.remote_commit
        if args == ("git", "rev-parse", "--short", "HEAD"):
            return self.local_commit[:7]
        if args[:3] == ("git", "fetch", "--quiet"):
            return ""
        if args[:4] == ("git", "rev-list", "--left-right", "--count"):
            return f"{self.ahead}\t{self.behind}"
        if args[:3] == ("git", "pull", "--ff-only"):
            self.local_commit = self.remote_commit
            self.ahead = 0
            self.behind = 0
            return "Fast-forward"
        if args == ("uv", "sync", "--frozen"):
            return "Resolved"
        raise AssertionError(f"unexpected command: {args}")


def source_checkout(tmp_path: Path) -> Path:
    """Create only the source markers required before command execution."""
    (tmp_path / ".git").mkdir(parents=True)
    (tmp_path / "src" / "browser_mcp").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='browser-mcp'\n")
    return tmp_path


def test_installation_metadata_exposes_absolute_agent_commands(tmp_path: Path) -> None:
    """Status metadata should let an Agent copy one unambiguous command."""
    root = source_checkout(tmp_path / "Browser MCP checkout")
    runner = FakeUpgradeRunner(local_commit="1" * 40, remote_commit="1" * 40, behind=0)

    metadata = installation_metadata(root, runner)

    assert metadata.install_mode == "source"
    assert metadata.project_root == str(root.resolve())
    assert metadata.source_commit == "1111111"
    assert metadata.upgrade_check_command is not None
    assert "--check --json" in metadata.upgrade_check_command
    assert str(root.resolve()) in metadata.upgrade_check_command
    assert metadata.upgrade_apply_command is not None
    assert "--apply --json" in metadata.upgrade_apply_command


def test_check_upgrade_reports_remote_and_dirty_state(tmp_path: Path) -> None:
    """A check may inspect a dirty checkout but must warn that apply will refuse it."""
    root = source_checkout(tmp_path)
    runner = FakeUpgradeRunner(ahead=0, behind=3, dirty=True)

    report = check_upgrade(root, runner)

    assert report.ok is True
    assert report.state == "behind"
    assert report.update_available is True
    assert report.behind == 3
    assert report.dirty is True
    assert "--apply will refuse" in report.detail
    assert ("git", "fetch", "--quiet", "--no-tags", "origin", "refs/heads/main") in runner.calls


def test_apply_upgrade_fast_forwards_and_syncs_locked_dependencies(tmp_path: Path) -> None:
    """A clean behind checkout should update without merge commits or lockfile drift."""
    root = source_checkout(tmp_path)
    runner = FakeUpgradeRunner(ahead=0, behind=2)

    report = apply_upgrade(root, runner)

    assert report.ok is True
    assert report.changed is True
    assert report.dependencies_synced is True
    assert report.restart_required is True
    assert report.update_available is False
    assert report.local_commit == "b" * 40
    assert ("git", "pull", "--ff-only", "origin", "refs/heads/main") in runner.calls
    assert ("uv", "sync", "--frozen") in runner.calls


def test_apply_upgrade_refuses_dirty_worktree(tmp_path: Path) -> None:
    """Automatic upgrades must preserve every uncommitted user file."""
    root = source_checkout(tmp_path)
    runner = FakeUpgradeRunner(dirty=True)

    with pytest.raises(UpgradeError, match="dirty worktree"):
        apply_upgrade(root, runner)

    assert not any(call[:2] == ("git", "pull") for call in runner.calls)
    assert ("uv", "sync", "--frozen") not in runner.calls


def test_apply_upgrade_refuses_diverged_branch(tmp_path: Path) -> None:
    """A non-fast-forward checkout must require manual reconciliation."""
    root = source_checkout(tmp_path)
    runner = FakeUpgradeRunner(ahead=1, behind=1)

    with pytest.raises(UpgradeError, match="diverged"):
        apply_upgrade(root, runner)

    assert not any(call[:2] == ("git", "pull") for call in runner.calls)
