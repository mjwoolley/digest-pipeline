# Per-source staleness thresholds + last_fetched / last_included

## Context

The console currently flags a source as **stale** when `.source_state.json` shows no `last_updated` or it's >3 days old (`console/src/pages/SourceHealth.jsx:18-21`). That field is only written by `merge_pending_ids` in `digest_pipeline/source_state.py:290,299` **when a source contributes new unseen item IDs** — not on every fetch.

Two problems fall out of this:

1. **Legitimate-but-quiet feeds are mislabeled.** Low-frequency RSS feeds (e.g. Google AI Blog, ~weekly; One Useful Thing, ~3 weeks) get flagged stale during their normal quiet periods. The 3-day threshold is hardcoded and doesn't match publication cadence.
2. **`last_updated` is semantically ambiguous.** It conflates "last fetch succeeded" with "last time new content arrived" with "last time an article shipped." Debugging a flagged source requires guessing which failed.

Keep `feed_hours` at its 24-hour default — looking back further would just re-extract already-seen items and burn tokens. Instead:

- Track **two distinct timestamps** per source: `last_fetched` (fetch returned non-empty content) and `last_included` (article made it into the final digest).
- Make the **staleness threshold configurable per source**, matched to that publication's actual cadence.

Intended outcome: a weekly feed that published 5 days ago and has been fetched cleanly every day renders as healthy; a feed that hasn't returned any content within its configured cadence window flips to stale; a feed that is fetching fine but never contributes to the final digest shows as warning.

## Design

### 1. State schema (`.source_state.json`)

Current entry shape:
```json
"blog:simon_willison": {"seen_ids": [...], "last_updated": "2026-04-14"}
```

New entry shape:
```json
"blog:simon_willison": {
  "seen_ids": [...],
  "last_fetched": "2026-04-15",
  "last_included": "2026-04-14"
}
```

**Migration on load** (in `digest_pipeline/source_state.py:load_state`): if an entry has `last_updated` but not `last_fetched`, copy `last_updated` → `last_fetched` and drop the old key. Leave `last_included` unset for legacy entries — let subsequent runs backfill it. No separate migration script needed; happens transparently on next save.

### 2. Pipeline writes

Two new write paths in `digest_pipeline/digest.py`, both consolidated into the existing post-archive block around `digest.py:604-610`:

**`last_fetched`** — update only when the fetch returned **non-empty content** (i.e. at least one item within the `feed_hours` window, or any non-empty payload for non-RSS fetchers). An exception, HTTP error, bad-payload, or parse that yielded zero in-window items does **not** advance `last_fetched`. In `gather.py:204-247` the result dict already carries `content: str`; digest.py simply collects source keys where `content.strip() != ""` and writes `last_fetched = date` for each.

Rationale: under a 24h window, a healthy feed that happens to publish weekly will have empty content on 6 of 7 runs. That's expected — but advancing `last_fetched` on those empty days would hide a real fetch regression (e.g. the URL starts returning a redirect-to-HTML page). Tying `last_fetched` to actual content received keeps it a useful signal. Staleness checks against `last_fetched` will then need a threshold generous enough to accommodate publication cadence (see §5).

**`last_included`** — after the final article list is settled (`digest.py` line ~537, where `deduped` is the post-prioritize list passed to FORMAT), collect `set(sk for article in deduped for sk in article.get("source_keys", []))` and write `last_included = date` for each. The `source_keys` field is already attached to articles during extraction and survives through dedupe/prioritize (confirmed: `digests/ai/work/2026-04-14/deduped.json` entries carry `source_keys: ["blog:simon_willison"]`).

**Unconditional save.** The current block at `digest.py:605-610` only calls `save_state` when `pending_ids` is non-empty. Broaden: always save if any of `pending_ids`, `fetched_keys`, or `included_keys` is non-empty — which for any run that produced a digest is always.

**Refactor `merge_pending_ids`** in `source_state.py` to accept these new inputs and update all three fields per source in one pass. Rename the function to `update_source_state(state, pending_ids, fetched_keys, included_keys, date, max_ids)` — callers at `digest.py:606` update accordingly. `fetched_keys` here means "fetched with non-empty content."

**Update `prune_state`** in `source_state.py:55` to use `max(last_fetched, last_included)` (or just `last_fetched`) for the age cutoff instead of `last_updated`.

### 3. Config schema — per-source `stale_after_days`

Add an optional integer field to every source config block. Example:

```json
"google_ai_blog": {
  "name": "Google AI Blog",
  "feed_url": "https://blog.google/technology/ai/rss/",
  "description": "Gemini and DeepMind updates",
  "stale_after_days": 14
}
```

Default = 3 when unspecified (preserves current behavior for sources that don't need tuning).

**Calculated values for currently-stale AI digest RSS sources**, derived from the last 10 `pubDate` entries on each feed (see cadence analysis below):

| source_key | recent cadence | `stale_after_days` |
|---|---|---|
| `blog:google_ai_blog` | weekly bursts, up to 12-day gap | **14** |
| `blog:huggingface_blog` | multiple posts/week, ~3–4 day gap | **7** |
| `blog:openai_blog` | multiple posts/week, ~3 day gap | **7** |
| `blog:the_ai_engineer` | 2×/week | **7** |
| `blog:latent_space` | near-daily, up to 3-day gap | **5** |
| `blog:one_useful_thing` | ~3-week cadence | **45** |
| `blog:the_new_stack` | many posts/day | default (3) |
| `blog:simon_willison` | daily-ish | default (3) |

Rule-of-thumb used: ~1.5–2× the observed median inter-post gap, with a floor at 3. Twitter and newsletter sources are out of scope for this pass — the ask was RSS.

### 4. Console API (`digest_pipeline/console_api.py`)

Update `/api/digests/<slug>/sources` (around `console_api.py:679-707`) to return:

```json
{
  "source_key": "blog:google_ai_blog",
  "last_fetched": "2026-04-15",
  "last_included": "2026-04-14",
  "stale_after_days": 14,
  ...existing fields
}
```

Read `stale_after_days` from the source's config block (fall back to 3). Drop `last_updated` from the response.

### 5. Frontend (`console/src/pages/SourceHealth.jsx`)

- Replace the single "Last Updated" column with two: **Last Fetched** and **Last Included**.
- New `healthStatus(src)` logic (lines 17-23):
  ```js
  if (!src.last_fetched || daysSince(src.last_fetched) > src.stale_after_days) return 'stale';
  if (!src.last_included || daysSince(src.last_included) > src.stale_after_days) return 'warning';
  return 'healthy';
  ```
  Rationale: because `last_fetched` now only advances on non-empty content, it already tracks publication cadence. `stale_after_days` is therefore the right threshold for it — a weekly feed with `stale_after_days: 14` won't go stale during normal quiet stretches but will once it stops returning content entirely. Warning status distinguishes "content is arriving but nothing is making it into the digest" (quality signal) from "source is genuinely dark" (fetch signal).
- Sort order and summary badges at lines 62, 173-208 already handle stale/warning/healthy — no change needed beyond the new rule.

## Working tree and rollout

**All work happens in `/mnt/HC_Volume_105380972/digest-pipeline-staging`** (this checkout), not the production checkout at `/mnt/HC_Volume_105380972/digest-pipeline`. Both are clones of the same GitHub repo (`mjwoolley/digest-pipeline`); this checkout is currently on branch `fix/clustering-embedding-tuning` with push disabled on `origin`.

This checkout has its own data root and its own `.source_state.json` per digest. `DIGEST_ENV=staging` is already set in `digest-subscriptions-staging.service` and must be set in the shell when invoking `run.sh` manually against this tree. Overlay files already exist at `digests/{ai,wealthtech}/config.staging.json`.

**All file paths in the "Critical files to modify" section below are relative to `/mnt/HC_Volume_105380972/digest-pipeline-staging`.** Nothing is edited under `/mnt/HC_Volume_105380972/digest-pipeline` as part of this change.

**Sequence:**

1. **Create a feature branch here** off the current tip: `git checkout -b feat/source-staleness-per-source`. Implement all code + config changes here.
2. **Install the local package** so the staging systemd services pick up Python changes: `.venv/bin/pip install -e .`.
3. **Add the new `stale_after_days` values to the staging overlays**, not the base configs. Each overlay gets a `sources.blogs.<key>.stale_after_days` entry. `load_config` deep-merges dicts recursively (per CLAUDE.md), so this layers cleanly on top of the base blog entries without redefining `feed_url`/`name`.
4. **Run tests:** `.venv/bin/pytest`.
5. **Run the staging pipeline end-to-end:** `DIGEST_ENV=staging ./run.sh digests/ai/config.json` (and same for wealthtech). Inspect the staging digest's `.source_state.json` — every source with non-empty content should have `last_fetched = today`; every source that contributed to `deduped.json` should have `last_included = today`; legacy `last_updated` entries should be migrated.
6. **Restart staging services** so the live API/console reflect the new code: `sudo systemctl restart digest-subscriptions-staging` (and the staging console service if one exists).
7. **Verify in the staging console UI** (two columns, correct per-source thresholds, sensible stale/warning/healthy badges across both digests).
8. **Let staging run on its normal cron for 2–3 days** to confirm thresholds behave correctly against real cadence (especially weekly/low-frequency feeds).
9. **Promote to production:** push the branch to GitHub (separate step — requires re-enabling push or opening a PR from a different clone), merge to `master`, then in the production checkout `cd /mnt/HC_Volume_105380972/digest-pipeline && git pull && .venv/bin/pip install -e .`. Move the `stale_after_days` entries from the staging overlays into the production base `config.json` files in the same PR (or keep in overlays and create production equivalents — whichever the user prefers; confirm before step 9). Restart prod services.

## Critical files to modify

All paths relative to `/mnt/HC_Volume_105380972/digest-pipeline-staging`:

- `digest_pipeline/source_state.py` — schema migration in `load_state`; extend `merge_pending_ids` to also record `last_fetched` and `last_included` (rename to `update_source_state(state, pending_ids, fetched_keys, included_keys, date, max_ids)`); update `prune_state`.
- `digest_pipeline/gather.py` — blog fetcher result dicts already carry `content: str`; no schema change needed there, but confirm all fetchers (`_fetch_blog`, HTML-scrape, twitter, newsletter, github_trending) consistently use empty string to signal "no usable content."
- `digest_pipeline/digest.py` — derive `fetched_keys` from gather results (source keys whose content is non-empty); derive `included_keys` from the final `deduped` list (post-prioritize) via `source_keys` field on each article; make the state-save block at ~line 604 unconditional on any of the three key sets being non-empty.
- `digest_pipeline/console_api.py` — `/api/digests/<slug>/sources` returns `last_fetched`, `last_included`, `stale_after_days` (reading from the source's config block, falling back to 3); drop `last_updated`.
- `console/src/pages/SourceHealth.jsx` — replace single "Last Updated" column with "Last Fetched" + "Last Included"; new `healthStatus` logic.
- `tests/test_source_state.py`, `tests/test_console_api.py` — update expectations.
- `digests/ai/config.staging.json` — add `sources.blogs.<key>.stale_after_days` for the 6 RSS sources listed above.
- `digests/wealthtech/config.staging.json` — same treatment once its low-cadence RSS sources are audited.

Base `config.json` files under `digests/ai/` and `digests/wealthtech/` are **not** edited during staging; promotion to production happens in step 9.

## Verification

All executed inside `/mnt/HC_Volume_105380972/digest-pipeline-staging`.

1. **Unit tests.** Update `tests/test_source_state.py` to cover: migration of legacy `last_updated` entries, write paths for each of the three fields, `update_source_state` with various combinations of pending/fetched/included keys. Update `tests/test_console_api.py` to assert the new response shape. Run `.venv/bin/pytest`.
2. **Back up staging state:** `cp digests/ai/.source_state.json digests/ai/.source_state.json.bak` (and wealthtech) before the first run, so legacy migration is visible.
3. **End-to-end run on AI digest (staging):** `DIGEST_ENV=staging ./run.sh digests/ai/config.json`. Inspect the resulting `.source_state.json` — entries that previously had `last_updated` now have `last_fetched`; sources that returned content today show `last_fetched: 2026-04-15`; sources whose articles appear in `work/2026-04-15/deduped.json` show `last_included: 2026-04-15`.
4. **Console smoke test.** Hit the staging console UI (via the staging systemd unit post-restart, or locally via `.venv/bin/digest-pipeline --console --digests-dir digests --port 5201`). Open Source Health. Confirm:
   - Google AI Blog renders two date columns with `stale_after_days: 14` and a healthy/warning badge consistent with its real cadence.
   - Break a feed URL locally in the staging base config (e.g. point `openai_blog` at a bad URL), re-run, confirm it still shows the prior `last_fetched` and — once that date ages past `stale_after_days` — flips to stale.
   - Restore the URL, re-run, confirm recovery on next run that returns content.
5. **Cron soak.** Leave staging running on its normal cron for 2–3 days. Confirm no source incorrectly flips to stale during a normal quiet stretch.

## Cadence analysis (reference)

Raw pubDate samples pulled on 2026-04-15 for each currently-stale feed — listed here so the thresholds above are auditable:

- **google_ai_blog**: 14-Apr (×2), 02-Apr (×2), 01-Apr (×2), 31-Mar, 26-Mar (×3) → clusters ~weekly, max gap 12d.
- **huggingface_blog**: 09-Apr (×2), 08-Apr (×2), 02-Apr, 01-Apr (×3), 31-Mar (×2) → ~3–4d gap.
- **openai_blog**: 14-Apr, 13-Apr, 10-Apr (×8) → ~3d gap.
- **the_ai_engineer**: 14-Apr, 10-Apr, 08-Apr, 03-Apr, 01-Apr, 27-Mar, 25-Mar, 20-Mar, 18-Mar, 13-Mar → 2–4d gap.
- **latent_space**: 15-Apr, 14-Apr, 10-Apr, 08-Apr (×2), 07-Apr (×2), 03-Apr (×3) → ~1–3d gap.
- **one_useful_thing**: 31-Mar, 12-Mar, 18-Feb, 27-Jan, 07-Jan → 12–21d gap.
