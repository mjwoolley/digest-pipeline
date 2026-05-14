# Interactive Twitter Discovery via Slash Command

## Context

The `digest-pipeline --discover-twitter` flow surfaces a ranked list of candidate Twitter accounts (with LLM relevance scoring) and pushes the result to Telegram. Currently the "add this candidate to my digest" step is manual: read the Telegram message, copy a handle, edit `digests/<slug>/config.json` by hand.

Goal: make adding candidates one keystroke per pick, invokable from inside a Claude Code chat session via `/discover-twitter <slug>`. The slash command wraps the existing discovery output and lets Claude orchestrate the pick-and-add loop with the user.

Per user preference (recorded 2026-05-14):
- **UX**: "Show all first, then pick by number" — print the full numbered list once, prompt for selections like `1,3,5` or `all` or `none`.
- **CLI**: invoke via a slash command rather than `--interactive` flag on the CLI.

This branch (`interactive-discovery`) is isolated so another concurrent session in `~/digest-pipeline-staging` can work in parallel without colliding.

## User flow

1. User types `/discover-twitter ai` in any Claude Code session anchored in this repo.
2. Claude reads `.claude/commands/discover-twitter.md`, which instructs it to:
   - Run `digest-pipeline --discover-twitter --digest ai --json` and capture stdout.
   - Parse the JSON report, present a numbered list to the user with each candidate's handle, followers, posts/wk, LLM score, rationale, bio, and a few sample tweets.
   - Ask "Which to add? (e.g. `1,3,5`, `all`, or `none`):"
   - Parse the user's reply.
   - Call `digest-pipeline --add-twitter-account <handle> [<handle> ...] --digest ai` once with the full list of picks.
   - Confirm what was added vs skipped (e.g. already in the list).

## Python changes

### `digest_pipeline/twitter_discovery.py`
- Add `tweet_samples: list[str]` field to `Candidate`. Populate it in `_rank()` from the matching `_Author.tweet_texts`. Cap the stored samples at ~5 to keep the JSON output bounded.
- Add `DiscoveryReport.to_dict()` returning a JSON-serializable dict (candidates, errors, keywords_searched, skipped_existing, generated_at). Use `dataclasses.asdict` or a hand-rolled converter that handles the `Candidate.matched_keywords` list cleanly.

### `digest_pipeline/cli.py`
- **New mode for `--discover-twitter`**: `--json` flag. When present, skip Telegram delivery and the `print` formatting; instead write `json.dumps(report.to_dict(), indent=2)` to stdout. Suppress all log output to stderr only so stdout is clean JSON.
- **New flag `--add-twitter-account`**: top-level dispatch alongside `--audit-sources` etc.
  - Args: `--digest <slug>` (required), one or more positional handles (or `--handle` repeated), optional `--dry-run` and `--digests-dir`.
  - Loads digest config path, calls `config_writer.add_twitter_accounts(...)`, prints a summary table (`added: [...]`, `already_present: [...]`).
  - Exit code 0 on success; non-zero if config file missing or write failed.

### New module `digest_pipeline/config_writer.py`
- Pure helper, no CLI argv parsing.
- `add_twitter_accounts(config_path: Path, handles: list[str], dry_run: bool=False) -> AddResult`
  - Reads `config_path` fresh (so concurrent edits in another session aren't clobbered).
  - Computes `to_add` (case-insensitively not already in `sources.twitter.accounts`) and `already_present`.
  - Appends `to_add` (preserving the input casing the user provided) to the accounts list.
  - Writes via temp file + `os.replace()` for atomicity. Uses `json.dumps(cfg, indent=2)` to preserve existing formatting style.
  - Returns `AddResult(added=[...], already_present=[...], config_path=str)`.
- Validates: config exists and has the `sources.twitter` structure; handles list is non-empty.

### `.claude/commands/discover-twitter.md` (new)
Project-local slash command. Content (literal text Claude follows when the user types `/discover-twitter <slug>`):

```
You are running interactive Twitter account discovery for the digest identified
by $ARGUMENTS (default `ai`).

Steps:
1. Validate $ARGUMENTS is a known digest slug — run `ls digests/` and confirm.
2. Run: `digest-pipeline --discover-twitter --digest <slug> --json`
   Capture stdout. If exit code is non-zero or output is not valid JSON, report
   the error and stop.
3. Parse the JSON. If `candidates` is empty, tell the user and stop.
4. Present a numbered list. For each candidate, show:
   - `N. @<handle>` — `<followers>` followers, `<posts_per_week>/wk`,
     LLM `<llm_score>/10`
   - LLM rationale (if present), one line
   - Bio (if present), one line truncated to ~120 chars
   - First 2 tweet samples, each truncated to ~140 chars, bulleted
5. Ask the user: "Which would you like to add? (e.g. `1,3,5`, `all`, or `none`)"
6. Parse the reply:
   - `none` or empty → say "OK, no changes" and stop
   - `all` → pick every candidate
   - Comma-separated numbers → pick those indices (1-based)
   - On invalid input, ask once for clarification then stop if still invalid
7. Run: `digest-pipeline --add-twitter-account <h1> <h2> ... --digest <slug>`
   Report the `added` and `already_present` lists back to the user.

Do NOT auto-pick or guess. Always wait for the user's reply at step 5.
Do NOT edit config.json directly — always go through the CLI command for safety.
```

## Tests

### `tests/test_config_writer.py` (new)
- Atomic write: simulate a crash mid-write by mocking `os.replace` to raise, verify original config unchanged.
- Case-insensitive dedup: adding "Karpathy" when "karpathy" is present → `already_present`, no duplicate appended.
- Order preserved: input order of new handles is preserved in the output config.
- Dry run: returns `AddResult` but file unchanged.
- Missing `sources.twitter` block: raises a clear error rather than corrupting the file.

### `tests/test_twitter_discovery.py` (extend)
- `Candidate.tweet_samples` populated from `_Author.tweet_texts`.
- `DiscoveryReport.to_dict()` round-trips through JSON.
- JSON output omits the internal `_Author` dict.

### `tests/test_cli_discover_json.py` (new) — small smoke
- Run `cli.main()` with `["--discover-twitter", "--json", ...]` and mocked discover → assert stdout is valid JSON and stderr has logs.

## Critical files

| Path | Change |
|---|---|
| `digest_pipeline/twitter_discovery.py` | `Candidate.tweet_samples`, `DiscoveryReport.to_dict()` |
| `digest_pipeline/cli.py` | `--json` flag on discovery; new `--add-twitter-account` handler |
| `digest_pipeline/config_writer.py` | **NEW** — atomic accounts-list writer |
| `.claude/commands/discover-twitter.md` | **NEW** — slash command instructions |
| `tests/test_config_writer.py` | **NEW** |
| `tests/test_twitter_discovery.py` | extend |
| `tests/test_cli_discover_json.py` | **NEW** |
| `README.md` | mention `/discover-twitter` slash command + `--add-twitter-account` flag |
| `BACKLOG.md` | mark the "Twitter discovery in console" item as superseded by slash command |

## Risks

1. **Concurrent config edits.** Another Claude session in `~/digest-pipeline-staging` may also edit `digests/ai/config.json`. Mitigation: `config_writer` re-reads on every call and writes via `os.replace()` for atomic publish. A genuine race window remains (TOCTOU between read and replace) but the worst case is one session's add overwriting the other's; for a single-user repo this is acceptable. If it becomes a real problem, add a file lock (`fcntl.flock`) around the read-modify-write.

2. **Slash command location convention.** Project-local slash commands typically live at `.claude/commands/<name>.md`, but the exact path Claude Code reads from can vary. Verify in a quick test session (`/discover-twitter ai`) before declaring it shipped. If `.claude/commands/` isn't picked up, fall back to a global `~/.claude/commands/discover-twitter.md` and note the path in README.

3. **JSON output stability.** Treat the JSON schema as an internal contract between the CLI and the slash command, not a public API. Don't promise stability in the README.

4. **Empty candidates.** If `min_relevance` filters everything out, the slash command should say "0 candidates after LLM filter — try lowering `min_relevance` in config" rather than silently exiting.

## Verification plan

1. **Unit tests**: `pytest tests/ -q` — must pass all (current 351 + ~6 new).
2. **JSON output smoke**:
   `DIGEST_ENV=staging digest-pipeline --discover-twitter --digest ai --json | jq .`
   — stdout parses, has `candidates` array, no log noise.
3. **Add command dry-run**:
   `digest-pipeline --add-twitter-account testhandle --digest ai --dry-run`
   — reports what would be added, doesn't touch the file.
4. **Add command live (then revert)**:
   `digest-pipeline --add-twitter-account testhandle --digest ai`
   — config.json gets the handle appended in order. `git checkout -- digests/ai/config.json` to undo.
5. **Slash command end-to-end**: `/discover-twitter ai` in a fresh Claude Code session. Pick a couple of numbers. Verify the final config has them. Revert.
6. **Merge & promote**: per CLAUDE.md flow on this branch — logical commits → `--no-ff` merge to master → push → pull into `~/digest-pipeline`.

## Out of scope

- A pure-CLI `--interactive` flag that drives the loop entirely from `input()` calls. The user picked the slash-command UX explicitly.
- Editing other parts of config.json (newsletters, blogs, etc.) via the CLI.
- Bulk-import handles from a file.
- Slash command for non-AI digests beyond passing the slug. The same command supports any digest via `$ARGUMENTS`.
