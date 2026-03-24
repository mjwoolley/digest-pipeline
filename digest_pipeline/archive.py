"""Archive & publish: commit and push daily artifacts to git after a successful run.

Uses an allowlist of artifact patterns (relative to data_root) to avoid
committing temp/runtime files. Patterns support {date} substitution.
"""
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import delivery

logger = logging.getLogger("archive")

# Default artifacts for the AI Daily Roundup workflow
DEFAULT_ARTIFACTS = [
    "{date}.md",                  # daily digest markdown
    "podcasts/{date}.mp3",        # podcast episode audio
    "podcasts/{date}.txt",        # podcast script
    "podcast.xml",                # RSS feed
    "index.html",                 # landing page
    ".seen_embeddings.json",      # cross-day dedup state
]


def resolve_artifacts(data_root: Path, date: str,
                      patterns: list[str] | None = None) -> list[Path]:
    """Expand artifact patterns and return paths that exist on disk.

    Args:
        data_root: Base directory for digest outputs (e.g. digests/ai/).
        date: Date string like "2026-03-17".
        patterns: Glob-like patterns relative to data_root.
                  May contain {date} placeholder.
    """
    if patterns is None:
        patterns = DEFAULT_ARTIFACTS

    found = []
    for pattern in patterns:
        expanded = pattern.replace("{date}", date)
        path = data_root / expanded
        if path.exists():
            found.append(path)
    return found


def find_repo_root(data_root: Path) -> Path | None:
    """Walk up from data_root to find the git repo root."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=data_root, capture_output=True, text=True, timeout=10,
    )
    if result.returncode == 0:
        return Path(result.stdout.strip())
    return None


def git_publish_daily(config: dict, date: str | None = None,
                      dry_run: bool = False) -> bool:
    """Commit and optionally push daily artifacts to git.

    Args:
        config: Full pipeline config dict.
        date: Date string (defaults to today UTC).
        dry_run: If True, log what would be committed but don't act.

    Returns:
        True if changes were committed (or would have been in dry_run).
    """
    archive_cfg = config.get("archive", {})
    if not archive_cfg.get("enabled", False):
        logger.info("[ARCHIVE] Disabled in config, skipping")
        return False

    if date is None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    data_root = config["_data_root"]
    repo_root = find_repo_root(data_root)
    if repo_root is None:
        logger.warning("[ARCHIVE] Not inside a git repository, skipping")
        return False

    # Resolve which artifacts exist
    patterns = archive_cfg.get("artifacts", DEFAULT_ARTIFACTS)
    artifacts = resolve_artifacts(data_root, date, patterns)

    if not artifacts:
        logger.info("[ARCHIVE] No artifacts found for %s", date)
        return False

    rel_paths = [str(f.relative_to(repo_root)) for f in artifacts]
    logger.info("[ARCHIVE] Artifacts to stage: %s", rel_paths)

    if dry_run:
        logger.info("[ARCHIVE] DRY RUN — would commit: %s", rel_paths)
        return True

    # Stage files
    subprocess.run(
        ["git", "add"] + rel_paths,
        cwd=repo_root, capture_output=True, text=True, check=True, timeout=30,
    )

    # Check if there are staged changes
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=repo_root, capture_output=True, timeout=10,
    )
    if result.returncode == 0:
        logger.info("[ARCHIVE] No changes to commit")
        return False

    # Commit
    commit_msg_template = archive_cfg.get(
        "commit_message", "Daily archive: {date}")
    commit_msg = commit_msg_template.replace("{date}", date)

    subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=repo_root, capture_output=True, text=True, check=True, timeout=30,
    )
    logger.info("[ARCHIVE] Committed: %s", commit_msg)

    # Push (if configured)
    if archive_cfg.get("push", True):
        branch = archive_cfg.get("branch")
        push_cmd = ["git", "push"]
        if branch:
            push_cmd += ["origin", branch]

        push_result = subprocess.run(
            push_cmd,
            cwd=repo_root, capture_output=True, text=True, timeout=60,
        )
        if push_result.returncode == 0:
            logger.info("[ARCHIVE] Pushed to remote")
        else:
            logger.error("[ARCHIVE] Push failed: %s",
                         push_result.stderr[:300])
            delivery.send_alert("ARCHIVE",
                                f"Git push failed: {push_result.stderr[:200]}",
                                config)
    else:
        logger.info("[ARCHIVE] Push disabled, committed locally only")

    return True
