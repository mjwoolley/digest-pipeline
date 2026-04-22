# Digest Pipeline Backlog

## Open

- [ ] **Source Health Audit Job**
  - Add a weekly job that audits recent extraction history to find sources that are no longer yielding articles.
  - Reuse logs/work artifacts instead of re-fetching where possible.
  - Diagnose likely causes, for example:
    - broken RSS/feed URL
    - HTML/app-shell/error page treated as valid content
    - source format changed
    - source inactive or low-volume
    - parser/extractor mismatch
  - Send Mike a concise report with unhealthy sources, likely cause, and suggested action.
  - Also recommend authoritative new AI sources or replacements for weak/dead ones.

- [x] **Move prod + staging digest repos to dedicated volume**
  - Mount the new 10G volume at a stable path (planned: `/mnt/digest-data`).
  - Migrate:
    - `/home/clawdbot/digest-pipeline`
    - `/home/clawdbot/digest-pipeline-staging`
  - Symlink both back to their original paths so cron/jobs/scripts remain transparent.
  - Verify git, dry-runs, podcast generation, and path resolution before removing old copies.
  - Expected outcome: free about 4.8G on root disk.

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

- [ ] **Split landing page template from build output**
  - Stop `_update_landing_page()` in `digest_pipeline/podcast.py` from writing into the tracked template file.
  - Rename tracked source to something like `digests/*/index.template.html`.
  - Generate `digests/*/index.html` from the template.
  - Gitignore generated `digests/*/index.html`.
  - Update archive artifact defaults so prod no longer commits generated landing pages.
  - Keep Caddy serving `/index.html` as before.
  - Reason: staging currently has to reset `digests/*/index.html` before pulls because podcast runs rewrite it.

- [x] **Podcast pronunciation rewrites**
  - Fix Kokoro mispronunciations like `401(k)`, `FINRA`, `AWS`, `S&P`.
  - Rewrite podcast turn text after script parsing but before TTS synthesis.
  - Do not affect saved script, digest markdown, RSS, or archive text.
  - See [pronunciation.md](pronunciation.md) for the design.

- [ ] **AI relevance filter stage**
  - Add a story-level relevance filter after extract/normalize and before clustering.
  - Prevent non-AI stories from slipping into the AI Daily Roundup when AI-adjacent sources publish unrelated material.
  - Use rules-first filtering with optional LLM handling for borderline cases.
  - Operate on normalized article objects (`title`, `description`, `category`, source metadata).
  - Save filtered artifacts and log exclusion reasons for observability.
  - See `/home/clawdbot/digest-pipeline-staging/relevance.md` for the plan.

- [ ] **CI/CD: Auto-deploy to production on merge**
  - Add a GitHub Actions workflow that deploys to production after merge to master.
  - SSH into the server, `git pull`, restart services.
  - Eliminates the current two-clone workflow where staging push URL must be toggled manually.
  - Consolidate to a single clone if feasible, using branches for dev and `DIGEST_ENV` for runtime config.

- [ ] **Podcast download stats via Caddy access logs**
  - Add a `log` directive to `/etc/caddy/Caddyfile` for the podcast domains so MP3 requests are persisted.
  - Write a weekly stats script that greps the logs for `/podcasts/*.mp3` requests and deduplicates by IP + User-Agent + episode.
  - Report per-episode download counts and top podcast apps (Apple Podcasts, Overcast, etc.) via User-Agent.
  - Caveats: not IAB-compliant, doesn't handle range-request chunking perfectly, includes some bot noise. Good enough as a trend indicator at current scale. Consider Podtrac/Chartable if precise numbers are ever needed.

- [ ] **Per-stage Telegram progress notifications**
  - Send a short Telegram message after each major pipeline stage completes.
  - Include stage name, duration, and key metric (e.g. article count, cluster count, email count).
  - Provides real-time visibility without tailing logs.
  - Keep messages short and non-spammy.
  - Consider a config flag to enable/disable per digest.

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

### Console: Rename "Runs" to "Digest", add Podcast run history
- **Done 2026-03-25.** Restructured sidebar (Sources > Digest > Podcast > Delivery), added podcast run history table and detail page with stage logs.

### Console: Responsive mobile layout
- **Done 2026-03-25.** Collapsible sidebar, responsive tables and cards for phone/tablet viewports.

### Management Console for Multiple Digests
- **Done 2026-03-25.** Read-only Preact + Material Web dashboard served by Flask on port 5200 (Tailscale only). Views: Overview (all digests at a glance), Run History + Detail (stage timeline, article funnel, source files), Source Health (grouped by type with stale/healthy flags), Delivery (subscriber count, 7-day success rate, send history), Podcast (episode list, RSS sync check). Auto-refresh toggle (5s poll). CLI: `digest-pipeline --console [--digests-dir DIR] [--port PORT]`. Deploy as systemd service `digest-console.service`.

### Custom Domain for Landing Page
- **Done 2026-03-21.** Registered `aidailyroundup.com` via Cloudflare, pointed to GitHub Pages with CNAME. Switched email delivery from AgentMail to Resend (`digest@aidailyroundup.com`). Subscription API remains on `ai-digest.duckdns.org` with CORS configured for the new domain.
