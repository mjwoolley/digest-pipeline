# Configurable Clustering Thresholds Plan

## Summary
Add per-digest configurable clustering thresholds so intra-day clustering and cross-day dedup can use different values.

Current problem:
- `0.85` is hardcoded for both intra-day clustering and cross-day dedup.
- AI appears to benefit from a looser intra-day threshold.
- WealthTech may need different values later.

## Proposed config shape
```json
"clustering": {
  "intra_day_threshold": 0.80,
  "cross_day_threshold": 0.85
}
```

## Files likely to change
- `digest_pipeline/digest.py`
- `digest_pipeline/seen_articles.py`
- `digests/ai/config.json`
- `tests/test_digest.py`
- `tests/test_seen_articles.py` or equivalent

## Design
- Read `config.get("clustering", {})` in `digest.py`
- Default to:
  - `intra_day_threshold = 0.85`
  - `cross_day_threshold = 0.85`
  so existing digests behave the same unless configured
- Use `intra_day_threshold` in the cluster stage
- Use `cross_day_threshold` in the cross-day dedup stage
- Update `seen_articles.py` helper paths so any simulation/test logic can accept an explicit threshold too
- Then add AI-specific config in staging:
  - `intra_day_threshold: 0.80`
  - `cross_day_threshold: 0.85`

## Test plan
- default behavior remains 0.85 / 0.85 when config missing
- partial config works (one key missing falls back)
- explicit AI-style override works
- cross-day helper uses passed threshold
- no regression in existing clustering/dedup tests

## Recommendation
Implement plumbing first with safe defaults, then set AI config to `0.80 / 0.85` in staging and test there before any production rollout.
