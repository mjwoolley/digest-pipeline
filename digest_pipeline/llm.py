"""OpenRouter / Anthropic API client for chat completions and embeddings."""
import json
import logging
import time
import urllib.request
import urllib.error

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
        "embedding": "openai/text-embedding-3-small",
    },
    "anthropic": {
        "haiku": "claude-haiku-4-5-20251001",
        "sonnet": "claude-sonnet-4-6-20250514",
        "embedding": "openai/text-embedding-3-small",  # always via OpenRouter
    },
}

# Pricing per million tokens (OpenRouter rates)
PRICING = {
    "anthropic/claude-haiku-4.5": {"input": 0.80, "output": 4.00},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "anthropic/claude-sonnet-4.6": {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-6-20250514": {"input": 3.00, "output": 15.00},
    "openai/text-embedding-3-small": {"input": 0.02, "output": 0.0},
}

TIMEOUT = 300
MAX_RETRIES = 1
RETRY_BACKOFF = 5

_keys: dict[str, str] = {}


def configure(provider: str = "openrouter"):
    """Set the chat provider and load API keys."""
    global _provider, _keys
    _provider = provider
    _keys = _load_keys()
    logger.info(f"[LLM] Provider: {_provider}")


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

    Returns (articles, usage). Uses Haiku.
    """
    model = MODELS[_provider]["haiku"]

    # Build source content with SRC reference tokens
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
    text, usage = chat(messages, model, max_tokens=65536)
    articles = _parse_json_array(text)

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
    Returns (merged_articles, usage). Uses Sonnet.

    Source provenance (source_keys, source_labels) is collected by code
    from the input articles in each cluster, not by the LLM.
    """
    model = MODELS[_provider]["sonnet"]

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
    merged = _parse_json_array(text)

    # Stamp source provenance from input clusters onto merged articles
    for i, article in enumerate(merged):
        if i < len(multi_clusters):
            prov = _collect_source_provenance(multi_clusters[i])
            article.update(prov)
        # Remove any per-article source fields the LLM might have echoed
        for field in ("source_key", "source_type", "source_label", "source_url"):
            article.pop(field, None)

    return singletons + merged, usage


def prioritize_score(articles: list[dict], prompt: str) -> tuple[list[dict], dict]:
    """Score articles by importance for prioritization.
    Returns (scored_list, usage) where scored_list is [{"title": str, "score": int}, ...].
    Uses Haiku.
    """
    model = MODELS[_provider]["haiku"]
    messages = [
        {"role": "user", "content": prompt},
    ]
    text, usage = chat(messages, model, max_tokens=4096)
    scored = _parse_json_array(text)
    return scored, usage


def summarize_format(articles: list[dict], date: str,
                     prompt_template: str) -> tuple[str, dict]:
    """Summarize each article and format the digest.
    Returns (formatted_message, usage). Uses Sonnet.
    """
    model = MODELS[_provider]["sonnet"]
    articles_str = json.dumps(articles, indent=2)
    prompt = prompt_template.replace("{{ARTICLES}}", articles_str).replace("{{DATE}}", date)

    messages = [
        {"role": "system", "content": f"Today's date: {date}"},
        {"role": "user", "content": prompt},
    ]
    text, usage = chat(messages, model, max_tokens=8192)
    return text, usage


# ── Helpers ──────────────────────────────────────────────────────────────────

def _request_with_retry(url: str, payload: bytes,
                        headers: dict) -> dict:
    """Make HTTP request with 1 retry on 5xx errors."""
    last_err = None
    for attempt in range(1 + MAX_RETRIES):
        req = urllib.request.Request(url, data=payload, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            logger.warning(f"[LLM] HTTP {e.code} (attempt {attempt+1}): {body[:200]}")
            if e.code >= 500 and attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF)
                last_err = e
                continue
            raise RuntimeError(f"API error {e.code}: {body[:500]}") from e
        except urllib.error.URLError as e:
            logger.warning(f"[LLM] URL error (attempt {attempt+1}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF)
                last_err = e
                continue
            raise RuntimeError(f"API connection error: {e}") from e
    raise RuntimeError(f"API request failed after retries: {last_err}")


def _parse_json_array(text: str) -> list[dict]:
    """Parse a JSON array from LLM output, stripping markdown fences."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
        logger.warning(f"[LLM] Expected JSON array, got {type(result).__name__}")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"[LLM] JSON parse error: {e}\nRaw text: {text[:500]}")
        return []


def _calc_cost(model: str, usage: dict) -> float:
    """Calculate cost from usage dict and model pricing."""
    prices = PRICING.get(model, {"input": 0, "output": 0})
    inp = usage.get("prompt_tokens", usage.get("input_tokens", 0))
    out = usage.get("completion_tokens", usage.get("output_tokens", 0))
    return (inp * prices["input"] + out * prices["output"]) / 1_000_000
