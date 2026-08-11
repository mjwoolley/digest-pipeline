"""Story-level relevance filtering for digest articles."""
from __future__ import annotations

import json
import logging
from typing import Any

from . import llm
from .config import render_prompt
from .util import extract_json_object

logger = logging.getLogger("digest")


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


def _llm_relevance_decision(article: dict[str, Any], config: dict[str, Any]
                            ) -> tuple[bool, str, dict]:
    """Borderline classification via one cheap LLM call.

    Returns (relevant, reason, usage).
    """
    rf = config.get("relevance_filter", {})
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
    model = llm.model_for("relevance")
    messages = [{"role": "user", "content": prompt}]
    text, usage = llm.chat(messages, model, max_tokens=300)
    data = extract_json_object(text)
    return (bool(data.get("relevant", False)),
            data.get("reason", "LLM relevance check"),
            usage)


def filter_articles(articles: list[dict[str, Any]], config: dict[str, Any]
                    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict]:
    """Filter articles via rules-first relevance logic.

    Returns (kept, removed, usage). Removed items get `_filter_reason`
    metadata; usage aggregates the borderline-LLM calls (previously
    discarded, understating the reported run cost).

    A malformed LLM response for one article degrades to keeping that
    article — it must not kill the whole run.
    """
    rf = config.get("relevance_filter", {})
    total_usage = {"input_tokens": 0, "output_tokens": 0, "cost": 0.0}
    if not rf.get("enabled", False):
        return articles, [], total_usage

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
                try:
                    relevant, llm_reason, usage = _llm_relevance_decision(
                        article, config)
                    total_usage["input_tokens"] += usage.get(
                        "prompt_tokens", usage.get("input_tokens", 0))
                    total_usage["output_tokens"] += usage.get(
                        "completion_tokens", usage.get("output_tokens", 0))
                    total_usage["cost"] += usage.get("cost", 0.0)
                except Exception as e:
                    logger.warning(
                        f"[RELEVANCE] LLM check failed for "
                        f"'{article.get('title', '(untitled)')}': {e} — keeping")
                    relevant, llm_reason = True, "llm check failed, kept by default"
                if relevant:
                    kept.append(article)
                else:
                    removed.append({**article, "_filter_reason": f"llm: {llm_reason}"})
            else:
                kept.append(article)

    return kept, removed, total_usage
