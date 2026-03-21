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

---

## Completed

### Custom Domain for Landing Page
- **Done 2026-03-21.** Registered `aidailyroundup.com` via Cloudflare, pointed to GitHub Pages with CNAME. Switched email delivery from AgentMail to Resend (`digest@aidailyroundup.com`). Subscription API remains on `ai-digest.duckdns.org` with CORS configured for the new domain.
