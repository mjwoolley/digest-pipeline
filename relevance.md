# AI Relevance Filter Plan

## Summary
Add a **RELEVANCE FILTER** stage to the digest pipeline after extract/normalize and before clustering.

The goal is to prevent non-AI stories from slipping into the AI Daily Roundup when generally AI-adjacent sources occasionally publish unrelated content.

The filter should operate on normalized article objects and use a **rules-first** approach with optional **LLM handling for borderline cases**.

---

## Why this stage
This is the right point in the pipeline because by then each article already has normalized fields like:
- `title`
- `description`
- `category`
- source metadata

That makes it possible to do story-level relevance filtering without trusting or rejecting an entire source.

---

## Files likely to change
- `digest_pipeline/relevance.py` *(new)*
- `digest_pipeline/digest.py`
- `digest_pipeline/llm.py`
- `digest_pipeline/prompts/relevance_check.md` *(new)*
- `tests/test_relevance.py` *(new)*
- `tests/test_digest.py`

---

## Proposed design

### New pipeline stage
Insert a **RELEVANCE FILTER** stage between:
- extract/normalize
- clustering

### Config shape
Per digest, add an optional config section:

```json
{
  "relevance_filter": {
    "enabled": true,
    "topic": "artificial intelligence, machine learning, and AI products",
    "keywords_include": [
      "AI", "LLM", "GPT", "Claude", "Anthropic", "OpenAI",
      "agent", "agents", "model", "inference", "training",
      "fine-tuning", "embedding", "multimodal", "copilot",
      "GPU", "MCP", "RAG"
    ],
    "keywords_exclude": [
      "sports", "weather", "horoscope"
    ],
    "borderline_llm": true,
    "borderline_threshold": 0.5
  }
}
```

### Filter behavior
For each article:

1. **Auto-keep**
   - Matches any include keyword in title/description
   - Or has a clearly AI-relevant category

2. **Auto-drop**
   - Matches an exclude keyword and does not match an include keyword

3. **Borderline**
   - Everything else
   - If `borderline_llm` is enabled, send borderline items to a cheap LLM classifier (Haiku)
   - If disabled, keep borderline items conservatively

### LLM borderline check
Add a new prompt file:
- `digest_pipeline/prompts/relevance_check.md`

The prompt should classify each borderline article as relevant or not relevant to the digest topic and return strict JSON.

### Observability
Add strong logging and artifacts:
- log every dropped article with reason
- save `work/{date}/filtered.json`
- include relevance filter stats in run log

This matters because hidden filtering is annoying and impossible to tune.

---

## Test plan

### Unit tests
Add `tests/test_relevance.py` covering:
- auto-keep include keyword match
- auto-drop exclude keyword match
- include keyword overriding exclude keyword
- borderline behavior with LLM disabled
- borderline classification with mocked LLM enabled
- case-insensitive matching
- empty input handling
- `_filter_reason` present on removed items

### Integration tests
Add/update `tests/test_digest.py` to verify:
- `filtered.json` is written when relevance filter is enabled
- stage is skipped when disabled

---

## Risks / open questions
- keyword list maintenance will require tuning
- category-based auto-keep depends on extraction quality
- borderline LLM calls add small cost and latency
- different digests will want different filter configs
- prompt should be resilient to weird article content

---

## Recommendation
Implement both the rules-first filter and the optional LLM fallback.

For the **AI digest**, the preferred rollout is:
- `enabled: true`
- `borderline_llm: true`

Reason:
- rules alone help, but ambiguous AI-adjacent stories are common
- borderline article volume should be small
- Haiku cost should be trivial

This gives better precision than rules-only without adding much complexity.
