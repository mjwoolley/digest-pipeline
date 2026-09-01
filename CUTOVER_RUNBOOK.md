# Cutover Runbook — Digest Pipeline Redesign → Prod

**Status when this was written:** the redesign is fully merged to `origin/master`
(merge commit `e48a4bc`, 2026-08-15) and the multi-day prod-parallel staging
trial is finished. What remains is server-side work on the mini server / homelab
box that hosts the two checkouts. This file is written to be executed by a
Claude session on that machine — every step has a verification, and nothing
here should require force flags or destructive git operations.

Delete this file (commit "Remove cutover runbook") once every step is checked
off, or leave it as a record.

---

## Preconditions (verify before touching anything)

```bash
# The two checkouts exist where expected:
ls -d ~/digest-pipeline ~/digest-pipeline-staging

# origin/master tip is the redesign merge:
cd ~/digest-pipeline && git fetch origin && git log -1 --oneline origin/master
# EXPECT: e48a4bc Digest pipeline redesign: dedup overhaul, Opus 5 writing stages, bug fixes
```

If `origin/master` is NOT `e48a4bc` (or a descendant of it), stop and surface
to the user — something moved.

## Step 1 — Stop the parallel-comparison crons

```bash
crontab -l > ~/crontab.backup.$(date +%Y%m%d)   # backup first
crontab -e
```

Delete exactly these two lines (added for the staging trial):

```cron
5 3 * * *  cd ~/digest-pipeline-staging && DIGEST_ENV=staging bash run.sh digests/ai/config.json >> logs/cron.log 2>&1
30 4 * * * cd ~/digest-pipeline-staging && DIGEST_ENV=staging .venv/bin/python3 scripts/compare_digests.py ~/digest-pipeline/digests/ai digests/ai --llm-judge --notify digests/ai/config.json >> logs/compare.log 2>&1
```

**Do NOT touch the original prod cron line** (the 3:00 AM ET `~/digest-pipeline`
run). Verify: `crontab -l` shows only the prod line(s), no `digest-pipeline-staging`
entries.

## Step 2 — Promote prod

```bash
cd ~/digest-pipeline
git status
```

**If the working tree is dirty: STOP and surface the diff to the user** — never
pull on top of uncommitted prod changes (CLAUDE.md rule). If clean:

```bash
git pull
git log -1 --oneline          # EXPECT: e48a4bc (or later)
```

Seed the new cross-day dedup state (shipped-URL index; embeddings backfill is
idempotent and will mostly no-op) from prod's own archives — once per digest:

```bash
.venv/bin/digest-pipeline digests/ai/config.json --backfill
.venv/bin/digest-pipeline digests/wealthtech/config.json --backfill
```

EXPECT each to print a "Backfilling .shipped_urls.json" section with a
non-zero URL count (ai was ~200 in testing). Verify the files exist:

```bash
ls -la digests/ai/.shipped_urls.json digests/wealthtech/.shipped_urls.json
```

Note: no new runtime dependencies were added — the existing `.venv` works
as-is. `pytest` is dev-only.

## Step 3 — Reset staging back to master

The staging checkout is currently on the feature branch. Per CLAUDE.md it
should track master between projects:

```bash
cd ~/digest-pipeline-staging
git status                     # if dirty: stop and surface to the user
git checkout master
git pull --ff-only origin master
git log -1 --oneline           # EXPECT: e48a4bc (or later)
```

## Step 4 — Delete the merged feature branch

Everything in it is in master now.

```bash
cd ~/digest-pipeline-staging
git branch -d claude/digest-pipeline-redesign-rsxppa      # local (-d refuses if unmerged — good)
git push origin --delete claude/digest-pipeline-redesign-rsxppa
```

## Step 5 — Verify the first prod run (next morning, or trigger manually)

To verify immediately instead of waiting for cron:

```bash
cd ~/digest-pipeline && bash run.sh digests/ai/config.json
```

Check, in the Telegram notification / `digests/ai/<date>.md` token footer /
`digests/ai/work/<date>/run.json`:

- **Models**: `Format` and `Dedupe` lines show `anthropic/claude-opus-5`;
  `Extract`/`Prioritize` still show `anthropic/claude-haiku-4.5`.
- **New notification lines**: `Sources with content N/M` and
  `Cross-day dupes skipped: N` are present.
- **Cost**: total in the ~$0.15–0.35 range (busy days higher). If it's
  wildly above, check `run.json` per-stage tokens before reacting.
- **Dedup working**: `work/<date>/cross_deduped.json` exists;
  `cross_skipped.json` lists anything suppressed, with reasons.
- Email arrives to prod subscribers and renders correctly (spot-check
  an article with `&` or quotes in the title if there is one).

## Rollback (if the first prod runs go badly)

The whole redesign is one merge commit, so backing it out is clean:

```bash
cd ~/digest-pipeline-staging          # develop the revert in staging per CLAUDE.md
git revert -m 1 e48a4bc
git push origin master
cd ~/digest-pipeline && git status && git pull
```

State files written by the new code (`.shipped_urls.json`, the extra keys in
`.seen_embeddings.json`) are ignored by the old code — no state cleanup needed
on rollback.

## Checklist

- [ ] Crontab backed up; two staging-trial lines removed; prod line intact
- [ ] Prod: clean tree → pulled to `e48a4bc`+ → both digests backfilled
- [ ] Staging back on master
- [ ] Feature branch deleted (local + origin)
- [ ] First prod run verified (models, dedup line, cost, email)
- [ ] This runbook deleted or archived
