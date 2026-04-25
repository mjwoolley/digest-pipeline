# Source Search: Twitter account discovery in the console

## Context

The console currently lets you view/edit existing sources but not discover new ones. Curating Twitter accounts today means manually finding handles on x.com and pasting them into `config.json`. This feature adds an in-console search that surfaces relevant Twitter accounts for a digest's topic, shows how popular each is, and lets you add them with one click — turning curation from a manual task into a guided flow.

Scoped to Twitter only, one digest at a time (the one currently being viewed). A pluggable/multi-source framework is explicitly out of scope for this iteration.

## Approach

Add a new **"Discover"** tab to each digest in the console. The tab runs a tweet-search via the existing `bird` CLI, aggregates unique authors from the results, enriches each with follower count + bio (available in `bird search --json-full` output), ranks by follower count, filters out already-added accounts, and renders a list with an Add button that writes directly back to `config.json`.

### Why author-aggregation

`bird` exposes no "search users by bio" endpoint. But `bird search <keyword> --json-full` returns each tweet with its author object including `legacy.followers_count`, `legacy.description` (bio), `core.name`, and `core.screen_name`. Aggregating authors across the tweet results effectively finds accounts *actively tweeting about* the keyword — generally higher signal than bio matching for a news digest.

## Backend

File: `digest_pipeline/console_api.py`

1. **New route** `POST /api/digests/<slug>/discover/twitter` — accepts JSON body `{ query, min_followers, max_tweets, exclude_retweets }`. Runs `bird search <query> -n <max_tweets> --json-full` (add `-filter:nativeretweets -filter:replies` to query when `exclude_retweets`). Parses stdout, walks each tweet's `_raw.core.user_results.result` to collect a dict keyed by `rest_id`:
   - `username` (screen_name), `name`, `followers_count`, `description`, `avatar_url`, `verified`, `tweet_count` (how many tweets in sample were by this author).
   - Drop authors with `followers_count < min_followers`.
   - Drop authors whose username is already in `config["sources"]["twitter"]["accounts"]` (case-insensitive).
   - Sort by `followers_count` desc, return top N (e.g. 50).
   - Return `{ results: [...], searched: <int>, already_added_filtered: <int> }`.
2. **Reuse existing helpers**: `_get_config(slug)` (per-request config load, around line 522) and `_write_config()` (lines 445–471) for the add path. No new write route needed — the existing `POST /api/digests/<slug>/sources` route already handles creating a Twitter source via `_validate_source` + `_write_config`. Discover page calls that route on Add.
3. **Subprocess handling**: model on `digest_pipeline/gather.py:44-71` (`_fetch_twitter`). Pass `AUTH_TOKEN`/`CT0` via env just like that function does. Timeout ~60s. On non-zero exit, return HTTP 502 with stderr so the UI can surface it (credential failures, rate limits).
4. **No caching layer** for this iteration — each search hits bird fresh. Cost is modest (one bird call per search).

## Frontend

Directory: `console/src/`

1. **New page** `pages/Discover.jsx`:
   - Search form (Material Web text field for keyword, number inputs for min followers / max tweets, checkbox for "exclude retweets & replies"), submit button.
   - Results list: each row shows avatar, display name, `@username` (linking to `https://x.com/<username>` in a new tab), follower count formatted (e.g. `42.3k`), bio truncated, and an **Add** button.
   - Add calls `mutateApi` POST to `/api/digests/<slug>/sources` with `{ source_type: "twitter", key: username }` (matching how `SourceEdit.jsx:190` creates sources). On success, remove the row from the list and toast.
   - Loading/empty/error states.
2. **Routing** (`app.jsx` lines 34–56): add `parts[1] === "discover"` case rendering `<Discover slug={slug} />`.
3. **Nav entry** (`Layout.jsx` lines 24–33): add `{ label: "Discover", href: \`#/${slug}/discover\` }` to `digestNav`.
4. **API hook**: use existing `useApi`/`fetchApi`/`mutateApi` from `src/hooks/useApi.js` — no new infrastructure.

## Critical files

- `digest_pipeline/console_api.py` — add discover route; reuse `_write_config`, `_validate_source`, digest lookup block around L520.
- `digest_pipeline/gather.py:44-71` — reference for bird invocation pattern (env vars, subprocess args).
- `console/src/app.jsx` — add route.
- `console/src/components/Layout.jsx` — add nav entry.
- `console/src/pages/Discover.jsx` — new file.
- `console/src/pages/SourceEdit.jsx` — reference for form + mutateApi usage pattern.
- `digests/ai/config.json` — target of writes (`sources.twitter.accounts`).

## Verification

1. **Backend smoke**: `curl -X POST localhost:5200/api/digests/ai/discover/twitter -d '{"query":"AI agents","min_followers":5000,"max_tweets":50,"exclude_retweets":true}' -H 'Content-Type: application/json'` — confirm JSON results with follower counts, no accounts already in `digests/ai/config.json:twitter.accounts`.
2. **Frontend E2E**: `digest-pipeline --console --port 5200` + `cd console && npm run dev`. Navigate to `#/ai/discover`, run a search, verify avatars/links/counts render, click an `@username` and confirm it opens the correct x.com profile, click **Add** and confirm (a) the row disappears, (b) the username now appears in `digests/ai/config.json`, (c) it also appears in `#/ai/sources`.
3. **Error paths**: unset `AUTH_TOKEN`, confirm UI shows a clear error. Try adding a duplicate (manually race two clicks) — backend's existing `_validate_source` should reject cleanly.
4. **No regression**: existing Sources tab CRUD still works; `#/ai/sources/new` unaffected.

## Out of scope

- Non-Twitter source types (newsletters, blogs, GitHub) — deferred.
- Per-account config (lookback, max_per_account overrides) — added accounts inherit the digest's existing `twitter.lookback` / `twitter.max_per_account`.
- Caching, search history, undo.
