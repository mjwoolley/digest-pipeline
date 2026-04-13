"""Tests for the pronunciation rewrite layer."""

from digest_pipeline.pronunciation import apply_rewrites, build_rewriter


def test_empty_config_passthrough():
    rewriter = build_rewriter({})
    assert rewriter("hello world") == "hello world"


def test_no_podcast_section_passthrough():
    rewriter = build_rewriter({"digest": {"name": "test"}})
    assert rewriter("hello world") == "hello world"


def test_simple_term_replacement():
    config = {"podcast": {"pronunciation": {"terms": {"ETF": "E T F"}}}}
    rewriter = build_rewriter(config)
    assert rewriter("Buy an ETF today") == "Buy an E T F today"


def test_case_insensitive_matching():
    config = {"podcast": {"pronunciation": {"terms": {"AWS": "A W S"}}}}
    rewriter = build_rewriter(config)
    assert rewriter("Using aws for hosting") == "Using A W S for hosting"
    assert rewriter("AWS is popular") == "A W S is popular"


def test_safe_escaping_401k():
    config = {"podcast": {"pronunciation": {"terms": {"401(k)": "four oh one k"}}}}
    rewriter = build_rewriter(config)
    assert rewriter("Your 401(k) balance") == "Your four oh one k balance"


def test_safe_escaping_s_and_p():
    config = {"podcast": {"pronunciation": {"terms": {"S&P": "S and P"}}}}
    rewriter = build_rewriter(config)
    assert rewriter("The S&P is up") == "The S and P is up"


def test_longest_match_wins():
    config = {
        "podcast": {
            "pronunciation": {
                "terms": {"AI": "A I", "AI agent": "A I agent"}
            }
        }
    }
    rewriter = build_rewriter(config)
    result = rewriter("An AI agent runs AI tasks")
    assert "A I agent" in result
    assert result == "An A I agent runs A I tasks"


def test_regex_rules_work():
    config = {
        "podcast": {
            "pronunciation": {
                "regex": [{"pattern": "\\$([0-9]+)B\\b", "replacement": "\\1 billion dollars"}]
            }
        }
    }
    rewriter = build_rewriter(config)
    assert rewriter("Worth $5B today") == "Worth 5 billion dollars today"


def test_regex_runs_after_terms():
    config = {
        "podcast": {
            "pronunciation": {
                "terms": {"MSFT": "Microsoft"},
                "regex": [{"pattern": "Microsoft stock", "replacement": "Microsoft shares"}],
            }
        }
    }
    rewriter = build_rewriter(config)
    # Terms run first (MSFT -> Microsoft), then regex (Microsoft stock -> Microsoft shares)
    assert rewriter("MSFT stock is up") == "Microsoft shares is up"


def test_apply_rewrites_returns_new_list():
    config = {"podcast": {"pronunciation": {"terms": {"ETF": "E T F"}}}}
    rewriter = build_rewriter(config)
    original = [("ALEX", "Buy an ETF"), ("SARAH", "Good idea")]
    result = apply_rewrites(original, rewriter)
    assert result is not original
    assert original[0] == ("ALEX", "Buy an ETF")  # not mutated
    assert result[0] == ("ALEX", "Buy an E T F")


def test_speaker_labels_preserved():
    config = {"podcast": {"pronunciation": {"terms": {"ETF": "E T F"}}}}
    rewriter = build_rewriter(config)
    turns = [("ALEX", "ETF news"), ("SARAH", "More ETF talk")]
    result = apply_rewrites(turns, rewriter)
    assert result[0][0] == "ALEX"
    assert result[1][0] == "SARAH"


def test_ipa_markup_replacement():
    config = {
        "podcast": {
            "pronunciation": {
                "terms": {
                    "LLM": "[LLM](/ɛlɛlɛm/)",
                    "API": "[API](/eɪpiːaɪ/)",
                }
            }
        }
    }
    rewriter = build_rewriter(config)
    assert rewriter("The LLM uses an API") == "The [LLM](/ɛlɛlɛm/) uses an [API](/eɪpiːaɪ/)"


def test_full_mixed_roundtrip():
    config = {
        "podcast": {
            "pronunciation": {
                "terms": {
                    "401(k)": "four oh one k",
                    "S&P": "S and P",
                    "ETF": "E T F",
                    "FINRA": "FIN-rah",
                },
                "regex": [
                    {"pattern": "\\bQ([1-4])\\b", "replacement": "quarter \\1"},
                ],
            }
        }
    }
    rewriter = build_rewriter(config)
    turns = [
        ("ALEX", "The S&P is up in Q1, and FINRA updated 401(k) rules."),
        ("SARAH", "ETF inflows hit records."),
    ]
    result = apply_rewrites(turns, rewriter)
    assert result[0] == (
        "ALEX",
        "The S and P is up in quarter 1, and FIN-rah updated four oh one k rules.",
    )
    assert result[1] == ("SARAH", "E T F inflows hit records.")
