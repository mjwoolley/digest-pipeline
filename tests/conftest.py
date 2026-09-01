"""Shared fixtures for the digest-pipeline test suite."""
import json

import pytest


@pytest.fixture
def data_root(tmp_path):
    """A temporary digest data root (the directory containing config.json)."""
    return tmp_path


@pytest.fixture
def sample_config(tmp_path):
    """A minimal but realistic digest config dict, with _data_root injected."""
    return {
        "digest": {
            "name": "Test Digest",
            "tagline": "Testing",
            "emoji": "🧪",
            "tone": "neutral",
            "max_articles": 20,
        },
        "categories": [
            {"id": "models", "label": "Models", "emoji": "🤖", "description": "Model releases"},
            {"id": "tools", "label": "Tools", "emoji": "🔧", "description": "Developer tools"},
        ],
        "llm": {"provider": "openrouter"},
        "sources": {},
        "_data_root": str(tmp_path),
        "_pipeline_dir": str(tmp_path),
    }


@pytest.fixture
def sample_articles():
    """A small set of extracted articles in pipeline schema."""
    return [
        {
            "title": "Acme releases Widget 2.0",
            "description": "Acme announced Widget 2.0 with faster processing.",
            "category": "tools",
            "urls": ["https://acme.example/blog/widget-2"],
            "source_keys": ["blog:acme"],
            "source_labels": ["Acme Blog"],
        },
        {
            "title": "New model tops benchmark",
            "description": "A new open model has topped the leaderboard.",
            "category": "models",
            "urls": ["https://lab.example/model"],
            "source_keys": ["twitter:lab"],
            "source_labels": ["@lab"],
        },
    ]


class CannedLLM:
    """Callable stand-in for llm.chat that returns queued responses.

    Each call pops the next (text, usage) pair; records every call's
    messages/model/max_tokens for assertions.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, messages, model, max_tokens=4096, **kwargs):
        self.calls.append({
            "messages": messages,
            "model": model,
            "max_tokens": max_tokens,
        })
        text = self.responses.pop(0)
        if isinstance(text, tuple):
            return text
        return text, {"input_tokens": 10, "output_tokens": 5}


@pytest.fixture
def canned_llm():
    """Factory for a CannedLLM: canned_llm(['{"a": 1}', ...])."""
    return CannedLLM


def write_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
