---
name: diagnose-audit
description: |
  Walk the actionable findings from the weekly source-health audit one
  source at a time. For each flagged source, probe with the right tools
  (Gmail MCP for newsletters, WebFetch/curl for RSS discovery, `bird
  search from:<handle>` for Twitter, file reads for raw fetch artifacts
  and ledger), recommend an action (drop / swap / keep+flag / wait /
  manual), apply the config edit on user approval, then offer a single
  bundled commit at the end. Use when the user asks to "diagnose the
  audit", "walk me through flagged sources", "/diagnose-audit", or
  similar. Never runs the digest pipeline, never pushes, never edits
  anything outside `digests/<slug>/config.json` and
  `digests/<slug>/.source_state.json`.
---

# diagnose-audit — interactive source-health remediation

Formalizes the manual probe-and-fix loop the user has been running by
hand each Sunday. The user receives a terse Telegram audit report,
opens a session inside the digest-pipeline checkout, invokes this
skill, and walks through each flagged source with you.

## Invocation

Triggered by:
- Slash command: `/diagnose-audit`
- Natural-language: "walk me through the audit", "diagnose the audit
  findings", "fix the flagged sources", "diagnose source audit",
  "investigate the audit"
- Optional inline parameters in the user's message:
  - `--digest <slug>` — restrict to one digest (e.g. `ai`, `wealthtech`).
    If omitted and the checkout has multiple digests, ask which.
  - `--digests-dir <path>` — defaults to `./digests`. Useful when running
    against a different data root.
  - `--include-quiet` — also walk findings in the "Expected quiet"
    bucket. Off by default; those findings are informational.

## Pre-flight (run before anything else)

1. **Confirm cwd is a digest-pipeline checkout.** Verify
   `digest_pipeline/source_audit.py` exists. If not, ask the user where
   the checkout lives and `cd` into it.
2. **Verify clean working tree.** Run `git status --porcelain`. If
   output is non-empty (uncommitted edits or untracked files other than
   `.venv`), refuse and ask the user to stash or commit first. Config
   edits must land on a clean baseline so the final diff is reviewable.
3. **Note the current branch and HEAD.** Surface to the user in the
   first message so they can sanity-check they're on the branch they
   meant to be on (e.g. `feat/diagnose-audit-skill` if testing).

## Step 1 — Load the audit report

Run the audit in JSON mode. The CLI flag is documented in
`digest_pipeline/cli.py:14` and the JSON schema is produced by
`digest_pipeline.source_audit.to_json` (lines around 115).

```bash
./.venv/bin/digest-pipeline --audit-sources --json \
    --digests-dir <digests-dir> [--digest <slug>]
```

Parse stdout JSON. Top-level shape is `{<slug>: {<payload>}}`. Each
payload has:

- `digest_name`, `digest_slug`, `date` (audit run date),
  `total_sources`, `healthy_count`, `actionable_count`
- `findings`: list of finding dicts. Each dict has:
  `source_key`, `source_type` (`twitter` | `blog` | `newsletter` |
  `github_trending` | etc.), `name`, `badge` (`healthy` | `warning` |
  `stale`), `cause` (see §5), `evidence`, `suggested_action`,
  `sort_key` (lower = more urgent), `bucket` (`Action recommended` |
  `Yield dropped` | `Expected quiet — no action`).

If multiple digests come back, prompt user via `AskUserQuestion` for
which one to walk. Process one digest per invocation.

## Step 2 — Summarize and confirm scope

Print a short header summarizing what's on the table:

```
Audit for <digest_name> (run <date>):
  • <total_sources> sources, <healthy_count> healthy
  • <N> actionable: by cause: <breakdown>
  • (skipping <M> "Expected quiet" findings — pass --include-quiet to
    include them)
```

Filter findings to walk:
- Always: findings where `bucket == "Action recommended"` or
  `bucket == "Yield dropped"`.
- Plus, if `--include-quiet` was passed: `bucket == "Expected quiet
  — no action"`.
- Sort by `sort_key` ascending (most urgent first), then by
  `source_key` for stability.

If zero findings to walk, say so and exit cleanly. Don't proceed to
the loop.

Otherwise ask the user: "Walk through these N findings now? (yes /
specific subset / abort)". Allow them to choose a subset by index or
by source_key prefix (e.g. "just the twitter ones").

## Step 3 — Per-source loop

For each finding in walking order:

### Print the header

```
═══════════════════════════════════════════════════════════════
[i/N] <source_key>
  type:     <source_type>
  badge:    <badge>
  cause:    <cause>
  evidence: <evidence>
  audit's suggestion: <suggested_action>
```

### Dispatch on `cause` to the right playbook

Each `cause` has its own probes and decision rules (§5 below). After
running the probes, propose ONE recommended action and cite the
evidence inline. Recommendations are one of:

- **drop** — remove from config; source isn't producing useful content.
- **swap** — replace one source entry with another (e.g. newsletter →
  blog/RSS).
- **keep+flag** — leave config alone; user is aware. Optional `_notes`
  field in config (see §6).
- **wait** — within grace window or normal cadence; revisit later.
- **manual** — needs human judgment beyond what probes can reveal.

### Ask for approval

Use `AskUserQuestion` with at least:
- "Approve recommendation: <action>"
- "Modify — I want to do something else"
- "Skip this finding (no action, move on)"
- "Abort the whole walk"

If the user picks "modify", let them describe what they want and apply
their requested action instead.

### Apply the action

See §6.

### Record outcome

Keep a running list of `(source_key, action_taken)` tuples in memory.
Used in §7 for the final commit message.

## Step 4 — Final review

After all findings processed:

1. Print summary table: `source_key → action_taken`.
2. Run `git diff digests/<slug>/config.json` and show it to the user.
3. Run `git diff digests/<slug>/.source_state.json` if state cleanup
   happened.
4. Ask via `AskUserQuestion`: "Commit these changes? (yes / no — keep
   uncommitted / discard everything)".
5. On **commit**: stage and commit per §7. Never push.
6. On **keep uncommitted**: stop here. Tell the user they can review
   and commit when ready.
7. On **discard**: `git checkout -- digests/<slug>/config.json
   digests/<slug>/.source_state.json`. Confirm with the user before
   running.

## Step 5 — Per-cause playbooks

The actual diagnosis recipes. For each `cause`, run the listed probes
and apply the decision table. **Always cite the specific probe result
that drove the recommendation** — don't just say "drop it", say "drop
it because <handle> returned `No tweets found.` 3/3 fetches".

---

### Playbook: `FETCH_EMPTY` for `source_type=newsletter`

Pattern: 3 most recent IMAP fetches returned ≤50 chars (empty
results). The newsletter pipeline searches Gmail INBOX by sender; an
empty result can mean:
- Sender stopped publishing
- Substack unsubscribed the user (Clean Email service is a known
  culprit)
- Gmail filter is auto-archiving past INBOX
- Newsletter migrated to a different sender address

**Probes:**

1. **Gmail search across all folders** — use MCP:
   ```
   mcp__claude_ai_Gmail__search_threads with query:
     from:<sender_addr> newer_than:60d in:anywhere
   ```
   `in:anywhere` includes archive/spam/trash, which catches the
   Clean-Email-auto-archive pattern. Note the date of the most recent
   email if any.

2. **Probe publisher's public site for RSS.** Take the sender's domain
   (parse from the `from:` config field — strip `<addr>` if present).
   Try in this order with `WebFetch`:
   - `https://<domain>/feed`
   - `https://<domain>/rss`
   - `https://<domain>/feed.xml`
   - `https://<domain>/atom.xml`
   - The root page (look for `<link rel="alternate" type="application/rss+xml" ...>`)

   If WebFetch returns 404, try `Bash: curl -sI <url>` to confirm.
   Record the working RSS URL if any.

3. **Confirm the publisher is alive** by checking the public site root
   for a recent post date (use WebFetch with prompt: "list the dates
   and titles of the most recent posts, newest first").

**Decision table:**

| Probe result | Recommendation |
|---|---|
| Gmail has no email in 30+ days AND public site has RSS AND publisher is publishing recent posts | **swap** to blog/RSS |
| Gmail has no email in 30+ days AND no public RSS found | **drop** |
| Gmail HAS recent emails (so IMAP filter / Gmail-side issue) | **drop** (clean-email auto-unsubscribed; surface this to the user, recommend they fix Gmail filters and re-subscribe if they want it back) |
| Publisher's site shows no posts in 60+ days | **drop** (publisher inactive) |

---

### Playbook: `FETCH_EMPTY` for `source_type=twitter`

Pattern: 3 most recent `bird search from:<handle>` calls returned ≤50
chars (typically `No tweets found.`).

**Probes:**

1. **Live `bird` probe of the handle.**
   ```bash
   bird search "from:<handle>" -n 5 --json-full
   ```
   Parse the JSON output (it's an array). Count tweets returned; if
   any, inspect timestamps and content.

2. **Case-insensitive duplicate check.** Read
   `digests/<slug>/config.json`, walk `sources.twitter.accounts`. For
   the failing handle, look for any other entry that matches
   case-insensitively (e.g. `mistralai` vs `MistralAI`). Twitter is
   case-insensitive on lookups but the pipeline keys sources by exact
   case, so duplicates double-fetch and one of the pair will look
   broken.

3. **Ledger sanity.** Read `digests/<slug>/source_history.jsonl`,
   filter for the failing handle. How recent is the last successful
   extract? Compare to the canonical-case sibling if any.

**Decision table:**

| Probe result | Recommendation |
|---|---|
| `bird` returns 0 tweets AND no canonical-case sibling in config | **drop** (handle dead / suspended / typo) |
| `bird` returns 0 tweets AND a canonical-case sibling exists with healthy ledger | **drop** the lowercase/non-canonical one |
| `bird` returns recent tweets normally → audit was wrong about FETCH_EMPTY | **manual** review (look at why the most recent run failed) |

---

### Playbook: `CONTENT_BUT_NO_ARTICLES`

Pattern: raw fetches arrive with normal-size content but the
extractor LLM finds nothing AI-relevant in them. Common for:
- Twitter accounts that shifted to personal/meme content
- Newsletters that drift off-topic

**Probes:**

1. **Sample the actual content.** Find the 3 most recent
   `digests/<slug>/work/<date>/` directories with a non-empty raw file
   for this source. Run:
   ```bash
   ls -t digests/<slug>/work/*/raw-<source_type>-<source_key_suffix>.txt | head -3
   ```
   (`source_key_suffix` is the part after `:` — e.g. for
   `twitter:zenoware`, the file is `raw-twitter-zenoware.txt`.)
   `Read` the first ~2KB of each.

2. **Recent extraction trend.** Read
   `digests/<slug>/source_history.jsonl`, filter for this source.
   Look at the last 30 days: how many days `extracted > 0`? Has the
   number been dropping?

3. **Recent prioritize scores.** Optional but useful: read the last 3
   `digests/<slug>/work/<date>/prioritized.json` if present and check
   the source's drop scores (if any articles were even extracted then
   dropped).

**Decision table:**

| Probe result | Recommendation |
|---|---|
| Recent content is clearly off-topic / personal / meme for ≥7 days | **drop** |
| Recent content is on-topic but extractor LLM rejects everything (e.g. very short tweets, no URL bodies) | **keep+flag** — relevance-filter tuning is out of scope for this skill |
| Recent content is product-promotion / self-marketing dominated | **drop** (user judgment; explain the pattern and recommend drop) |

---

### Playbook: `EXTRACTED_BUT_LOST`

Pattern: articles ARE being extracted from this source, but the
prioritize stage keeps dropping them (in_digest=0).

**Probes:**

1. **Recent prioritize.json scores for the source.** Walk the last 7
   `digests/<slug>/work/<date>/prioritized.json` files (those that
   exist). Filter entries by `source_keys` containing this source's
   key. Note their scores. Use:
   ```bash
   python3 -c "
   import json, glob
   for p in sorted(glob.glob('digests/<slug>/work/*/prioritized.json'))[-7:]:
       data = json.load(open(p))
       date = p.split('/')[-2]
       for a in (data.get('kept', data) if isinstance(data, dict) else data):
           if '<source_key>' in a.get('source_keys', []):
               print(date, a.get('score'), a.get('title', '')[:80])
   "
   ```

2. **Categories.** What categories are these articles being
   classified as? Look for `category` on the article object.

**Decision table:**

| Probe result | Recommendation |
|---|---|
| Scores consistently ≤4 across the window | **drop** |
| Scores mixed (some ≥6, some ≤4) | **keep+flag**; explain that prioritize is just being selective |
| Articles repeatedly land in "other" category | **drop** or **keep+flag**; explain the LLM doesn't think they fit the digest's vertical |

---

### Playbook: `NEVER_YIELDED`

Pattern: source has been configured ≥14 days (past grace window) but
has never produced a ledger row.

**Probes:** Same as either FETCH_EMPTY-newsletter or FETCH_EMPTY-twitter
depending on `source_type`. The user almost certainly misconfigured
something — handle typo, broken URL, dead newsletter.

**Decision table:**

| Probe result | Recommendation |
|---|---|
| Source is reachable and producing content elsewhere | **manual** — pipeline issue, not a source issue |
| Source is dead / wrong handle / no RSS | **drop** (or **swap** if a working alternative exists) |

---

### Playbook: `YIELD_DROP`

Pattern: 14-day extraction rate is <40% of 90-day baseline. The
source isn't dead — just degraded.

**Probes:**

1. **Confirm the trend** by re-reading ledger. The audit's evidence
   field already states the numbers; verify by aggregating from the
   ledger yourself.
2. **Sample recent vs old content.** Read the most recent raw fetch
   and one from ~60 days ago (whichever work-dir is closest). Has
   the content shifted topically?

**Decision table:** Soft flag only. Present the evidence and ask
user. Don't push a strong recommendation. Options:
- **wait** — source might recover
- **drop** — degradation is decisive
- **keep+flag** — note it for next quarter

---

### Playbook: `NOT_FETCHED`

Pattern: no `raw-<type>-<key>.txt` files in recent work-dirs at all.

**Probes:**

1. Confirm the source is still in
   `digests/<slug>/config.json`.
2. Check `digests/<slug>/source_history.jsonl` — does the source key
   appear AT ALL, or is it a ghost from a previously-removed source?

**Decision table:**

| Probe result | Recommendation |
|---|---|
| Source not in config (already dropped) | **manual** — the audit is reading a stale state entry; the user can clean state manually if they care |
| Source in config but not being fetched | **manual** — pipeline bug; surface to user |

---

### Playbook: `UNCLASSIFIED`

Should be rare. If hit, treat as **manual** — print all the evidence
and let the user decide.

## Step 6 — Action application

All edits target `digests/<slug>/config.json` (and possibly
`digests/<slug>/.source_state.json`). Never edit anything else.

### Drop a Twitter handle

The config block is:
```json
    "twitter": {
      "accounts": [
        "...",
        "<handle>",
        "...",
      ],
```

Use the `Edit` tool to remove just `"<handle>",` plus its newline.
Include enough surrounding context in `old_string` to make the match
unique (the surrounding handles).

After the edit, **always validate the JSON immediately**:

```bash
python3 -c "import json; json.load(open('digests/<slug>/config.json')); print('json: OK')"
```

If validation fails, immediately `git checkout --
digests/<slug>/config.json` and ask the user how to proceed. Never
leave a broken config.json.

Then run the state-cleanup helper (see end of §6).

### Drop a newsletter

Newsletter blocks look like:
```json
        "<key>": {
          "name": "<Name>",
          "from": "<sender>",
          "lookback_days": <N>
        },
```

Use `Edit` to remove the whole block (including the trailing comma if
not the last entry, or the leading comma if last). Validate JSON.
Run state-cleanup with `newsletter:<key>`.

### Drop a blog

Same pattern as newsletter but under `sources.blogs.<key>`.

### Swap newsletter → blog (RSS)

Two-step Edit:

1. Remove the newsletter entry under `sources.newsletters.sources`
   (see "Drop a newsletter" above).
2. Add a blog entry under `sources.blogs`. Pattern:
   ```json
       "<key>": {
         "name": "<Name>",
         "feed_url": "<rss_url>",
         "description": "<one-line description>",
         "stale_after_days": <N>
       },
   ```
   Pick a reasonable insertion point (alphabetical or after similar
   sources). Choose `stale_after_days` based on observed publish
   cadence: daily-ish → 3, ~weekly → 7-14, monthly → 30-45.

Validate JSON after each Edit (do it after step 1 AND after step 2).

State cleanup: remove the OLD `newsletter:<key>` from
`.source_state.json` so it doesn't shadow the new `blog:<key>` (which
will populate fresh after the next run).

### Keep+flag (note in config)

Append an optional `_notes` field to the source's config block:

```json
    "_notes": "Flagged 2026-05-14 in audit: <one-line reason>. Revisit after <date>."
```

The pipeline ignores fields starting with `_`. Validate JSON.

If the user prefers no config noise, skip the `_notes` field — just
record the keep+flag decision in your in-memory list and surface it in
the final summary.

### State cleanup helper (run after drop or swap)

```bash
python3 -c "
import json
from pathlib import Path
p = Path('digests/<slug>/.source_state.json')
state = json.loads(p.read_text())
removed = state.pop('<source_key>', None)
p.write_text(json.dumps(state, indent=2))
print('removed' if removed else 'not present', '<source_key>')
"
```

Do NOT prune `source_history.jsonl`; ledger rows are historical
record and don't cause issues.

## Step 7 — Bundled commit

Default message format (the user usually wants this style based on
prior sessions):

```
Source audit cleanup: drop N low-value sources (<date>)

Why: The weekly source-health audit on <date> flagged N sources
under "Action recommended". Diagnosed each with diagnose-audit;
user decisions are captured below. Net effect: <before> -> <after>
actionable findings.

What:
  - DROP twitter:<handle> — <one-line reason from probes>.
  - DROP newsletter:<key> — <one-line reason>.
  - SWAP newsletter:<key> -> blog:<key> using <rss_url> — <reason>.
  - KEEP twitter:<handle> — <reason>; revisit if pattern continues.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

Bash form (use a heredoc):
```bash
git add digests/<slug>/config.json digests/<slug>/.source_state.json
git commit -m "$(cat <<'EOF'
<title>

<body as above>

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

After committing, show `git log -1 --oneline` and note the new
commit SHA. Tell the user to push when they're ready; **never push
yourself**.

## Step 8 — Things this skill refuses to do

If the user asks for any of these mid-flow, decline and explain. They
need different tooling.

- **Run the digest pipeline.** That's `digest-pipeline <config>` —
  separate workflow.
- **Push the commit.** Always user's call.
- **Edit `relevance.md`, prioritize weights, or any non-config file.**
- **Process multiple digest slugs in one invocation.** Run the skill
  again per slug.
- **Make API calls to unsubscribe/resubscribe newsletters.** Print
  instructions for the user to do it manually.
- **Run shell commands the user didn't approve.** Especially anything
  destructive (rm, git reset --hard, etc).

## Operational notes

- **Worktree convention.** When developing/iterating on this skill,
  use a separate git worktree off `origin/master`. The repo lives at
  `/mnt/HC_Volume_105380972/digest-pipeline*` (prod + staging
  checkouts as symlinks). A new worktree under `/home/clawdbot/` is
  the typical pattern — `git worktree add -b <branch>
  /home/clawdbot/digest-pipeline-<name> origin/master`.
- **CLAUDE.md flow.** This skill produces a commit on whatever branch
  the worktree is on. The merge flow from CLAUDE.md still applies for
  promoting changes to prod: branch → merge to master (`--no-ff`,
  Why/What body, Co-Authored-By trailer) → pull on prod.
- **Don't fight the audit.** If the audit's classification looks
  wrong, surface that to the user — but don't try to "correct" it by
  picking a different cause. The audit logic lives in
  `digest_pipeline/source_audit.py` and is the canonical truth for
  this skill.
