from digest_pipeline.relevance import classify_article, filter_articles


def test_include_keyword_keeps_article():
    config = {"relevance_filter": {"enabled": True, "keywords_include": ["Anthropic"], "keywords_exclude": []}}
    article = {"title": "Anthropic launches something", "description": "AI news"}
    decision, reason = classify_article(article, config)
    assert decision == "keep"
    assert "include keyword" in reason


def test_exclude_keyword_drops_article():
    config = {"relevance_filter": {"enabled": True, "keywords_include": [], "keywords_exclude": ["sports"]}}
    article = {"title": "Sports roundup", "description": "Baseball and football"}
    decision, reason = classify_article(article, config)
    assert decision == "drop"
    assert "exclude keyword" in reason


def test_include_overrides_exclude():
    config = {"relevance_filter": {"enabled": True, "keywords_include": ["AI"], "keywords_exclude": ["sports"]}}
    article = {"title": "AI in sports", "description": "AI analytics in baseball"}
    decision, _ = classify_article(article, config)
    assert decision == "keep"


def test_borderline_kept_conservatively_without_llm():
    config = {"relevance_filter": {"enabled": True, "keywords_include": ["AI"], "keywords_exclude": ["sports"], "borderline_llm": False}}
    article = {"title": "Enterprise software update", "description": "General business tooling"}
    kept, removed, _usage = filter_articles([article], config)
    assert len(kept) == 1
    assert len(removed) == 0


def test_removed_articles_get_reason():
    config = {"relevance_filter": {"enabled": True, "keywords_include": [], "keywords_exclude": ["weather"]}}
    article = {"title": "Weather warning", "description": "Storms incoming"}
    kept, removed, _usage = filter_articles([article], config)
    assert len(kept) == 0
    assert removed[0]["_filter_reason"] == "exclude keyword: weather"


def test_borderline_llm_drop(monkeypatch):
    config = {"relevance_filter": {"enabled": True, "keywords_include": [], "keywords_exclude": [], "borderline_llm": True}}
    article = {"title": "Enterprise software update", "description": "General business tooling"}

    def fake_llm(article, config):
        return False, "not primarily AI-related", {"input_tokens": 5, "output_tokens": 2, "cost": 0.0}

    monkeypatch.setattr("digest_pipeline.relevance._llm_relevance_decision", fake_llm)
    kept, removed, _usage = filter_articles([article], config)
    assert len(kept) == 0
    assert removed[0]["_filter_reason"] == "llm: not primarily AI-related"


def test_borderline_llm_keep(monkeypatch):
    config = {"relevance_filter": {"enabled": True, "keywords_include": [], "keywords_exclude": [], "borderline_llm": True}}
    article = {"title": "Enterprise software update", "description": "General business tooling"}

    def fake_llm(article, config):
        return True, "useful AI builder news", {"input_tokens": 5, "output_tokens": 2, "cost": 0.0}

    monkeypatch.setattr("digest_pipeline.relevance._llm_relevance_decision", fake_llm)
    kept, removed, _usage = filter_articles([article], config)
    assert len(kept) == 1
    assert len(removed) == 0


# ── Word-boundary keyword matching ───────────────────────────────────────────
# "AI" as an include keyword previously matched m-ai-n, s-ai-d, em-ai-l, etc.,
# keeping nearly everything and starving both the exclude list and the
# borderline LLM check.

def _cfg(includes=None, excludes=None):
    return {"relevance_filter": {
        "enabled": True,
        "keywords_include": includes or [],
        "keywords_exclude": excludes or [],
    }}


def test_include_keyword_requires_word_boundary():
    article = {"title": "Weather forecast for the main travel season",
               "description": "Rain is available in detail"}
    decision, _ = classify_article(article, _cfg(includes=["AI"]))
    assert decision == "borderline"  # 'main'/'available'/'detail' must NOT match 'AI'


def test_include_keyword_matches_whole_word():
    article = {"title": "New AI model released", "description": ""}
    decision, reason = classify_article(article, _cfg(includes=["AI"]))
    assert decision == "keep"
    assert "ai" in reason


def test_exclude_now_reachable():
    """With substring includes, the exclude branch was effectively dead."""
    article = {"title": "Bitcoin gains amid crypto rally",
               "description": "Contains the word maintain"}
    decision, reason = classify_article(
        article, _cfg(includes=["AI"], excludes=["crypto"]))
    assert decision == "drop"
    assert "crypto" in reason


def test_multiword_keyword_still_matches():
    article = {"title": "Advances in machine learning systems", "description": ""}
    decision, _ = classify_article(article, _cfg(includes=["machine learning"]))
    assert decision == "keep"


def test_borderline_llm_error_keeps_article(monkeypatch):
    """One malformed LLM response must not kill the run — degrade to keep."""
    config = {"relevance_filter": {"enabled": True, "keywords_include": [],
                                   "keywords_exclude": [], "borderline_llm": True}}

    def boom(article, config):
        raise ValueError("no JSON object found")

    monkeypatch.setattr("digest_pipeline.relevance._llm_relevance_decision", boom)
    kept, removed, _usage = filter_articles([{"title": "X"}], config)
    assert len(kept) == 1
    assert removed == []


def test_borderline_usage_accumulated(monkeypatch):
    config = {"relevance_filter": {"enabled": True, "keywords_include": [],
                                   "keywords_exclude": [], "borderline_llm": True}}

    def fake_llm(article, config):
        return True, "ok", {"input_tokens": 100, "output_tokens": 10, "cost": 0.001}

    monkeypatch.setattr("digest_pipeline.relevance._llm_relevance_decision", fake_llm)
    kept, removed, usage = filter_articles([{"title": "X"}, {"title": "Y"}], config)
    assert usage["input_tokens"] == 200
    assert usage["cost"] == 0.002
