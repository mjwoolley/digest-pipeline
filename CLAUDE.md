# CLAUDE.md

Quick orientation for AI agents working in this repo. **For full project documentation (install, usage, configuration, deployment), see [README.md](README.md).** This file only covers what an agent needs to know that isn't obvious from the code or README.

## Development workflow

There are two checkouts of this repo on the VPS:

- `~/digest-pipeline` — **production**, cron-driven, sends real digests, runs the daily archive cron.
- `~/digest-pipeline-staging` — **development sandbox**, manual runs only, isolated subscribers, staging subdomains, no archive commits.

**Always develop in staging, and always sync staging with `origin/master` before starting.** The two checkouts share a remote, and master can move forward from elsewhere (e.g. work committed directly in prod, or another machine). Branching off a stale local master is what causes painful merges later.

The flow:

```bash
# 1. Sync staging with origin first
cd ~/digest-pipeline-staging
git fetch origin
git status                              # confirm working tree is clean
git pull --ff-only origin master        # fast-forward only; aborts if master diverged

# 2. Branch, edit, commit logically grouped changes
git checkout -b my-feature
# ... edits + commits ...

# 3. Merge locally with a real merge commit, push, clean up
git checkout master
git merge --no-ff my-feature            # opens editor for the merge commit message
git push origin master
git branch -d my-feature

# 4. Promote to prod
cd ~/digest-pipeline
git status                              # abort and surface to the user if dirty
git pull
```

**Rules:**

- No PRs — single contributor, the GitHub PR step is unnecessary ceremony. The branch + merge-commit pattern alone gives clean history and a clean revert path.
- `--no-ff` always. The merge commit groups the feature into one logical unit; `git revert -m 1 <merge-sha>` then backs out the whole thing cleanly.
- Group commits on the branch logically (one commit per concern, not one big "everything" commit).
- Merge commit message must include a **Why** section — the motivation, not just the diff. Format:
  ```
  Brief feature title

  Why: <motivation — the problem, the ask, the constraint>
  What: <high-level summary of the change>
  ```
- Co-author trailer on the merge commit: `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`
- Before `git pull` in prod, always run `git status` first. If dirty, stop and surface to the user — never pull on top of uncommitted changes.

If a session starts in `~/digest-pipeline` and the user asks for a non-trivial change, switch to `~/digest-pipeline-staging` before editing. The only exception is a one-line fix the user explicitly asks to apply in prod.

## Architecture map

Pipeline orchestrator: `digest_pipeline/digest.py`. CLI entry: `digest_pipeline/cli.py`.

For the full module-by-module breakdown and the 10-stage pipeline flow, see [Project structure](README.md#project-structure) and [How it works](README.md#how-it-works) in README. Don't duplicate that here — it's authoritative there.

## Gotchas

- **Entry point for systemd / scripts:** use `.venv/bin/digest-pipeline` (the installed console script), not `python -m digest_pipeline.cli`. There is no `__main__.py` for the package.
- **Staging requires `DIGEST_ENV=staging`** when running manually. Without it, the run uses prod values from the base config — the staging overlay only applies when the env var is set. The systemd unit sets it; for shell runs, do it explicitly: `DIGEST_ENV=staging bash run.sh digests/ai/config.json`.
- **Staging's `digests/*/index.html` drifts after every podcast run.** The landing-page rewrite happens on every podcast generation, but staging has no archive cron to commit/discard the result. Run `git checkout -- digests/*/index.html` before `git pull` in staging if `git status` shows it dirty.
