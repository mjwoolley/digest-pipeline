"""OpenRouter / Anthropic API client for chat completions and embeddings."""
import json
import logging
import random
import time
import urllib.request
import urllib.error

from .util import extract_json_array

logger = logging.getLogger("digest")

# ── Configuration ────────────────────────────────────────────────────────────

# Provider config: set via configure()
_provider = "openrouter"  # "openrouter" or "anthropic"

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_EMBED_URL = "https://openrouter.ai/api/v1/embeddings"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

MODELS = {
    "openrouter": {
        "haiku": "anthropic/claude-haiku-4.5",
        "sonnet": "anthropic/claude-sonnet-4.6",
        "opus": "anthropic/claude-opus-5",
        "embedding": "openai/text-embedding-3-small",
    },
    "anthropic": {
        "haiku": "claude-haiku-4-5",
        "sonnet": "claude-sonnet-4-6",
        "opus": "claude-opus-5",
        "embedding": "openai/text-embedding-3-small",  # always via OpenRouter
    },
}

# Which model tier each pipeline stage runs on by default. The writing
# stages (reader-visible prose: merged descriptions, summaries and
# why-it-matters, podcast script) get Opus; mechanical extraction,
# classification, and scoring stay on Haiku. Override per digest via the
# config's llm.models block, e.g. {"format": "sonnet"} or a full model id.
STAGE_DEFAULTS = {
    "extract": "haiku",
    "relevance": "haiku",
    "prioritize": "haiku",
    "title": "haiku",
    "discovery": "haiku",
    "same_story": "haiku",
    "dedupe": "opus",
    "format": "opus",
    "podcast": "opus",
}

# Pricing per million tokens (OpenRouter rates)
PRICING = {
    "anthropic/claude-haiku-4.5": {"input": 0.80, "output": 4.00},
    "claude-haiku-4-5": {"input": 0.80, "output": 4.00},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "anthropic/claude-sonnet-4.6": {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "anthropic/claude-opus-5": {"input": 5.00, "output": 25.00},
    "claude-opus-5": {"input": 5.00, "output": 25.00},
    "openai/text-embedding-3-small": {"input": 0.02, "output": 0.0},
}

TIMEOUT = 300
MAX_RETRIES = 4
RETRY_BACKOFF = 5   # base seconds; grows exponentially with jitter
RETRY_MAX_SLEEP = 60

_keys: dict[str, str] = {}
_stage_overrides: dict[str, str] = {}


def configure(provider: str = "openrouter", stage_models: dict = None):
    """Set the chat provider, per-stage model overrides, and load API keys.

    stage_models maps a stage name (see STAGE_DEFAULTS) to a tier name
    ("haiku"/"sonnet"/"opus") or a full model id.
    """
    global _provider, _keys, _stage_overrides
    _provider = provider
    _stage_overrides = dict(stage_models or {})
    _keys = _load_keys()
    logger.info(f"[LLM] Provider: {_provider}"
                + (f" | model overrides: {_stage_overrides}" if _stage_overrides else ""))


def model_for(stage: str) -> str:
    """Resolve the model id for a pipeline stage."""
    choice = _stage_overrides.get(stage) or STAGE_DEFAULTS.get(stage, "haiku")
    return MODELS[_provider].get(choice, choice)


def _load_keys() -> dict[str, str]:
    """Load API keys from environment variables (set via secrets.env at repo root).

    Environment variables:
        OPENROUTER_API_KEY — OpenRouter API key
        ANTHROPIC_API_KEY  — Anthropic API key
    """
    import os
    keys = {}

    if os.environ.get("OPENROUTER_API_KEY"):
        keys["openrouter"] = os.environ["OPENROUTER_API_KEY"]
    if os.environ.get("ANTHROPIC_API_KEY"):
        keys["anthropic"] = os.environ["ANTHROPIC_API_KEY"]

    return keys


# ── Chat Completions ─────────────────────────────────────────────────────────

def chat(messages: list[dict], model: str, max_tokens: int = 4096) -> tuple[str, dict]:
    """Call chat completions API. Returns (response_text, usage_dict)."""
    if _provider == "anthropic" and not model.startswith("openai/"):
        return _chat_anthropic(messages, model, max_tokens)
    return _chat_openrouter(messages, model, max_tokens)


def _chat_openrouter(messages: list[dict], model: str,
                     max_tokens: int) -> tuple[str, dict]:
    """OpenRouter chat completions (OpenAI-compatible)."""
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_keys['openrouter']}",
    }
    data = _request_with_retry(OPENROUTER_URL, payload, headers)
    # OpenRouter returns HTTP 200 with an error envelope (and no choices) on
    # upstream provider errors, moderation blocks, and credit exhaustion.
    if not isinstance(data, dict) or "choices" not in data or not data["choices"]:
        err = (data or {}).get("error") if isinstance(data, dict) else None
        preview = json.dumps(err or data, ensure_ascii=False)[:500]
        raise RuntimeError(f"OpenRouter returned no choices: {preview}")
    finish_reason = data["choices"][0].get("finish_reason", "")
    if finish_reason == "length":
        logger.warning(f"[LLM] Output truncated (hit max_tokens={max_tokens})")
    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    usage["cost"] = _calc_cost(model, usage)
    return text, usage


def _chat_anthropic(messages: list[dict], model: str,
                    max_tokens: int) -> tuple[str, dict]:
    """Anthropic Messages API."""
    # Convert OpenAI-style messages to Anthropic format
    system = ""
    api_messages = []
    for m in messages:
        if m["role"] == "system":
            system = m["content"]
        else:
            api_messages.append({"role": m["role"], "content": m["content"]})

    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": api_messages,
    }
    if system:
        body["system"] = system

    payload = json.dumps(body).encode()
    headers = {
        "Content-Type": "application/json",
        "x-api-key": _keys["anthropic"],
        "anthropic-version": "2023-06-01",
    }
    data = _request_with_retry(ANTHROPIC_URL, payload, headers)
    if data.get("stop_reason") == "max_tokens":
        logger.warning(f"[LLM] Output truncated (hit max_tokens={max_tokens})")
    text = data["content"][0]["text"]
    usage = {
        "input_tokens": data["usage"]["input_tokens"],
        "output_tokens": data["usage"]["output_tokens"],
        "prompt_tokens": data["usage"]["input_tokens"],
        "completion_tokens": data["usage"]["output_tokens"],
    }
    usage["cost"] = _calc_cost(model, usage)
    return text, usage


# ── Embeddings ───────────────────────────────────────────────────────────────

def embed(texts: list[str]) -> tuple[list[list[float]], dict]:
    """Get embeddings via OpenRouter (text-embedding-3-small).
    Returns (embeddings, usage).
    """
    model = MODELS["openrouter"]["embedding"]
    payload = json.dumps({
        "model": model,
        "input": texts,
    }).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_keys['openrouter']}",
    }
    char_lengths = [len(t or "") for t in texts]
    total_chars = sum(char_lengths)
    max_chars = max(char_lengths) if char_lengths else 0
    avg_chars = round(total_chars / len(char_lengths), 1) if char_lengths else 0
    logger.info(
        f"[EMBED] Requesting {len(texts)} embeddings via {model} "
        f"(total_chars={total_chars}, avg_chars={avg_chars}, max_chars={max_chars}, timeout={TIMEOUT}s)"
    )
    t0 = time.time()
    try:
        data = _request_with_retry(OPENROUTER_EMBED_URL, payload, headers)
    except Exception as e:
        dur = time.time() - t0
        logger.error(
            f"[EMBED] Failed after {dur:.1f}s for {len(texts)} texts "
            f"(total_chars={total_chars}, max_chars={max_chars}): {e}"
        )
        raise
    dur = time.time() - t0
    if not isinstance(data, dict):
        logger.error(f"[EMBED] Unexpected non-dict response type: {type(data).__name__}")
        raise RuntimeError(f"Embedding API returned non-object response: {type(data).__name__}")
    if "data" not in data:
        preview = json.dumps(data, ensure_ascii=False)[:1000]
        logger.error(
            f"[EMBED] Response missing 'data' key after {dur:.1f}s "
            f"for {len(texts)} texts: {preview}"
        )
        raise RuntimeError("Embedding API returned unexpected response without 'data'")
    if not isinstance(data["data"], list):
        preview = json.dumps(data, ensure_ascii=False)[:1000]
        logger.error(f"[EMBED] Response 'data' is not a list: {preview}")
        raise RuntimeError("Embedding API returned unexpected 'data' payload type")
    embeddings = [item["embedding"] for item in data["data"]]
    usage = data.get("usage", {})
    usage["cost"] = _calc_cost(model, usage)
    logger.info(
        f"[EMBED] Received {len(embeddings)} embeddings in {dur:.1f}s"
    )
    return embeddings, usage


# ── High-Level Functions ─────────────────────────────────────────────────────

def extract_normalize(sources_batch: list[dict], date: str,
                      prompt_template: str) -> tuple[list[dict], dict]:
    """Extract articles from raw sources, normalize to JSON.

    Sources are labeled with compact reference tokens (SRC1, SRC2, ...) in the
    prompt. The model emits src_ref per article; code maps it back to the
    gathered source provenance (source_key, source_type, source_label, source_url).

    Returns (articles, usage).
    """
    model = model_for("extract")

    # Build source content with SRC reference tokens.
    # The model only sees opaque ``SRC1`` / ``SRC2`` labels and is asked to
    # echo the matching ref on each article it emits. Code (not the LLM)
    # then maps each ref back to the real provenance fields. This keeps the
    # source URL, key, and label out of the model's reach so it can't
    # hallucinate or rewrite them.
    source_texts = []
    src_ref_map = {}  # SRC1 -> source provenance dict
    src_idx = 0
    for s in sources_batch:
        content = s.get("content", "").strip()
        if not content:
            continue
        src_idx += 1
        ref = f"SRC{src_idx}"
        src_ref_map[ref] = {
            "source_key": s["source_key"],
            "source_type": s["source_type"],
            "source_label": s["source_label"],
            "source_url": s.get("source_url", ""),
        }
        source_texts.append(
            f"### {ref}: {s['source_label']} (type: {s['source_type']})\n{content}")

    if not source_texts:
        return [], {"input_tokens": 0, "output_tokens": 0, "cost": 0}

    sources_str = "\n\n".join(source_texts)
    prompt = prompt_template.replace("{{SOURCES}}", sources_str)

    messages = [
        {"role": "system", "content": f"Today's date: {date}"},
        {"role": "user", "content": prompt},
    ]
    # Haiku 4.5 caps output at 64K; the article JSON for a batch is far
    # smaller than the raw input, so 32K is generous headroom.
    text, usage = chat(messages, model, max_tokens=32768)
    articles = _parse_json_array(text, strict=True)

    # Stamp source provenance from gathered sources, replacing src_ref
    stamped = []
    for article in articles:
        ref = article.pop("src_ref", "")
        provenance = src_ref_map.get(ref)
        if provenance:
            article.update(provenance)
            stamped.append(article)
        else:
            logger.warning(f"[EXTRACT] Dropping article with unknown src_ref '{ref}': '{article.get('title', '')}'")

    return stamped, usage


def _collect_source_provenance(articles: list[dict]) -> dict:
    """Collect unique source provenance from a list of articles.

    Returns dict with source_keys and source_labels lists.
    """
    seen_keys = []
    seen_labels = []
    for a in articles:
        sk = a.get("source_key", "")
        sl = a.get("source_label", "")
        if sk and sk not in seen_keys:
            seen_keys.append(sk)
        if sl and sl not in seen_labels:
            seen_labels.append(sl)
    return {"source_keys": seen_keys, "source_labels": seen_labels}


def dedupe_merge(clusters: list[list[dict]], date: str,
                 prompt_template: str) -> tuple[list[dict], dict]:
    """Merge articles within each cluster into canonical items.
    Returns (merged_articles, usage).

    Source provenance (source_keys, source_labels) is collected by code
    from the input articles in each cluster, not by the LLM.
    """
    model = model_for("dedupe")

    # Only send multi-article clusters for merging; pass through singletons
    singletons = []
    multi_clusters = []
    for cluster in clusters:
        if len(cluster) == 1:
            art = cluster[0]
            if "url" in art and "urls" not in art:
                art["urls"] = [art.pop("url")]
            # Collect source provenance into lists
            prov = _collect_source_provenance(cluster)
            art.update(prov)
            # Remove per-article source fields (replaced by list fields)
            for field in ("source_key", "source_type", "source_label", "source_url"):
                art.pop(field, None)
            singletons.append(art)
        else:
            multi_clusters.append(cluster)

    if not multi_clusters:
        return singletons, {"input_tokens": 0, "output_tokens": 0, "cost": 0}

    # Format clusters for the prompt
    cluster_strs = []
    for i, cluster in enumerate(multi_clusters, 1):
        items = json.dumps(cluster, indent=2)
        cluster_strs.append(f"## Group {i} ({len(cluster)} articles)\n{items}")

    clusters_str = "\n\n".join(cluster_strs)
    prompt = prompt_template.replace("{{CLUSTERS}}", clusters_str)

    messages = [
        {"role": "system", "content": f"Today's date: {date}"},
        {"role": "user", "content": prompt},
    ]
    text, usage = chat(messages, model, max_tokens=8192)
    merged = _parse_json_array(text, strict=True)

    # Stamp source provenance from input clusters onto merged articles,
    # matched by the group_id the model echoes. Positional matching silently
    # mis-attributed sources whenever the model reordered, split, or dropped
    # a group.
    for i, article in enumerate(merged):
        gid = article.pop("group_id", None)
        prov = None
        if isinstance(gid, int) and 1 <= gid <= len(multi_clusters):
            prov = _collect_source_provenance(multi_clusters[gid - 1])
        elif len(merged) == len(multi_clusters):
            # 1:1 output without usable ids — positional is unambiguous
            logger.warning(f"[DEDUPE] Missing/invalid group_id "
                           f"'{gid}' — falling back to positional match")
            prov = _collect_source_provenance(multi_clusters[i])
        else:
            logger.warning(f"[DEDUPE] Cannot attribute sources for "
                           f"'{article.get('title', '(untitled)')}' "
                           f"(group_id={gid!r}, {len(merged)} merged vs "
                           f"{len(multi_clusters)} groups)")
        if prov:
            article.update(prov)
        # Remove any per-article source fields the LLM might have echoed
        for field in ("source_key", "source_type", "source_label", "source_url"):
            article.pop(field, None)

    return singletons + merged, usage


def prioritize_score(articles: list[dict], prompt: str) -> tuple[list[dict], dict]:
    """Score articles by importance for prioritization.
    Returns (scored_list, usage) where scored_list is [{"id": int, "score": int}, ...].
    """
    model = model_for("prioritize")
    messages = [
        {"role": "user", "content": prompt},
    ]
    text, usage = chat(messages, model, max_tokens=4096)
    scored = _parse_json_array(text)
    return scored, usage


def same_story_check(candidates: list[dict],
                     prompt_template: str) -> tuple[set[int], dict]:
    """Adjudicate grey-zone cross-day duplicate candidates in one Haiku call.

    candidates: [{index, title, description, match_title, similarity}, ...]
    Returns (indices judged to be the same story, usage).
    """
    model = model_for("same_story")
    pairs = [
        {
            "id": c["index"],
            "candidate_title": c["title"],
            "candidate_description": c["description"],
            "previously_published_title": c["match_title"],
        }
        for c in candidates
    ]
    prompt = prompt_template.replace("{{PAIRS}}", json.dumps(pairs, indent=2))
    text, usage = chat([{"role": "user", "content": prompt}], model,
                       max_tokens=2048)
    results = _parse_json_array(text)
    dup_ids = {
        r["id"] for r in results
        if isinstance(r, dict) and r.get("same_story") is True
        and isinstance(r.get("id"), int)
    }
    return dup_ids, usage


def summarize_format(articles: list[dict], date: str,
                     prompt_template: str) -> tuple[str, dict]:
    """Summarize each article and format the digest.
    Returns (formatted_message, usage).
    """
    model = model_for("format")
    articles_str = json.dumps(articles, indent=2)
    prompt = prompt_template.replace("{{ARTICLES}}", articles_str).replace("{{DATE}}", date)

    messages = [
        {"role": "system", "content": f"Today's date: {date}"},
        {"role": "user", "content": prompt},
    ]
    text, usage = chat(messages, model, max_tokens=8192)
    return text, usage


# ── Helpers ──────────────────────────────────────────────────────────────────

def _retry_sleep_seconds(attempt: int, retry_after: str = None) -> float:
    """Backoff for a retry: honor Retry-After when given, else exponential
    with jitter (5s, 10s, 20s, 40s… capped)."""
    if retry_after:
        try:
            return min(max(float(retry_after), 1.0), RETRY_MAX_SLEEP)
        except ValueError:
            pass
    delay = min(RETRY_BACKOFF * (2 ** attempt), RETRY_MAX_SLEEP)
    return delay + random.uniform(0, delay * 0.25)


def _request_with_retry(url: str, payload: bytes,
                        headers: dict) -> dict:
    """Make an HTTP request, retrying 429s, 5xx, and connection errors.

    429 was previously not retried at all — one rate-limit hit on any of the
    per-run LLM calls killed the whole day's digest.
    """
    last_err = None
    for attempt in range(1 + MAX_RETRIES):
        req = urllib.request.Request(url, data=payload, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            logger.warning(f"[LLM] HTTP {e.code} (attempt {attempt+1}): {body[:200]}")
            retryable = e.code == 429 or e.code >= 500
            if retryable and attempt < MAX_RETRIES:
                sleep = _retry_sleep_seconds(attempt, e.headers.get("Retry-After")
                                             if e.headers else None)
                logger.info(f"[LLM] Retrying in {sleep:.0f}s")
                time.sleep(sleep)
                last_err = e
                continue
            raise RuntimeError(f"API error {e.code}: {body[:500]}") from e
        except urllib.error.URLError as e:
            logger.warning(f"[LLM] URL error (attempt {attempt+1}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(_retry_sleep_seconds(attempt))
                last_err = e
                continue
            raise RuntimeError(f"API connection error: {e}") from e
    raise RuntimeError(f"API request failed after retries: {last_err}")


def _parse_json_array(text: str, strict: bool = False) -> list[dict]:
    """Parse a JSON array from LLM output.

    strict=True raises on failure — used on stages where silently returning
    [] would make an entire batch of content vanish and ship a thin digest
    with no error. Non-strict callers degrade gracefully instead.
    """
    try:
        return extract_json_array(text)
    except ValueError as e:
        if strict:
            raise RuntimeError(f"LLM returned unparseable JSON: {e}") from e
        logger.error(f"[LLM] JSON parse error: {e}")
        return []


def _calc_cost(model: str, usage: dict) -> float:
    """Calculate cost from usage dict and model pricing."""
    prices = PRICING.get(model, {"input": 0, "output": 0})
    inp = usage.get("prompt_tokens", usage.get("input_tokens", 0))
    out = usage.get("completion_tokens", usage.get("output_tokens", 0))
    return (inp * prices["input"] + out * prices["output"]) / 1_000_000
