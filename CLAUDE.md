# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Automated news aggregation and podcast generation pipeline. Fetches content from multiple sources (Twitter, Gmail newsletters, blog RSS feeds, GitHub trending), processes it through LLM-based extraction/deduplication, and delivers a formatted digest via email/notifications with an optional podcast.

## Installation

```bash
# Install with uv (recommended)
uv pip install -e .

# Or with pip
pip install -e .
```

## Running the Pipeline

```bash
# Via installed CLI (after pip install)
digest-pipeline /path/to/config.json
digest-pipeline /path/to/config.json --dry-run
digest-pipeline /path/to/config.json --digest-only
digest-pipeline /path/to/config.json --podcast-only

# Via shell script
./run.sh /path/to/config.json [--dry-run]

# Individual modules
python -m digest_pipeline.digest --config config.json [--dry-run]
python -m digest_pipeline.podcast --config config.json [--dry-run] [date]
```

## Testing

```bash
pip install -e ".[dev]"
pytest
```

## Architecture

Six-stage pipeline orchestrated by `digest_pipeline/digest.py`:

1. **GATHER** (`gather.py`) — Concurrent fetch from configured sources using `concurrent.futures`. Each source type has its own fetcher (Twitter via `bird` CLI, Gmail via `gog` CLI, blogs via `curl`, GitHub via HTML parsing). Output: raw text files in `/tmp/digest-{date}/`.

2. **EXTRACT** (`digest.py` + `llm.py`) — Batched Haiku LLM calls to parse raw content into structured `{title, category, description, url, source}` articles. Sources are batched by character count (max 200K chars/batch, ~4 chars/token).

3. **CLUSTER** (`cluster.py`) — Groups similar articles using `text-embedding-3-small` embeddings and centroid-based cosine similarity (threshold 0.85). Pure Python, no sklearn.

4. **DEDUPE** (`digest.py` + `llm.py`) — Single Sonnet call merges articles within each cluster into canonical items.

5. **FORMAT** (`digest.py` + `llm.py`) — Single Sonnet call to summarize and organize by category into final markdown.

6. **DELIVER** (`delivery.py`) — Sends via email (GOG/SMTP/AgentMail), notifications (Telegram/Slack), and archives to `{data_root}/{date}.md`.

**Podcast** (`podcast.py`) is a secondary pipeline: reads archived digest, generates a two-host script via Sonnet, synthesizes audio via Kokoro (local) TTS, outputs MP3 to `{data_root}/podcasts/`. After each episode, generates an RSS feed at `{data_root}/podcast.xml` for podcast app subscriptions via GitHub raw URLs.

**Archive** (`archive.py`) runs after all stages complete (digest + podcast). Commits and pushes daily artifacts to git using a configurable allowlist of file patterns. Disabled by default; enable via the `"archive"` config section.

## Key Module Responsibilities

| Module | Role |
|---|---|
| `digest_pipeline/digest.py` | Main orchestrator, batching logic, pipeline stages |
| `digest_pipeline/gather.py` | Source-specific fetchers, concurrent execution |
| `digest_pipeline/llm.py` | OpenRouter/Anthropic API client, embeddings, token/cost tracking |
| `digest_pipeline/cluster.py` | Embedding-based cosine similarity clustering |
| `digest_pipeline/delivery.py` | Email/notification delivery, progress updates during pipeline |
| `digest_pipeline/podcast.py` | Script generation + TTS audio synthesis |
| `digest_pipeline/archive.py` | Allowlist-based git commit+push of daily artifacts |
| `digest_pipeline/kokoro_tts.py` | Kokoro-82M local TTS (streaming WAV, memory-efficient) |
| `digest_pipeline/subscription_api.py` | Flask API for subscribe/unsubscribe via HTTP |
| `digest_pipeline/config.py` | Config JSON loading, prompt templating, voice mapping |
| `digest_pipeline/log.py` | File + console logging, 30-day auto-cleanup |
| `digest_pipeline/cli.py` | Unified CLI entry point |
| `digest_pipeline/prompts/*.md` | LLM prompt templates with `{{variable}}` placeholders |

## Configuration

All configuration is external via a JSON file passed with `--config`. Key sections: `sources`, `digest`, `categories`, `podcast`, `delivery`, `llm`, `archive`, `subscriptions`. API keys are loaded from environment variables, typically set via `secrets.env` at the repo root.

### Archive Config

```json
{
  "archive": {
    "enabled": true,
    "push": true,
    "branch": "master",
    "commit_message": "Daily archive: {date}",
    "artifacts": [
      "{date}.md",
      "podcasts/{date}.mp3",
      "podcasts/{date}.txt",
      "podcast.xml",
      "index.html",
      ".seen_embeddings.json"
    ]
  }
}
```

All fields are optional. Defaults: `enabled=false`, `push=true`, `commit_message="Daily archive: {date}"`, `artifacts` = the default list above. Set `branch` to push to a specific branch (e.g. `gh-pages`).

### Subscriptions Config

```json
{
  "subscriptions": {
    "public_base_url": "https://example.com",
    "cors_origins": ["https://mjwoolley.github.io"],
    "port": 5100
  }
}
```

- `public_base_url`: Base URL for unsubscribe links in emails. When set, emails contain HTTP unsubscribe links; when empty, falls back to `mailto:`.
- `cors_origins`: Allowed origins for CORS on the subscription API.
- `port`: Port for the Flask subscription API server (default: 5100).

**CLI serve command:** `digest-pipeline <config> --serve [--port PORT]` starts the subscription API on `127.0.0.1`.

**Runtime files** (in `{data_root}/`, gitignored):
- `subscribers.json` — subscriber list
- `subscription_events.jsonl` — subscribe/unsubscribe event log
- `send_history.jsonl` — per-subscriber email delivery log

Required keys in `secrets.env` (at repo root):
- `OPENROUTER_API_KEY` — OpenRouter API key (for LLM and embeddings)
- `ANTHROPIC_API_KEY` — Anthropic API key (if using `"provider": "anthropic"` in config)

## External Dependencies

- **CLI tools**: `curl`, `ffmpeg`, `bird` (Twitter)
- **APIs**: OpenRouter or Anthropic (LLM), OpenAI embeddings (via OpenRouter), Telegram/Slack (notifications)
- **Python packages**: `numpy`, `kokoro`, `flask` (installed automatically via `pip install -e .`)
