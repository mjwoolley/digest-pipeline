# Cutover Record — Digest Pipeline Redesign → Prod

**Completed 2026-09-02.** The redesign (merge `e48a4bc`) is live in prod; the
prod-parallel staging trial is over and its crons are removed. This file is kept
as a record rather than deleted, because the original runbook was written against
an older VPS/systemd/`.venv` deployment and **most of its commands did not work on
the current Docker host**. The corrections below apply to the next promotion too.

## What this host actually looks like

Everything runs in Docker on the Beelink mini-PC. **There is no `.venv` on the
host** — any `.venv/bin/digest-pipeline` or bare `bash run.sh` instruction is
wrong here. The repo checkouts are bind-mounted into the containers at `/app`
(see `~/digest/compose.prod.yml`), so `git pull` is what promotes code; deps are
baked into the `digest-pipeline:local` image.

Run one-off commands through the batch container:

```bash
docker compose -f ~/digest/compose.prod.yml run --rm digest-batch \
  digest-pipeline /app/digests/ai/config.json --backfill
```

Cron does the same via `~/digest/scripts/run-batch.sh`, under a shared
`/tmp/digest-batch.lock` and a 5g memory ceiling.

## Four things the original runbook got wrong

1. **`.venv` commands.** See above — use `docker compose run --rm digest-batch`.
2. **The crontab lines to remove.** The runbook named two `cd
   ~/digest-pipeline-staging && DIGEST_ENV=staging …` lines. Reality was **three**
   Compose lines using a UTC schedule with an `America/New_York` guard (Debian
   Vixie cron ignores `CRON_TZ` and the host is UTC): staging digest 04:00 ET,
   `compare_digests.py` 05:00 ET, `deep_compare.py` 05:15 ET. All three removed;
   crontab backed up to `~/crontab.backup.20260902` first.
3. **Restarting the long-running services was missing.** `digest-subscriptions`
   and `digest-console` are Flask processes holding the bind-mounted code in
   memory — `git pull` alone does not promote them. Both must be recreated. And
   `digest-caddy` uses `network_mode: "service:digest-subscriptions"`, so **it
   must be recreated after them** or it loses its netns and the public site 502s:

   ```bash
   docker compose -f ~/digest/compose.prod.yml up -d --force-recreate \
     digest-subscriptions digest-console
   docker compose -f ~/digest/compose.prod.yml up -d --force-recreate caddy
   ```

4. **Backfill counts.** The runbook predicted "~200" for ai from staging's
   archive. Prod: **213** URLs for ai, **172** for wealthtech. The check is
   non-zero, not a specific number.

## What ran

- Crontab backed up; three staging-trial lines removed; 7 prod lines intact.
- Prod pulled to `a785e1d`; `--backfill` run for both digests (`.shipped_urls.json`
  seeded; embeddings already current, so that half no-oped as designed).
  The wealthtech dedup simulation retro-flagged 2 real repeats on 2026-08-31.
- Prod services + caddy recreated; `/health` 200, console 200.
- Staging returned to `master`; `claude/digest-pipeline-redesign-rsxppa` deleted
  local and remote.
- Test suite: 483 passing in the prod container.

## Bug found and fixed during the cutover

Prod state backups had been **silently failing since 2026-06-25** (69 days).
`~/bin/backup-state.sh` runs `set -euo pipefail`; its rsync aborted with exit 23
on `digests/ai/subscribers.json`, which the containerized subscription API had
written as `root:0600`. Cron's only output went to `/tmp/backup-state.log`, which
nothing watches, so it never surfaced.

The redesign would have widened this from one file to five per digest:
`tempfile.mkstemp` hard-codes 0600 regardless of umask, so every file written
through the new `atomic_write_*` helpers inherits it.

Three fixes, all applied:

- **Ownership reset** — `chown -R 1000:1000 /app/digests` from inside the batch
  container (host `sudo` needs a password; the container is already root over the
  same bind mount).
- **`digest_pipeline/util.py`** — `atomic_write_text` now chmods the temp file to
  0644 before `os.replace`, with a mode assertion in `tests/test_atomic_io.py`.
- **`~/bin/backup-state.sh`** — a Telegram alert on any non-zero exit (same
  credential source as `run-batch.sh`), so this can never fail silently again;
  and the two `~/bin/backup-*.sh` scripts now back themselves up via
  `HOST_SCRIPTS`.

Backups verified working: the state repo went from 2026-06-25 to current.

## Rollback

The redesign is one merge commit, so it still backs out cleanly:

```bash
cd ~/digest-pipeline-staging && git revert -m 1 e48a4bc && git push origin master
cd ~/digest-pipeline && git status && git pull
docker compose -f ~/digest/compose.prod.yml up -d --force-recreate \
  digest-subscriptions digest-console
docker compose -f ~/digest/compose.prod.yml up -d --force-recreate caddy
```

`.shipped_urls.json` and the extra `.seen_embeddings.json` keys are ignored by the
old code — no state cleanup needed. **Do not revert the atomic-write permission
fix** (`e5604d5`) along with it; it is independent and still wanted.

## First live run

Verification of the first cron run (models, `Cross-day dupes skipped`, cost,
email) was deferred to 03:00 ET on 2026-09-02 rather than triggering a manual run,
which would have re-sent an already-delivered digest to live subscribers.
