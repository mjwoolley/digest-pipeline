"""Tests for digest_pipeline.archive — artifact resolution and git publish logic."""
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

from digest_pipeline.archive import (
    resolve_artifacts,
    find_repo_root,
    git_publish_daily,
    DEFAULT_ARTIFACTS,
)


# ── resolve_artifacts ─────────────────────────────────────────────────────────

def test_resolve_artifacts_returns_existing_files(tmp_path):
    """Only returns paths that exist on disk."""
    (tmp_path / "2026-03-17.md").write_text("digest")
    (tmp_path / "podcast.xml").write_text("<rss/>")

    result = resolve_artifacts(tmp_path, "2026-03-17", [
        "{date}.md",
        "podcasts/{date}.mp3",  # does not exist
        "podcast.xml",
    ])
    assert len(result) == 2
    assert tmp_path / "2026-03-17.md" in result
    assert tmp_path / "podcast.xml" in result


def test_resolve_artifacts_empty_when_nothing_exists(tmp_path):
    result = resolve_artifacts(tmp_path, "2026-03-17", ["{date}.md"])
    assert result == []


def test_resolve_artifacts_date_substitution(tmp_path):
    podcasts = tmp_path / "podcasts"
    podcasts.mkdir()
    (podcasts / "2026-01-01.mp3").write_bytes(b"fake mp3")

    result = resolve_artifacts(tmp_path, "2026-01-01", ["podcasts/{date}.mp3"])
    assert len(result) == 1
    assert result[0].name == "2026-01-01.mp3"


def test_resolve_artifacts_uses_defaults_when_no_patterns(tmp_path):
    (tmp_path / "2026-03-17.md").write_text("digest")
    result = resolve_artifacts(tmp_path, "2026-03-17")
    # Only the .md exists, rest of defaults don't
    assert len(result) == 1
    assert result[0].name == "2026-03-17.md"


def test_resolve_artifacts_all_defaults(tmp_path):
    """All default artifacts found when they exist."""
    date = "2026-03-17"
    (tmp_path / f"{date}.md").write_text("digest")
    podcasts = tmp_path / "podcasts"
    podcasts.mkdir()
    (podcasts / f"{date}.mp3").write_bytes(b"audio")
    (podcasts / f"{date}.txt").write_text("script")
    (tmp_path / "podcast.xml").write_text("<rss/>")
    (tmp_path / "index.html").write_text("<html/>")
    (tmp_path / ".seen_embeddings.json").write_text("{}")

    result = resolve_artifacts(tmp_path, date)
    assert len(result) == len(DEFAULT_ARTIFACTS)


# ── find_repo_root ────────────────────────────────────────────────────────────

def test_find_repo_root_in_git_repo(tmp_path):
    """Returns repo root when inside a git repo."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    root = find_repo_root(tmp_path)
    assert root is not None
    assert root == tmp_path


def test_find_repo_root_outside_git_repo(tmp_path):
    """Returns None when not in a git repo."""
    # tmp_path is not a git repo, but it might be inside one on CI
    # So we mock subprocess to return failure
    with patch("digest_pipeline.archive.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        root = find_repo_root(tmp_path)
        assert root is None


# ── git_publish_daily ─────────────────────────────────────────────────────────

def test_git_publish_disabled_by_default(tmp_path):
    """Archive is disabled when config has no archive section."""
    config = {"_data_root": tmp_path}
    assert git_publish_daily(config, date="2026-03-17") is False


def test_git_publish_disabled_explicitly(tmp_path):
    config = {"_data_root": tmp_path, "archive": {"enabled": False}}
    assert git_publish_daily(config, date="2026-03-17") is False


def test_git_publish_no_artifacts(tmp_path):
    config = {
        "_data_root": tmp_path,
        "archive": {"enabled": True, "push": False},
    }
    with patch("digest_pipeline.archive.find_repo_root", return_value=tmp_path):
        assert git_publish_daily(config, date="2026-03-17") is False


def test_git_publish_dry_run(tmp_path):
    (tmp_path / "2026-03-17.md").write_text("digest content")
    config = {
        "_data_root": tmp_path,
        "archive": {"enabled": True, "artifacts": ["{date}.md"]},
    }
    with patch("digest_pipeline.archive.find_repo_root", return_value=tmp_path):
        result = git_publish_daily(config, date="2026-03-17", dry_run=True)
    assert result is True


def test_git_publish_commits_and_pushes(tmp_path):
    """Full flow: stage, commit, push."""
    (tmp_path / "2026-03-17.md").write_text("digest content")
    config = {
        "_data_root": tmp_path,
        "archive": {
            "enabled": True,
            "push": True,
            "commit_message": "Archive {date}",
            "artifacts": ["{date}.md"],
        },
    }

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        mock = MagicMock()
        # git diff --cached --quiet returns 1 (changes exist)
        if cmd[:3] == ["git", "diff", "--cached"]:
            mock.returncode = 1
        else:
            mock.returncode = 0
            mock.stdout = str(tmp_path)
        return mock

    with patch("digest_pipeline.archive.find_repo_root", return_value=tmp_path), \
         patch("digest_pipeline.archive.subprocess.run", side_effect=fake_run), \
         patch("digest_pipeline.archive.delivery"):
        result = git_publish_daily(config, date="2026-03-17")

    assert result is True
    # Verify git add, git diff --cached, git commit, git push were called
    git_cmds = [c[1] for c in calls if c[0] == "git"]
    assert "add" in git_cmds
    assert "diff" in git_cmds
    assert "commit" in git_cmds
    assert "push" in git_cmds


def test_git_publish_no_push_when_disabled(tmp_path):
    """push=False skips git push."""
    (tmp_path / "2026-03-17.md").write_text("digest content")
    config = {
        "_data_root": tmp_path,
        "archive": {
            "enabled": True,
            "push": False,
            "artifacts": ["{date}.md"],
        },
    }

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        mock = MagicMock()
        if cmd[:3] == ["git", "diff", "--cached"]:
            mock.returncode = 1
        else:
            mock.returncode = 0
            mock.stdout = str(tmp_path)
        return mock

    with patch("digest_pipeline.archive.find_repo_root", return_value=tmp_path), \
         patch("digest_pipeline.archive.subprocess.run", side_effect=fake_run):
        result = git_publish_daily(config, date="2026-03-17")

    assert result is True
    git_cmds = [c[1] for c in calls if c[0] == "git"]
    assert "push" not in git_cmds


def test_git_publish_custom_commit_message(tmp_path):
    """Custom commit_message template is expanded with {date}."""
    (tmp_path / "2026-03-17.md").write_text("digest content")
    config = {
        "_data_root": tmp_path,
        "archive": {
            "enabled": True,
            "push": False,
            "commit_message": "Publish daily digest {date}",
            "artifacts": ["{date}.md"],
        },
    }

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        mock = MagicMock()
        if cmd[:3] == ["git", "diff", "--cached"]:
            mock.returncode = 1
        else:
            mock.returncode = 0
        return mock

    with patch("digest_pipeline.archive.find_repo_root", return_value=tmp_path), \
         patch("digest_pipeline.archive.subprocess.run", side_effect=fake_run):
        git_publish_daily(config, date="2026-03-17")

    commit_calls = [c for c in calls if len(c) >= 4 and c[1] == "commit"]
    assert len(commit_calls) == 1
    assert commit_calls[0][3] == "Publish daily digest 2026-03-17"


def test_git_publish_no_changes_to_commit(tmp_path):
    """Returns False when git diff --cached shows no changes."""
    (tmp_path / "2026-03-17.md").write_text("digest")
    config = {
        "_data_root": tmp_path,
        "archive": {
            "enabled": True,
            "push": False,
            "artifacts": ["{date}.md"],
        },
    }

    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        # git diff --cached --quiet returns 0 (no changes)
        mock.returncode = 0
        mock.stdout = str(tmp_path)
        return mock

    with patch("digest_pipeline.archive.find_repo_root", return_value=tmp_path), \
         patch("digest_pipeline.archive.subprocess.run", side_effect=fake_run):
        assert git_publish_daily(config, date="2026-03-17") is False


def test_git_publish_custom_branch(tmp_path):
    """branch config adds 'origin <branch>' to push command."""
    (tmp_path / "2026-03-17.md").write_text("digest")
    config = {
        "_data_root": tmp_path,
        "archive": {
            "enabled": True,
            "push": True,
            "branch": "gh-pages",
            "artifacts": ["{date}.md"],
        },
    }

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        mock = MagicMock()
        if cmd[:3] == ["git", "diff", "--cached"]:
            mock.returncode = 1
        else:
            mock.returncode = 0
            mock.stdout = str(tmp_path)
        return mock

    with patch("digest_pipeline.archive.find_repo_root", return_value=tmp_path), \
         patch("digest_pipeline.archive.subprocess.run", side_effect=fake_run), \
         patch("digest_pipeline.archive.delivery"):
        git_publish_daily(config, date="2026-03-17")

    push_calls = [c for c in calls if len(c) >= 2 and c[1] == "push"]
    assert len(push_calls) == 1
    assert push_calls[0] == ["git", "push", "origin", "gh-pages"]
