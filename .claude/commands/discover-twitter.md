---
description: Interactively pick suggested Twitter accounts to add to a digest's source list.
argument-hint: <digest-slug>
---

You are running interactive Twitter account discovery for the digest identified
by `$ARGUMENTS` (default `ai` if empty).

Steps:

1. Validate `$ARGUMENTS` is a known digest slug — run `ls digests/` and confirm
   the slug appears. If `$ARGUMENTS` is empty, use `ai`.
2. Run: `digest-pipeline --discover-twitter --digest <slug> --json`
   Capture stdout. If exit code is non-zero or output is not valid JSON, report
   the error (and stderr if relevant) and stop.
3. Parse the JSON. If `candidates` is empty, tell the user the count and any
   errors, then stop.
4. Present a numbered list. For each candidate, show:
   - `N. @<handle>` — `<followers>` followers, `<posts_per_week:.1f>/wk`,
     LLM `<llm_score>/10` (omit the LLM bit if `llm_score` is null)
   - LLM rationale (if present), one line
   - Bio (if present), one line truncated to ~120 chars
   - First 2 tweet samples (from `tweet_samples`), each truncated to ~140 chars,
     bulleted with `   - `
5. Ask the user: "Which would you like to add? (e.g. `1,3,5`, `all`, or `none`)"
6. Parse the reply:
   - `none` or empty → say "OK, no changes" and stop
   - `all` → pick every candidate
   - Comma-separated numbers (1-based) → pick those indices
   - On invalid input, ask once for clarification; if still invalid, stop
7. Run: `digest-pipeline --add-twitter-account <h1> <h2> ... --digest <slug>`
   Report the `Added:` and `Already present:` lines back to the user.

Rules:

- Do NOT auto-pick or guess. Always wait for the user's reply at step 5.
- Do NOT edit `digests/<slug>/config.json` directly — always go through the
  `--add-twitter-account` CLI command for atomic, dedup-safe writes.
- Treat the `--json` output as an internal contract between this command and
  the CLI. Don't reformat it back to JSON for the user — present it as a
  human-readable numbered list.
