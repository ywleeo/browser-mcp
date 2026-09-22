"""Tests for the local-file policy applied before Chrome reads an upload path."""

from pathlib import Path

import pytest

from browser_mcp.security import LocalFilePolicy, UploadPolicyError


def test_policy_resolves_relative_and_user_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accepted paths come back absolute so Chrome never resolves them itself."""
    home = tmp_path / "home"
    home.mkdir()
    picture = home / "poster.png"
    picture.write_bytes(b"png-bytes")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(home)

    policy = LocalFilePolicy()

    assert policy.resolve(["poster.png"]) == (str(picture.resolve()),)
    assert policy.resolve(["~/poster.png"]) == (str(picture.resolve()),)


def test_policy_follows_symlinks_before_authorizing(tmp_path: Path) -> None:
    """A symlink is judged by its target so it cannot smuggle a refused path through."""
    secrets = tmp_path / ".ssh"
    secrets.mkdir()
    key = secrets / "deploy_key"
    key.write_text("private")
    link = tmp_path / "harmless.png"
    link.symlink_to(key)

    with pytest.raises(UploadPolicyError, match="credential directory"):
        LocalFilePolicy().resolve([str(link)])


@pytest.mark.parametrize("name", [".ssh", ".aws", ".gnupg", ".kube", ".docker"])
def test_policy_refuses_credential_directories(tmp_path: Path, name: str) -> None:
    """Credential stores stay unreachable no matter which page asked for the upload."""
    directory = tmp_path / name
    directory.mkdir()
    target = directory / "config"
    target.write_text("secret")

    with pytest.raises(UploadPolicyError, match="credential directory"):
        LocalFilePolicy().resolve([str(target)])


@pytest.mark.parametrize("name", [".env", ".netrc", "id_rsa", "credentials"])
def test_policy_refuses_credential_files(tmp_path: Path, name: str) -> None:
    """Well-known secret filenames are refused even outside a credential directory."""
    target = tmp_path / name
    target.write_text("secret")

    with pytest.raises(UploadPolicyError, match="credential file"):
        LocalFilePolicy().resolve([str(target)])


def test_policy_rejects_missing_paths_and_directories(tmp_path: Path) -> None:
    """Only existing regular files reach the extension."""
    policy = LocalFilePolicy()

    with pytest.raises(UploadPolicyError, match="does not exist"):
        policy.resolve([str(tmp_path / "absent.png")])
    with pytest.raises(UploadPolicyError, match="not a regular file"):
        policy.resolve([str(tmp_path)])
    with pytest.raises(UploadPolicyError, match="must not be empty"):
        policy.resolve(["   "])


def test_policy_enforces_count_and_size_limits(tmp_path: Path) -> None:
    """Per-file, total, and count limits each stop an oversized upload."""
    small = tmp_path / "small.png"
    small.write_bytes(b"x" * 10)
    large = tmp_path / "large.png"
    large.write_bytes(b"x" * 100)

    with pytest.raises(UploadPolicyError, match="at most 1 files"):
        LocalFilePolicy(max_files=1).resolve([str(small), str(small)])
    with pytest.raises(UploadPolicyError, match="byte upload limit"):
        LocalFilePolicy(max_file_bytes=50).resolve([str(large)])
    with pytest.raises(UploadPolicyError, match="total limit"):
        LocalFilePolicy(max_total_bytes=150).resolve([str(large), str(large)])


def test_policy_requires_at_least_one_path() -> None:
    """An empty request is rejected before any extension round trip."""
    with pytest.raises(UploadPolicyError, match="at least one file path"):
        LocalFilePolicy().resolve([])
