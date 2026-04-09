# Podcast Pronunciation Rewrites

Design for a pronunciation-rewrite layer that transforms podcast turn text after script parsing and before TTS synthesis.

## 1. Summary

Add a pronunciation rewrite layer that transforms podcast text after script parsing and before TTS synthesis.

Primary config is a friendly terms map, with optional regex rules as an escape hatch.

Rewrites only affect the ephemeral text sent to Kokoro, not the saved script, digest markdown, RSS, or archive text.

## 2. Files likely to change

- `digest_pipeline/pronunciation.py` (new)
- `digest_pipeline/podcast.py`
- `digests/wealthtech/config.json`
- `digests/ai/config.json`
- `tests/test_pronunciation.py` (new)

No changes to:

- `digest_pipeline/kokoro_tts.py`

## 3. Proposed design

### Config shape

```json
{
  "podcast": {
    "pronunciation": {
      "terms": {
        "401(k)": "four oh one k",
        "401k": "four oh one k",
        "FINRA": "FIN-rah",
        "AWS": "A W S",
        "S&P": "S and P",
        "ETF": "E T F"
      },
      "regex": [
        {"pattern": "\\$(\\d+)B\\b", "replacement": "$1 billion dollars"},
        {"pattern": "\\bQ([1-4])\\b", "replacement": "quarter $1"}
      ]
    }
  }
}
```

### New module: `digest_pipeline/pronunciation.py`

Public functions:

- `build_rewriter(config: dict) -> callable`
- `apply_rewrites(turns: list[tuple[str, str]], rewriter: callable) -> list[tuple[str, str]]`

Behavior:

- Read `podcast.pronunciation` from config
- Apply `terms` first
- Apply `regex` second
- Return rewritten `(speaker, text)` tuples without mutating originals

### Hook point in `podcast.py`

Insert after `parse_script()` and before `synthesize_script()`:

- Build rewriter from config
- Create `tts_turns = apply_rewrites(turns, rewriter)`
- Pass `tts_turns` into Kokoro instead of `turns`

### Precedence rules

1. Per-digest config only
2. Terms before regex
3. Longer terms win before shorter ones
4. Term matching is case-insensitive

## 4. Test plan

New `tests/test_pronunciation.py` covering:

- Empty config passthrough
- No `podcast` section passthrough
- Simple term replacement
- Case-insensitive matching
- Safe escaping of `401(k)` and `S&P`
- Longest-match wins
- Regex rules work
- Regex runs after terms
- `apply_rewrites` returns a new list
- Speaker labels preserved
- Full mixed roundtrip

## 5. Risks / open questions

- `\b` boundaries don't work cleanly for terms like `401(k)` and `S&P`, so implementation should use lookarounds for non-word chars
- Possible accidental rewrites inside URLs — low risk for podcast prose
- No shared glossary file initially, so duplicated terms may exist between digests
- Performance is negligible

## 6. Recommendation

This is the right plan.

My read:

- Good scope
- Good hook point
- Tests are sane
- Friendly config shape is much better than regex-first nonsense

One opinionated note: keeping per-digest config self-contained for now is the better trade, even if it duplicates a few terms. Better than prematurely inventing shared glossary plumbing.
