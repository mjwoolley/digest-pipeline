"""dedupe_merge provenance must follow the echoed group_id, never position."""
import json
from unittest import mock

from digest_pipeline import llm


CLUSTERS = [
    [  # Group 1: two sources covering story A
        {"title": "Story A", "source_key": "blog:one", "source_label": "One",
         "source_type": "blog", "url": "https://one.example/a"},
        {"title": "Story A again", "source_key": "twitter:two", "source_label": "@two",
         "source_type": "twitter", "url": "https://x.com/two/1"},
    ],
    [  # Group 2: two sources covering story B
        {"title": "Story B", "source_key": "blog:three", "source_label": "Three",
         "source_type": "blog", "url": "https://three.example/b"},
        {"title": "Story B too", "source_key": "blog:four", "source_label": "Four",
         "source_type": "blog", "url": "https://four.example/b"},
    ],
]


def _merge_with(response_articles):
    with mock.patch.object(llm, "chat",
                           return_value=(json.dumps(response_articles), {})):
        merged, _ = llm.dedupe_merge([list(c) for c in CLUSTERS], "2026-08-11",
                                     "{{CLUSTERS}}")
    return merged


def test_provenance_follows_group_id_when_reordered():
    """The LLM returns Group 2's article first — positional matching would
    stamp it with Group 1's sources."""
    merged = _merge_with([
        {"group_id": 2, "title": "Story B merged", "urls": []},
        {"group_id": 1, "title": "Story A merged", "urls": []},
    ])
    by_title = {a["title"]: a for a in merged}
    assert by_title["Story B merged"]["source_keys"] == ["blog:three", "blog:four"]
    assert by_title["Story A merged"]["source_keys"] == ["blog:one", "twitter:two"]


def test_provenance_when_group_dropped():
    """LLM drops Group 1 entirely: the surviving article keeps ITS group's
    sources, and nothing inherits the dropped group's attribution."""
    merged = _merge_with([
        {"group_id": 2, "title": "Story B merged", "urls": []},
    ])
    assert merged[0]["source_keys"] == ["blog:three", "blog:four"]


def test_provenance_when_group_split():
    """A group split into two articles: both carry that group's sources."""
    merged = _merge_with([
        {"group_id": 1, "title": "A part one", "urls": []},
        {"group_id": 1, "title": "A part two", "urls": []},
        {"group_id": 2, "title": "B merged", "urls": []},
    ])
    assert merged[0]["source_keys"] == ["blog:one", "twitter:two"]
    assert merged[1]["source_keys"] == ["blog:one", "twitter:two"]
    assert merged[2]["source_keys"] == ["blog:three", "blog:four"]


def test_missing_group_id_positional_fallback_when_unambiguous():
    merged = _merge_with([
        {"title": "A merged", "urls": []},
        {"title": "B merged", "urls": []},
    ])
    assert merged[0]["source_keys"] == ["blog:one", "twitter:two"]
    assert merged[1]["source_keys"] == ["blog:three", "blog:four"]


def test_missing_group_id_ambiguous_gets_no_attribution():
    """Count mismatch + no ids: wrong attribution is worse than none."""
    merged = _merge_with([
        {"title": "Only one came back", "urls": []},
    ])
    assert "source_keys" not in merged[0]


def test_singletons_bypass_llm():
    singleton = [{"title": "Solo", "source_key": "blog:solo",
                  "source_label": "Solo", "source_type": "blog",
                  "url": "https://solo.example/s"}]
    with mock.patch.object(llm, "chat") as chat:
        merged, usage = llm.dedupe_merge([singleton], "2026-08-11", "{{CLUSTERS}}")
    chat.assert_not_called()
    assert merged[0]["source_keys"] == ["blog:solo"]
    assert merged[0]["urls"] == ["https://solo.example/s"]
