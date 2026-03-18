# Phase 2 Digest Subscriptions — Final Implementation Plan

Repo: `/home/clawdbot/digest-pipeline`

This plan covers the subscription backend for the AI Daily Roundup landing page and email delivery flow.

## Goal

Enable real self-serve subscriptions for the digest:

- users can enter an email on the landing page and subscribe
- the backend stores active subscribers
- the digest sends to that subscriber list every time it runs
- emails include a real unsubscribe link
- unsubscribe removes the address from the active list
- maintain durable history of:
  - who subscribed
  - who unsubscribed
  - who each digest was sent to

---

## Final architecture decisions

### Backend shape
Use a **small Flask backend** inside this repo, running on the VPS.

Why:
- cleaner than raw `http.server`
- easier JSON handling, routing, testing, and future maintenance
- minimal additional complexity for a much cleaner implementation

### Deployment/security shape
- backend app binds to **`127.0.0.1` only**
- public exposure happens only through a **tightly scoped reverse proxy**
- do **not** bind the app directly to `0.0.0.0`

### Subscription model
Use **single-step subscription** for Phase 2.

- user submits email
- backend validates and stores it immediately
- no double opt-in for now

### Unsubscribe UX
Use a **confirmation page** flow:

- email link goes to `GET /unsubscribe?token=...`
- page renders confirmation UI
- actual unsubscribe happens via `POST /api/unsubscribe`

This avoids accidental unsubscribes from email/link scanners more safely than immediate GET deletion.

### Storage model
Keep persistence **file-based**.

- active subscribers in JSON
- audit/send history in append-friendly logs

This is appropriate for current scale and matches the existing repo design.

---

## Source-of-truth files likely to change

### Existing files
- `digests/ai/index.html`
  - landing page source-of-truth
- `digest_pipeline/subscribers.py`
  - subscriber storage, token logic, event helpers
- `digest_pipeline/delivery.py`
  - subscriber email fan-out, send logging
- `digest_pipeline/digest.py`
  - integrate real unsubscribe base URL and send-history hooks
- `digest_pipeline/cli.py`
  - optional serve command / backend entrypoint wiring
- `digests/ai/config.json`
  - backend/public URL config and related settings
- `README.md`
  - usage/deployment/docs updates

### New files
- `digest_pipeline/subscription_api.py` or `digest_pipeline/web.py`
  - Flask backend for subscribe/unsubscribe endpoints
- `tests/test_subscribers.py`
- `tests/test_subscription_api.py`
- updates to `tests/test_delivery.py`
- optional deployment docs/snippets for reverse proxy + service setup

### Runtime/state files (not source-of-truth code)
- `digests/ai/subscribers.json`
- `digests/ai/subscription_events.jsonl`
- `digests/ai/send_history.jsonl`

---

## Data/storage plan

### 1. Active subscribers
File: `digests/ai/subscribers.json`

Purpose:
- current truth for who receives the next digest

Proposed shape:

```json
{
  "subscribers": [
    {
      "email": "user@example.com",
      "token": "abc123",
      "status": "active",
      "subscribed_at": "2026-03-17T15:40:00Z",
      "source": "landing-page"
    }
  ]
}
```

Notes:
- preserve backward compatibility with current structure where practical
- if `status` is unnecessary for active-only storage, keep the active file simple and rely on history logs for lifecycle state

### 2. Subscription audit history
File: `digests/ai/subscription_events.jsonl`

Purpose:
- durable record of subscribes/unsubscribes

Example lines:

```json
{"ts":"2026-03-17T15:40:00Z","event":"subscribe","email":"user@example.com","token":"abc123","source":"landing-page"}
{"ts":"2026-03-20T16:02:00Z","event":"unsubscribe","email":"user@example.com","token":"abc123","source":"unsubscribe-link"}
```

### 3. Digest send history
File: `digests/ai/send_history.jsonl`

Purpose:
- durable record of which digest was sent to which recipient

Example lines:

```json
{"ts":"2026-03-17T21:00:00Z","digest_date":"2026-03-17","email":"user@example.com","status":"sent","delivery_method":"agentmail"}
{"ts":"2026-03-17T21:00:01Z","digest_date":"2026-03-17","email":"other@example.com","status":"failed","error":"timeout"}
```

Why JSONL:
- append-friendly
- easy to inspect
- easy to query later
- simpler than introducing SQLite right now

---

## Backend/API plan

### POST `/api/subscribe`
Request body:

```json
{"email":"user@example.com"}
```

Behavior:
- normalize and validate email
- reject invalid input
- detect duplicate active subscriber
- add subscriber if new
- append subscribe event to `subscription_events.jsonl`
- return structured JSON response

### GET `/unsubscribe?token=...`
Behavior:
- validate token existence
- render simple confirmation page
- do not unsubscribe yet

### POST `/api/unsubscribe`
Behavior:
- accept token
- remove subscriber from active list
- append unsubscribe event to `subscription_events.jsonl`
- return confirmation JSON / success response

### Optional health endpoint
- `GET /health`
- only if useful for deployment monitoring
- not necessary for feature completeness

---

## Landing page plan

Update `digests/ai/index.html`:

- keep the current visual layout unless there is a reason to redesign
- replace the Phase 1 fake/manual signup behavior
- replace `mailto:` flow with `fetch()` to backend subscribe endpoint
- add:
  - inline validation
  - loading state
  - success/error message state
- update wording from “request email signup” to a real subscribe action
- remove the Phase 1 honesty note once backend exists

---

## Email pipeline integration plan

### Existing behavior to preserve
The digest already:
- sends to primary configured recipient
- fans out to subscriber emails separately

### Changes
- generate **real unsubscribe URLs** using configured public base URL
- inject those URLs into subscriber email HTML
- on each send attempt, append result to `send_history.jsonl`
- keep main recipient flow intact unless intentionally changed

### Unsubscribe link generation
Replace current `mailto:` behavior with backend URL generation, e.g.:

```text
https://<public-base-url>/unsubscribe?token=<token>
```

### Config addition
Add a config section for subscription/backend settings, something like:

```json
"subscriptions": {
  "public_base_url": "https://example.com",
  "cors_origin": "https://mjwoolley.github.io"
}
```

Final exact nesting can be chosen during implementation to best fit the repo.

---

## Abuse protection / security controls

Minimum protections for Phase 2:

- backend binds to **localhost only**
- reverse proxy exposes only required routes
- server-side email validation
- basic IP rate limiting on subscribe endpoint
- CORS restricted to landing-page origin(s)
- no public admin/debug/list-all-subscribers endpoints
- subscriber storage files never served directly
- request body size kept small

Security posture note:
- do **not** copy any existing “bind to all interfaces and trust firewall magic” pattern
- keep this surface deliberately tiny

---

## Tests to add/update

### New: `tests/test_subscribers.py`
Cover:
- add subscriber success
- duplicate subscribe
- invalid email handling (if implemented there vs API layer)
- remove by token
- remove nonexistent token
- persistence of active subscriber file
- event logging to `subscription_events.jsonl`
- unsubscribe URL generation

### New: `tests/test_subscription_api.py`
Cover:
- subscribe success
- subscribe invalid input
- duplicate subscribe response
- unsubscribe confirmation page GET
- unsubscribe POST success
- unsubscribe invalid token
- CORS behavior
- rate limiting basics

### Update: `tests/test_delivery.py`
Cover:
- send to subscribers logs per-recipient send history
- failures are logged correctly
- unsubscribe URL injection uses configured public base URL

### Possibly update: digest integration tests
Cover:
- digest fan-out still works after subscription changes
- personalized unsubscribe links are included

---

## Docs / deployment updates

Update `README.md` with:
- subscription backend overview
- required config values
- run/deploy instructions
- data files created and what they mean
- how landing page and backend interact

If needed, add deployment reference docs for:
- Flask service startup
- reverse proxy config
- localhost binding expectations

---

## Implementation order

1. **Expand subscriber storage layer**
   - active subscribers
   - event logging helpers
   - send-history helpers

2. **Implement Flask backend**
   - subscribe endpoint
   - unsubscribe confirmation page
   - unsubscribe POST endpoint

3. **Wire landing page to backend**
   - replace fake/manual signup flow

4. **Update digest unsubscribe link generation**
   - use real public base URL

5. **Add send-history logging**
   - record who each digest was sent to and whether it succeeded

6. **Add/update tests**

7. **Update docs/deployment notes**

---

## Explicit non-goals for Phase 2

Not doing these unless separately approved:
- full admin UI
- subscriber export API
- analytics dashboard
- double opt-in workflow
- database migration to SQLite/Postgres
- direct public app binding on `0.0.0.0`

---

## Pre-implementation security reminder

Before exposing the backend publicly, confirm:
- VPS firewall/network posture is verified
- any existing public listeners are understood and intentional
- new backend is bound to localhost only
- reverse proxy exposure is minimal and deliberate

---

## Security addendum — implementation guardrails

These are required security constraints for implementation and deployment.

### Network exposure
- backend must bind to **`127.0.0.1` only**
- do **not** bind directly to `0.0.0.0`
- public access must go through a reverse proxy that exposes only the exact required routes
- do **not** broadly proxy the whole app or all `/api/*` paths

### Allowed public routes
Expose only what is needed for Phase 2:
- `POST /api/subscribe`
- `GET /unsubscribe?token=...`
- `POST /api/unsubscribe`
- optional `GET /health` only if explicitly needed for deployment monitoring

No public routes for:
- subscriber listing
- event history viewing
- send-history viewing
- admin/debug/status dumps
- raw file access

### Token handling
- unsubscribe tokens must be generated with strong cryptographic randomness
- tokens must be opaque and unguessable
- do not derive tokens from email in an obvious/predictable way
- do not expose subscriber email in unsubscribe URLs

### Input handling and abuse protection
- validate email server-side
- enforce a small request body size limit
- add basic rate limiting on subscribe/unsubscribe endpoints
- restrict CORS to the landing-page origin(s), but do not rely on CORS as the only protection
- optional honeypot field is encouraged for the subscribe form

### Error handling
- return generic, non-leaky error responses
- do not expose filesystem paths, stack traces, tokens, or internal config in API responses
- invalid unsubscribe tokens should fail cleanly without revealing internal details

### Data storage and privacy
- runtime files must stay local and must never be served directly by the web server
- ensure runtime state/history files are ignored by git if not already ignored
- keep stored metadata minimal: collect only what is needed for operations/audit
- be cautious about storing unnecessary long-term IP/user-agent history

### Send/history endpoints
- send history and subscriber event history are for local storage/audit only
- do not add read APIs for these logs in Phase 2 without separate review

### Deployment discipline
- prefer a narrow reverse-proxy config that matches explicit paths only
- terminate TLS at the proxy layer
- if reverse proxy adds rate limiting/body-size controls, keep them narrow and explicit
- keep backend service ownership and file permissions limited to the service user where practical

## Approval checkpoint

This is the approved plan baseline for implementation discussion.

If implementation proceeds, work should happen in:
- **Repo:** `/home/clawdbot/digest-pipeline`
- not in the OpenClaw workspace repo itself
