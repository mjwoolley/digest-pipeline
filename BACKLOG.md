# Digest Pipeline Backlog

## Open

### Defects

- [ ] **Split landing page template from build output**
  - Stop `_update_landing_page()` in `digest_pipeline/podcast.py` from writing into the tracked template file.
  - Rename tracked source to something like `digests/*/index.template.html`.
  - Generate `digests/*/index.html` from the template.
  - Gitignore generated `digests/*/index.html`.
  - Update archive artifact defaults so prod no longer commits generated landing pages.
  - Keep Caddy serving `/index.html` as before.
  - Reason: staging currently has to reset `digests/*/index.html` before pulls because podcast runs rewrite it.

- [ ] **Fix `exclusive` miscount in `_compute_source_metrics`**
  - `console_api.py:250` reads `deduped.json` to count articles with a single `source_key`. When prioritize ran, `deduped.json` includes post-prioritize dropped articles, so sources whose only contribution got dropped are still counted as "exclusive."
  - Either skip the exclusive read when prioritize ran (kept list is in `prioritized.json`), or take the intersection of `deduped.json` and `prioritized.json` for the exclusive tally.
  - The equivalent logic in `source_history.py:compute_daily_rows` already handles this correctly (uses kept when prioritized.json exists), so this is API-side only.

### Enhancements

- [ ] **Source Health Audit Job — automation, diagnosis, push report**
  - Foundation shipped 2026-05-13 (per-source ledger + windowed Source Health view). Remaining audit-job work:
    - Weekly scheduled run (cron or systemd timer) that reads `source_history.jsonl` and applies thresholds.
    - Diagnosis: when a source has gone quiet, classify likely cause — broken RSS/feed URL, HTML/app-shell/error page treated as valid content, source format changed, source inactive or low-volume, parser/extractor mismatch.
    - Push report to Telegram with unhealthy sources, likely cause, suggested action.
    - Recommend authoritative new AI sources or replacements for weak/dead ones — **Twitter-only shipped via `twitter_discovery` module (2026-05-14): audit appends suggested new accounts to its Telegram report.** Blog/RSS/newsletter recommendation still open.

- [ ] **Welcome Email on Subscribe**
  - Send a confirmation/welcome email when someone subscribes so they know it worked and what to expect.

- [ ] **Double Opt-In**
  - Require email confirmation before adding to the subscriber list.
  - Send a unique confirmation link.
  - Only add to `subscribers.json` after click.
  - Expire unconfirmed signups after 24 hours.

- [ ] **Web Archive of Past Digests**
  - Render past digests as browsable HTML pages on the landing page.
  - Generate from archived `.md` files.
  - Add a "Recent Issues" section linking to them.

- [ ] **Delivery Event Tracking (Bounces, Opens)**
  - Use Resend webhooks to track delivery events.
  - Auto-remove hard-bounced addresses.
  - Log delivery metrics for diagnostics.

- [ ] **Subscriber Count on Landing Page**
  - Add a `/api/stats` endpoint returning subscriber count.
  - Fetch and display the count on the landing page.
  - Do not expose email addresses.

- [ ] **Email Preheader Text**
  - Extract the first 1-2 story titles from the digest.
  - Inject them as hidden preheader text in the email HTML.

- [ ] **Twitter account discovery in the console (UI wrapper)**
  - CLI version shipped 2026-05-14 as `digest_pipeline/twitter_discovery.py` (`digest-pipeline --discover-twitter`), with composite scoring (followers × post frequency × engagement) and audit-report integration.
  - Remaining: a "Discover" tab per digest that exposes the same module via the console — `POST /api/digests/<slug>/discover/twitter` route in `digest_pipeline/console_api.py`, new `console/src/pages/Discover.jsx`, reuses existing `POST /api/digests/<slug>/sources` for the add path.
  - See [twitter_search.md](twitter_search.md) for the original design.

- [ ] **Configurable per-digest clustering thresholds**
  - Replace the hardcoded `0.85` similarity thresholds in intra-day clustering (`digest.py`) and cross-day dedup (`seen_articles.py`) with values read from a per-digest `clustering` config block.
  - AI appears to benefit from a looser intra-day threshold (`0.80`) while cross-day stays at `0.85`.
  - Defaults stay `0.85 / 0.85` so existing digests behave the same when the config block is absent.
  - Empty `feat/configurable-clustering-thresholds` branch is already pushed; design in [clustering-thresholds.md](clustering-thresholds.md).

- [ ] **Pipeline error observability**
  - Notify Mike when a digest pipeline run fails.
  - Hook failure reporting into the main run path so exceptions do not fail silently.
  - Include digest name, failing stage, timestamp, and error summary in the alert.
  - Decide whether alerts should go to Telegram only, PagerDuty only, or both.
  - Consider a matching success or recovery notification so failures are easier to contextualize.

- [ ] **PagerDuty integration for digest failures**
  - Research whether PagerDuty still has a usable free tier for this setup.
  - If viable, add a PagerDuty integration key/config path for pipeline alerts.
  - Trigger a PagerDuty event when a digest run fails.
  - Document setup steps, limits, and any fallback plan if PagerDuty's free tier is gone or too constrained.

- [ ] **Register Podcast with Directories**
  - Submit the AI Daily Roundup RSS feed to major podcast directories.
  - Targets:
    - Apple Podcasts
    - Spotify
    - Google Podcasts
    - Amazon Music / Audible
    - Pocket Casts
    - Overcast
  - Audit `podcast.xml` for directory compliance.
  - Ensure artwork meets platform requirements.
  - Add `<itunes:*>` tags if needed.

---

## Completed

### Console Material 3 button styling + Twitter handle validation
- **Done 2026-05-13.** Two console defects from the audit work. (1) Add Source rejected real Twitter handles: the backend regex was `^[a-z0-9_]+$` and nothing stripped a leading `@`. Backend now branches on source_type — Twitter accepts `^[A-Za-z0-9_]{1,15}$` and strips `@`; other source types keep the lowercase convention. (2) `md-filled-button`/`md-outlined-button`/`md-text-button` rendered with no horizontal padding because the universal `* { padding: 0 }` reset overrode Material Web's `:host` rule (outer-document rules beat shadow `:host` regardless of specificity). Restored standard Material 3 padding in outer scope. Window-chip selector redesigned with proportional dimensions and `font: inherit`.

### Source Health audit foundation
- **Done 2026-05-13.** Persistent per-source history ledger `digests/<slug>/source_history.jsonl` (one row per active source per day: extracted, in_digest, dropped, exclusive, avg_score). `digest.py` appends after each successful run. `--backfill-source-history` CLI seeds prod from existing `work/<date>/` artifacts (initial run: 477 rows × 58 days for AI, 219 × 52 for wealthtech). `/api/digests/<slug>/sources` accepts `?days=N` (clamp [1, 365]) and prefers the ledger when present. UI: 14d / 30d / 90d / 1y chip selector on Source Health. Also fixed an `in_digest` undercount in `_compute_source_metrics` for runs where prioritize was skipped. Unblocks the broader Audit Job (automation, diagnosis, push report) tracked separately.

### Stale-source health UI redesign
- **Done 2026-05-13.** Backend `commit_pending` was silently wiping `last_fetched` / `last_included` on every run by replacing the source entry dict; only `last_updated` survived. Fix: merge into the entry instead of replacing, drop `last_updated` writes, add a `load_state` migration shim that hydrates `last_fetched` from legacy `last_updated`. Console API now surfaces both timestamps plus a per-source `stale_after_days` config (14 for google_ai_blog, 45 for one_useful_thing, 7 for openai_blog / huggingface_blog / the_ai_engineer, 5 for latent_space; default 3). Source Health page replaces the single "Last Updated" column with separate "Last Fetched" / "Last Included" columns and a cadence-aware health badge. Design in [stale_fix_plan.md](stale_fix_plan.md).

### Podcast download stats via Caddy access logs
- **Done 2026-04-23.** Added subscriber and download stats plumbing (`digest_pipeline/podcast_stats.py`) and a `--podcast-stats` CLI flag that recomputes counts from access logs. Caddy logs MP3 requests for the podcast domains; the stats job dedupes by IP + User-Agent + episode and surfaces the totals plus top podcast apps (via User-Agent) in the management console. Not IAB-compliant — good enough as a trend indicator at current scale.

### AI relevance filter stage
- **Done 2026-04-16.** Added a story-level relevance filter (`digest_pipeline/relevance.py`) that runs after extract/normalize and before clustering. Rules-first filtering using `keywords_include`/`keywords_exclude` from config, with an optional borderline LLM (Haiku) classifier. Filtered articles are written to `work/<date>/filtered.json` for inspection. Configured per-digest via the `relevance_filter` config block.

### Podcast pronunciation rewrites
- **Done 2026-04-13.** Fixed Kokoro mispronunciations (`401(k)`, `FINRA`, `AWS`, `S&P`, etc.) by rewriting podcast turn text after script parsing but before TTS synthesis. Does not affect the saved script, digest markdown, RSS, or archive text. Implementation in `digest_pipeline/pronunciation.py`; design in [pronunciation.md](pronunciation.md).

### Move prod + staging digest repos to dedicated volume
- **Done 2026-04-12.** Mounted a 10G Hetzner volume at `/mnt/HC_Volume_105380972`, migrated `/home/clawdbot/digest-pipeline` and `/home/clawdbot/digest-pipeline-staging` onto it, and symlinked both back to their original paths so cron/jobs/scripts remain transparent. Freed about 4.8G on root disk.

### Console: Rename "Runs" to "Digest", add Podcast run history
- **Done 2026-03-25.** Restructured sidebar (Sources > Digest > Podcast > Delivery), added podcast run history table and detail page with stage logs.

### Console: Responsive mobile layout
- **Done 2026-03-25.** Collapsible sidebar, responsive tables and cards for phone/tablet viewports.

### Management Console for Multiple Digests
- **Done 2026-03-25.** Read-only Preact + Material Web dashboard served by Flask on port 5200 (Tailscale only). Views: Overview (all digests at a glance), Run History + Detail (stage timeline, article funnel, source files), Source Health (grouped by type with stale/healthy flags), Delivery (subscriber count, 7-day success rate, send history), Podcast (episode list, RSS sync check). Auto-refresh toggle (5s poll). CLI: `digest-pipeline --console [--digests-dir DIR] [--port PORT]`. Deploy as systemd service `digest-console.service`.

### Custom Domain for Landing Page
- **Done 2026-03-21.** Registered `aidailyroundup.com` via Cloudflare, pointed to GitHub Pages with CNAME. Switched email delivery from AgentMail to Resend (`digest@aidailyroundup.com`). Subscription API remains on `ai-digest.duckdns.org` with CORS configured for the new domain.
