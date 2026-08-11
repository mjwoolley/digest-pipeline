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
