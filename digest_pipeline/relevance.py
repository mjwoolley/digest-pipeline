"""Story-level relevance filtering for digest articles."""
from __future__ import annotations

import json
from typing import Any

from . import llm
from .config import render_prompt


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def _article_text(article: dict[str, Any]) -> str:
    parts = [
        article.get("title", ""),
        article.get("description", ""),
        article.get("category", ""),
        article.get("source_label", ""),
        article.get("source_type", ""),
    ]
    return "\n".join(p for p in parts if p)


def classify_article(article: dict[str, Any], config: dict[str, Any]) -> tuple[str, str]:
    """Classify article as keep/drop/borderline using rules only."""
    rf = config.get("relevance_filter", {})
    text = _norm(_article_text(article))
    includes = [_norm(k) for k in rf.get("keywords_include", []) if k]
    excludes = [_norm(k) for k in rf.get("keywords_exclude", []) if k]

    include_hits = [k for k in includes if k and k in text]
    exclude_hits = [k for k in excludes if k and k in text]

    if include_hits:
        return "keep", f"include keyword: {include_hits[0]}"
    if exclude_hits:
        return "drop", f"exclude keyword: {exclude_hits[0]}"
    return "borderline", "no relevance rule matched"


def _extract_json_object(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("empty response")
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"no JSON object found in: {text[:200]}")
    return json.loads(text[start:end+1])


def _llm_relevance_decision(article: dict[str, Any], config: dict[str, Any]) -> tuple[bool, str]:
    rf = config.get("relevance_filter", {})
    provider = config.get("llm", {}).get("provider", "openrouter")
    topic = rf.get("topic", "artificial intelligence, machine learning, and AI products")
    prompt = render_prompt(
        "relevance_check.md",
        config,
        {
            "TOPIC": topic,
            "ARTICLE": json.dumps(
                {
                    "title": article.get("title", ""),
                    "description": article.get("description", ""),
                    "category": article.get("category", ""),
                    "source_label": article.get("source_label", ""),
                    "source_type": article.get("source_type", ""),
                },
                indent=2,
            ),
        },
    )
    model = llm.MODELS[provider]["haiku"]
    messages = [{"role": "user", "content": prompt}]
    text, _usage = llm.chat(messages, model, max_tokens=300)
    data = _extract_json_object(text)
    return bool(data.get("relevant", False)), data.get("reason", "LLM relevance check")


def filter_articles(articles: list[dict[str, Any]], config: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Filter articles via rules-first relevance logic.

    Returns (kept, removed). Removed items get `_filter_reason` metadata.
    Borderline items can optionally be checked with a cheap LLM classifier.
    """
    rf = config.get("relevance_filter", {})
    if not rf.get("enabled", False):
        return articles, []

    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    borderline_llm = rf.get("borderline_llm", False)

    for article in articles:
        decision, reason = classify_article(article, config)
        if decision == "drop":
            removed.append({**article, "_filter_reason": reason})
        elif decision == "keep":
            kept.append(article)
        else:
            if borderline_llm:
                relevant, llm_reason = _llm_relevance_decision(article, config)
                if relevant:
                    kept.append(article)
                else:
                    removed.append({**article, "_filter_reason": f"llm: {llm_reason}"})
            else:
                kept.append(article)

    return kept, removed
