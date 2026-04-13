# Digest Pipeline Backlog

## Source Health Audit Job

### Goal
Add a weekly job that audits the digest pipeline's recent extraction history to find sources that are no longer yielding articles.

### What it should do
- Run on a weekly cron schedule.
- Inspect recent extraction history / work artifacts to identify sources that repeatedly produce zero extracted articles.
- Diagnose likely causes for each bad source, for example:
  - broken RSS/feed URL
  - HTML/app-shell/error page being treated as valid content
  - source format changed
  - source is inactive or low-volume
  - parser/extractor mismatch
- Send Mike a concise report with:
  - sources that appear unhealthy
  - likely reason each is failing
  - suggested action (fix URL, switch strategy, remove source, keep monitoring, etc.)

### Additional research task
After diagnosing unhealthy sources, also search for authoritative AI sources on the internet and recommend:
- additions to the current source list
- substitutions for weak or dead sources

### Notes
- Prefer a lightweight, low-token approach for the audit step where possible.
- Reuse existing logs/work artifacts instead of re-fetching everything.
- Keep recommendations opinionated and practical, not just a giant dump of possible feeds.
- This should complement the digest source list maintenance workflow, not just detect failures.

## Welcome Email on Subscribe

### Goal
Send a confirmation/welcome email when someone subscribes so they know it worked and see what to expect (next delivery time, what's included, unsubscribe link).

## Double Opt-In

### Goal
Require email confirmation before adding to the subscriber list. Prevents abuse and improves sender reputation with email providers.

### What it should do
- On subscribe, send a confirmation email with a unique link
- Only add to `subscribers.json` after the link is clicked
- Expire unconfirmed signups after 24 hours

## Web Archive of Past Digests

### Goal
Render past digests as browsable HTML pages on the landing page so visitors can see what they'd be subscribing to.

### What to consider
- Generate HTML pages from the archived `.md` files
- Add a "Recent Issues" section to the landing page linking to them
- Could be generated during the archive stage

## Delivery Event Tracking (Bounces, Opens)

### Goal
Use Resend webhooks to track delivery events (bounced, opened, clicked) for subscriber list hygiene.

### What it should do
- Set up a webhook endpoint to receive Resend events
- Auto-remove hard-bounced addresses
- Log delivery metrics for diagnostics

## Subscriber Count on Landing Page

### Goal
Show social proof on the landing page (e.g., "Join 12 subscribers") pulled live from the subscription API.

### What to consider
- Add a `/api/stats` endpoint returning subscriber count
- Fetch and display on the landing page via JS
- Don't expose email addresses, just the count

## Email Preheader Text

### Goal
Add a preheader (preview text) to digest emails — the snippet Gmail shows next to the subject line in the inbox list.

### What it should do
- Extract the first 1-2 story titles from the digest
- Inject as a hidden preheader div at the top of the HTML email body

## Split landing page template from build output

### Goal
Stop `_update_landing_page()` in `digest_pipeline/podcast.py` from writing into the same `digests/*/index.html` file that is also the tracked source-of-truth template. Today that file is both input and output, which causes working-tree drift on any deployment where the archive cron isn't committing the regenerated output (e.g. the staging environment, where `archive.enabled=false`).

### What it should do
- Rename the tracked file to `digests/*/index.template.html` (or similar).
- Update `_update_landing_page()` to read the template, substitute `{{PUBLIC_BASE_URL}}` + episode block, and write out `digests/*/index.html`.
- Gitignore `digests/*/index.html`.
- Update `archive.artifacts` defaults in `config.py` / CLAUDE.md so prod no longer commits the built HTML (the template is the source of truth).
- Caddy's file server keeps serving `/index.html` as before — no vhost changes needed.

### Why
The staging environment currently has to `git checkout -- digests/*/index.html` before every `git pull`, because `_update_landing_page()` rewrites the file with staging URLs on every podcast run and the archive cron isn't around to commit it. Splitting template from output removes this last paper cut.

## Podcast pronunciation rewrites

### Goal
Fix mispronunciations in Kokoro TTS output (e.g. `401(k)`, `FINRA`, `AWS`, `S&P`) by rewriting podcast turn text after script parsing but before TTS synthesis, without affecting the saved script, digest markdown, RSS, or archive text.

### Plan
See [pronunciation.md](pronunciation.md) for the full design: a new `digest_pipeline/pronunciation.py` module, a `podcast.pronunciation` config section with a friendly terms map and optional regex escape hatch, and a hook point in `podcast.py` between `parse_script()` and `synthesize_script()`.

## Register Podcast with Directories

### Goal
Submit the AI Daily Roundup podcast RSS feed to major podcast directories so listeners can subscribe through their preferred app.

### Directories to target
- Apple Podcasts
- Spotify
- Google Podcasts
- Amazon Music / Audible
- Pocket Casts
- Overcast

### What to consider
- Most directories require a valid RSS feed with specific tags (artwork, author, category, explicit flag, etc.) — audit `podcast.xml` for compliance
- Apple Podcasts is the most strict; getting accepted there usually means the feed works everywhere
- Some directories (Spotify) have their own submission portal separate from RSS
- Need podcast artwork (minimum 1400x1400, recommended 3000x3000)
- May need to add `<itunes:*>` tags to the RSS feed if not already present

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
