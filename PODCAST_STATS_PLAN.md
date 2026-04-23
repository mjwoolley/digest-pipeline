# Podcast Stats Plan

## Summary

Add podcast analytics to the digest management console using Caddy access logs as the source of truth. The goal is to show:

- **Overview card**
  - Email Subscribers
  - Podcast Subscribers (estimated)
- **Podcast page**
  - Add a **Downloads** column to the episode table, to the right of **Articles**

The stats should work independently for both podcasts:
- AI Daily Roundup
- WealthTech Daily Roundup

Persist aggregated podcast stats into each digest's own data root so the console API can serve fast responses without reparsing raw logs on every request.

---

## Recommended Approach

Use **structured Caddy access logs** to derive two estimated metrics:

1. **Estimated podcast subscribers**
   - based on unique clients fetching `/podcast.xml`
2. **Episode downloads**
   - based on deduped requests to `/podcasts/YYYY-MM-DD.mp3`

Build a small stats pipeline in the repo that:
- parses Caddy access logs
- separates AI vs WealthTech by host/domain
- dedupes noisy repeated requests
- writes aggregated JSON artifacts into each digest root
- exposes the results through the existing console API
- renders the new stats in the existing console UI

---

## UI Plan

### Overview card

Keep the existing subscriber count but relabel it to:
- **Email Subscribers**

Add a new metric:
- **Podcast Subscribers**

### Podcast page

In the episode table, add a new column:
- **Downloads**

Place it to the right of **Articles**.

---

## Data Collection Plan

### Source of truth

Use **Caddy access logs** for podcast traffic.

Required request coverage:
- `/podcast.xml`
- `/podcasts/*.mp3`

Recommended log format:
- JSON

Recommended fields:
- timestamp
- host
- uri/path
- method
- status
- remote IP
- user-agent
- referer
- bytes written

### Caddy change

Update `/etc/caddy/Caddyfile` to enable access logging for:
- `aidailyroundup.com`
- `wealthtechdigest.com`

Also make sure log rotation/retention is configured so the metrics window is supported.

---

## Stats Processing Plan

### New module

Add a repo module, likely:
- `digest_pipeline/podcast_stats.py`

Responsibilities:
- parse structured access logs
- classify requests as:
  - feed requests
  - episode audio requests
- map each request to the correct digest slug
- normalize user agents
- dedupe repeated requests
- generate summary artifacts

### Persistence

Write generated stats into each digest root.

Example files:
- `podcast_stats.json`
- `podcast_downloads.json`

This keeps AI and WealthTech fully isolated.

---

## Metric Definitions

### 1. Podcast Subscribers (estimated)

Best practical approximation:
- unique clients fetching `/podcast.xml`
- measured over a rolling **7-day window**

Recommended dedupe heuristic:
- digest slug
- normalized client key = IP + normalized user-agent
- daily bucket

Count a client once per day, then take the union across the last 7 days.

Recommended filtering:
- include statuses: `200`, `206`, `304`
- exclude obvious bots/crawlers when possible

### 2. Episode Downloads

Best practical approximation:
- unique client downloads per episode from `/podcasts/YYYY-MM-DD.mp3`

Recommended dedupe heuristic:
- episode
- normalized client key = IP + normalized user-agent
- rolling 24-hour window

Recommended filtering:
- include statuses: `200`, `206`
- exclude `HEAD` requests
- optionally ignore extremely tiny transfers
- merge repeated range/chunk requests into one download estimate

These numbers will not be exact or IAB-compliant, but should be good enough for trends and useful product decisions.

---

## API Changes

Extend the existing console API in:
- `digest_pipeline/console_api.py`

### Overview endpoint changes

In the digest list / overview response:
- rename or clarify existing subscriber count as `email_subscriber_count`
- add `estimated_podcast_subscribers`

### Podcast endpoint changes

In the podcast page response:
- add per-episode `download_count`
- add summary podcast stats if useful
- preserve existing episode metadata

If needed, add a dedicated podcast stats endpoint later, but likely not necessary for the first pass.

---

## UI Files Likely to Change

### Console UI
- `console/src/components/DigestCard.jsx`
- `console/src/pages/Podcasts.jsx`
- possibly shared styles if the card layout gets crowded

### Backend / repo
- `digest_pipeline/console_api.py`
- new: `digest_pipeline/podcast_stats.py`
- maybe CLI wiring if we add a refresh command

### Infra outside repo
- `/etc/caddy/Caddyfile`

---

## Suggested Artifact Shapes

### `podcast_stats.json`

```json
{
  "estimated_subscribers": 123,
  "subscriber_window_days": 7,
  "last_computed_at": "2026-04-23T01:00:00Z",
  "log_coverage_start": "2026-04-16T00:00:00Z",
  "log_coverage_end": "2026-04-23T00:59:59Z",
  "top_apps": [
    {"app": "Apple Podcasts", "requests": 80},
    {"app": "Overcast", "requests": 25}
  ]
}
```

### `podcast_downloads.json`

```json
{
  "episodes": {
    "2026-04-22": {
      "downloads": 37,
      "top_apps": [
        {"app": "Apple Podcasts", "downloads": 20},
        {"app": "Overcast", "downloads": 8}
      ]
    }
  }
}
```

---

## Rollout Plan

1. Enable Caddy access logging for podcast traffic
2. Build the parser / aggregator module
3. Generate podcast stats JSON artifacts per digest
4. Extend console API
5. Update overview cards and podcast episode table UI
6. Backfill from retained logs if available
7. If no historical logs exist, show empty state / collecting-data state until enough traffic accumulates

---

## Testing Plan

### Parser / metrics tests
- confirm AI vs WealthTech requests are separated correctly
- verify feed fetches count toward podcast subscriber estimate
- verify MP3 range requests collapse into one download estimate
- verify bots / noise are filtered reasonably
- verify malformed log lines are ignored safely

### API tests
- confirm overview endpoint returns:
  - email subscriber count
  - podcast subscriber estimate
- confirm podcast endpoint returns per-episode download counts

### UI tests
- overview cards render relabeled **Email Subscribers**
- overview cards render **Podcast Subscribers**
- podcast episode table renders **Downloads** column correctly
- empty-state behavior works when no stats exist yet

### Ops validation
- confirm Caddy logs are rotating properly
- confirm stats generation job can run repeatedly without duplicates
- confirm processing cost stays low enough for hourly/daily refresh

---

## Risks / Open Questions

1. **Subscriber estimates are approximate**
   - some podcast apps/proxies mask individual end users
   - exact subscriber counts are not realistically available from raw logs alone

2. **User-agent normalization may need tuning**
   - especially for Apple Podcasts and other proxy-heavy ecosystems

3. **Need enough log retention**
   - at least enough to support the chosen subscriber window

4. **Initial backfill may be limited**
   - if old logs do not exist, stats will need time to accumulate

5. **Need to choose refresh cadence**
   - hourly is probably fine
   - daily may be enough if simplicity matters more than freshness

---

## Final Recommendation

Implement:

### Overview card
- **Email Subscribers**
- **Podcast Subscribers**

### Podcast page
- add **Downloads** column to the episode table

And define:
- **Podcast Subscribers** = unique `podcast.xml` clients over the last **7 days**
- **Downloads** = deduped unique client downloads per episode over a **24-hour client/episode window**

This is the cleanest version of the feature and fits the current architecture well.
