"""Podcast pipeline: generate a two-host audio podcast from a daily digest.

Stages: read the archived digest markdown, ask Sonnet for a conversational
script, apply pronunciation rewrites for TTS, synthesize audio with Kokoro,
deliver the MP3, and refresh ``podcast.xml`` + the landing page so podcast
apps and human visitors see the new episode.
"""
import html
import json
import logging
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from email.utils import format_datetime
from pathlib import Path
from .pipeline_date import PIPELINE_TZ, today_str
from . import episode_title
from .config import load_config, render_prompt, get_voice_map, get_speaker_tags
from .run_log import RunLog
from . import llm
from . import log
from . import delivery


# Default speaker tags for parse_script when none are provided
_DEFAULT_SPEAKERS = ["ALEX", "SARAH"]
_PODCAST_TZ = PIPELINE_TZ


def _podcast_pub_datetime(ep_date: str) -> datetime:
    """Return a stable local publication time for a digest date.

    We anchor episodes to noon America/New_York so podcast apps don't invent
    date-rollover weirdness from UTC/server-local conversions.
    """
    local_date = datetime.strptime(ep_date, "%Y-%m-%d").date()
    return datetime.combine(local_date, dt_time(hour=12, minute=0), tzinfo=_PODCAST_TZ)


def parse_script(script_text: str, speaker_tags: list[str] = None) -> list[tuple[str, str]]:
    """Parse a script with SPEAKER: tags into (speaker, text) turns.

    Args:
        script_text: Raw script text
        speaker_tags: Valid speaker tags (e.g. ["ALEX", "SARAH"]).
                      If None, uses default speakers.
    """
    if speaker_tags is None:
        speaker_tags = _DEFAULT_SPEAKERS

    turns = []
    current_speaker = None
    current_lines = []

    for line in script_text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue

        for speaker in speaker_tags:
            if line.startswith(f"{speaker}:"):
                if current_speaker and current_lines:
                    turns.append((current_speaker, " ".join(current_lines)))
                current_speaker = speaker
                current_lines = [line[len(speaker) + 1:].strip()]
                break
        else:
            if current_speaker:
                current_lines.append(line)

    if current_speaker and current_lines:
        turns.append((current_speaker, " ".join(current_lines)))

    return turns


def main():
    """Run the podcast pipeline for one date (defaults to today, US Eastern).

    Reads the config path and optional ``YYYY-MM-DD`` date from ``sys.argv``.
    Failures are non-fatal to the digest pipeline that calls into here — they
    surface as a Telegram alert and a non-zero exit.
    """
    # Parse args
    dry_run = False
    config_path = None
    date = today_str()

    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg == "--dry-run":
            dry_run = True
        elif arg == "--config" and i + 1 < len(sys.argv):
            i += 1
            config_path = sys.argv[i]
        elif arg.endswith(".json"):
            config_path = arg
        elif not arg.startswith("-"):
            date = arg
        i += 1

    if not config_path:
        print("Usage: podcast.py --config /path/to/config.json [--dry-run] [date]")
        sys.exit(1)

    config = load_config(config_path)
    data_root = config["_data_root"]
    podcast_cfg = config.get("podcast", {})
    provider = config.get("llm", {}).get("provider", "openrouter")

    if not podcast_cfg.get("enabled", True):
        print("Podcast disabled in config")
        return

    podcasts_dir = data_root / "podcasts"

    logger = log.setup_logger(date, data_root / "logs")
    logger.info(f"[PODCAST] Starting for {date} (dry_run={dry_run})")

    podcasts_dir.mkdir(parents=True, exist_ok=True)

    run_log = None if dry_run else RunLog(
        podcast_cfg.get("name", "Podcast"), date, podcasts_dir,
        path=podcasts_dir / f"{date}-run.json", pipeline_type="podcast",
    )

    try:
        # ── Stage 7: SCRIPTGEN ──────────────────────────────────────────
        digest_path = data_root / f"{date}.md"
        if not digest_path.exists():
            raise FileNotFoundError(f"No digest found: {digest_path}")

        digest_text = digest_path.read_text()
        logger.info(f"[SCRIPTGEN] Loaded digest: {len(digest_text)} chars")

        llm.configure(provider)
        # Format date as spoken English: "March 28th, 2026"
        _dt = datetime.strptime(date, "%Y-%m-%d")
        _day = _dt.day
        _suffix = "th" if 11 <= _day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(_day % 10, "th")
        spoken_date = f"{_dt.strftime('%B')} {_day}{_suffix}, {_dt.year}"

        prompt = render_prompt("podcast_script.md", config,
                               {"DIGEST": digest_text, "DATE": spoken_date})

        model = llm.MODELS[provider]["sonnet"]
        messages = [
            {"role": "system", "content": f"Today's date: {date}"},
            {"role": "user", "content": prompt},
        ]

        t0 = time.time()
        script_text, usage = llm.chat(messages, model, max_tokens=8192)
        scriptgen_duration = time.time() - t0

        # Save script
        script_path = podcasts_dir / f"{date}.txt"
        script_path.write_text(script_text)
        logger.info(f"[SCRIPTGEN] Script saved: {script_path} ({len(script_text)} chars, {scriptgen_duration:.1f}s)")

        # Episode title — a one-line summary used as the RSS item title, the
        # landing page, and the email subject. The digest stage normally
        # generates this once and writes <date>.title; reuse it so the podcast
        # and email always match. Only generate here if it's missing (e.g. a
        # standalone --podcast-only run with no preceding digest stage).
        title_file = podcasts_dir / f"{date}.title"
        existing_title = (title_file.read_text(errors="replace").strip()
                          if title_file.is_file() else "")
        if existing_title:
            logger.info(f"[TITLE] Reusing title from digest stage: {existing_title}")
        else:
            title_text = _generate_title(digest_text, config, logger)
            if title_text:
                title_file.write_text(title_text + "\n")
                logger.info(f"[TITLE] Episode title: {title_text}")
            else:
                logger.warning("[TITLE] No title generated; feed will fall back to date")

        # Parse and validate
        speaker_tags = get_speaker_tags(config)
        turns = parse_script(script_text, speaker_tags=speaker_tags)
        if not turns:
            raise ValueError("Script parsing produced no turns")
        logger.info(f"[SCRIPTGEN] Parsed {len(turns)} turns")

        if run_log:
            run_log.log_stage("ScriptGen",
                f"Script: {len(script_text)} chars, {len(turns)} turns",
                {"duration": scriptgen_duration,
                 "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens", 0)),
                 "output_tokens": usage.get("output_tokens", usage.get("completion_tokens", 0)),
                 "cost": usage.get("cost", 0.0)})

        if dry_run:
            logger.info("[PODCAST] Dry run — skipping TTS and delivery")
            print(f"Script saved to {script_path}")
            print(f"Turns: {len(turns)}")
            for speaker, text in turns:
                print(f"  {speaker}: {text[:80]}...")
            return

        # ── Pronunciation rewrites (ephemeral, TTS only) ──────────────
        # Rewrites run on a copy of the turns. The original ``turns`` keep
        # their human-readable spelling and are what gets saved as the script
        # transcript above; only ``tts_turns`` (with phonetic spellings) is
        # handed to the synth.
        from . import pronunciation
        rewriter = pronunciation.build_rewriter(config)
        tts_turns = pronunciation.apply_rewrites(turns, rewriter)

        # ── Stage 8: AUDIO ──────────────────────────────────────────────
        mp3_path = podcasts_dir / f"{date}.mp3"
        voice_map = get_voice_map(config, "kokoro")

        t0 = time.time()
        from . import kokoro_tts
        kokoro_tts.configure()
        audio_usage = kokoro_tts.synthesize_script(tts_turns, mp3_path,
                                                   voice_map=voice_map)
        audio_duration = time.time() - t0

        swap_usage = _get_swap_usage()
        logger.info(f"[AUDIO] Complete in {audio_duration:.1f}s: {audio_usage}, Swap: {swap_usage}")

        if run_log:
            run_log.log_stage("Audio",
                f"TTS: {audio_usage['total_chars']} chars, {audio_usage['total_turns']} turns, "
                f"{audio_usage['duration_seconds']:.0f}s audio",
                {"duration": audio_duration})

        # ── Stage 9: DELIVER ────────────────────────────────────────────
        t0 = time.time()
        podcast_name = podcast_cfg.get("name", "The AI Daily Roundup")
        caption = f"{podcast_name} — {date}"
        success = delivery.send_audio(str(mp3_path), caption=caption,
                                      config=config)
        deliver_duration = time.time() - t0

        if success:
            logger.info(f"[DELIVER] Podcast sent ({deliver_duration:.1f}s)")
            # Generate/update RSS feed
            _update_rss_feed(podcasts_dir, date, audio_usage, config, logger)
            _update_landing_page(podcasts_dir, config, logger)
            if run_log:
                run_log.log_stage("Deliver",
                    f"Sent + RSS updated ({deliver_duration:.1f}s)",
                    {"duration": deliver_duration})
        else:
            logger.error("[DELIVER] Audio send failed")
            if run_log:
                run_log.log_error("Deliver", "Audio send failed")
            delivery.send_alert("DELIVER", "Failed to send podcast audio",
                                config)

        if run_log:
            run_log.complete_raw({
                "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens", 0)),
                "output_tokens": usage.get("output_tokens", usage.get("completion_tokens", 0)),
                "cost": round(usage.get("cost", 0.0), 4),
                "audio_duration_s": audio_usage.get("duration_seconds", 0),
                "mp3_size": mp3_path.stat().st_size if mp3_path.exists() else 0,
            })

    except Exception as e:
        logger.error(f"[PODCAST] Fatal error: {e}", exc_info=True)
        if run_log:
            run_log.fail(str(e))
        delivery.send_alert("PODCAST", str(e)[:500], config)
        sys.exit(1)


def _episode_description(podcasts_dir: Path, ep_date: str, max_chars: int = 300) -> str:
    script = podcasts_dir / f"{ep_date}.txt"
    if script.is_file():
        text = " ".join(script.read_text(errors="replace").split())
        if text:
            return text[:max_chars].rstrip() + ("…" if len(text) > max_chars else "")
    return f"Daily AI roundup for {ep_date}"


# Title generation lives in episode_title; kept aliased here under the original
# private names so existing callers (e.g. cli.py --backfill-titles) still work.
_clean_title = episode_title.clean
_generate_title = episode_title.generate


def _episode_title(podcasts_dir: Path, ep_date: str, fallback: str) -> str:
    """Return the stored one-line episode title, or ``fallback`` if none exists."""
    title_file = podcasts_dir / f"{ep_date}.title"
    if title_file.is_file():
        text = " ".join(title_file.read_text(errors="replace").split())
        if text:
            return text
    return fallback


def _episode_duration_s(work_root: Path, ep_date: str, size: int) -> int:
    run_json = work_root / ep_date / "run.json"
    if run_json.is_file():
        try:
            seconds = (json.loads(run_json.read_text()).get("totals") or {}).get("audio_duration_s")
            if seconds:
                return int(round(float(seconds)))
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            pass
    return int(round(size / 16000))  # 128 kbps estimate, matches console_api.py


def _format_duration(seconds: int) -> str:
    seconds = max(int(seconds), 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def _update_rss_feed(podcasts_dir: Path, date: str, audio_usage: dict,
                     config: dict, logger: logging.Logger) -> None:
    """Generate/update podcast RSS feed XML from all episodes in podcasts_dir."""
    try:
        podcast_cfg = config.get("podcast", {})
        digest_cfg = config.get("digest", {})
        sub_cfg = config.get("subscriptions", {})
        # Podcasts are published at the domain root under /podcasts and /podcast.xml
        data_root = config["_data_root"]

        # Use configured public base URL for proper podcast/feed URLs
        pages_base = podcast_cfg.get(
            "pages_base_url",
            sub_cfg.get("public_base_url", "https://aidailyroundup.com")
        ).rstrip("/")
        base_url = f"{pages_base}/podcasts"
        feed_url = f"{pages_base}/podcast.xml"

        title = podcast_cfg.get("name", digest_cfg.get("name", "The AI Daily Roundup"))
        description = podcast_cfg.get("description",
                                       f"AI-generated podcast from {title}")
        language = podcast_cfg.get("language", "en")

        # Collect all episodes
        episodes = []
        work_root = data_root / "work"
        for mp3 in sorted(podcasts_dir.glob("*.mp3"), reverse=True):
            ep_date = mp3.stem  # e.g. "2026-03-09"
            try:
                ep_dt = _podcast_pub_datetime(ep_date)
            except ValueError:
                continue
            size = mp3.stat().st_size
            episodes.append({
                "date": ep_date,
                "dt": ep_dt,
                "filename": mp3.name,
                "size": size,
                "title": _episode_title(podcasts_dir, ep_date,
                                        fallback=f"{title} — {ep_date}"),
                "description": _episode_description(podcasts_dir, ep_date),
                "duration_s": _episode_duration_s(work_root, ep_date, size),
            })

        if not episodes:
            logger.warning("[RSS] No episodes found, skipping feed generation")
            return

        # Build XML
        ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
        ATOM_NS = "http://www.w3.org/2005/Atom"
        ET.register_namespace("itunes", ITUNES_NS)
        ET.register_namespace("atom", ATOM_NS)
        rss = ET.Element("rss", version="2.0")
        channel = ET.SubElement(rss, "channel")
        ET.SubElement(channel, "title").text = title
        ET.SubElement(channel, "description").text = description
        ET.SubElement(channel, "language").text = language
        landing_url = f"{pages_base}/"
        ET.SubElement(channel, "link").text = landing_url
        ET.SubElement(channel, f"{{{ATOM_NS}}}link",
                      rel="self", type="application/rss+xml", href=feed_url)
        ET.SubElement(channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}author").text = title
        ET.SubElement(channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}explicit").text = "false"
        ET.SubElement(channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}type").text = "episodic"
        owner_cfg = podcast_cfg.get("owner", {}) or {}
        owner_email = owner_cfg.get("email", "")
        if owner_email:
            owner_el = ET.SubElement(channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}owner")
            ET.SubElement(owner_el, "{http://www.itunes.com/dtds/podcast-1.0.dtd}name").text = (
                owner_cfg.get("name") or title)
            ET.SubElement(owner_el, "{http://www.itunes.com/dtds/podcast-1.0.dtd}email").text = owner_email
        image_url = podcast_cfg.get("image_url", "")
        if image_url:
            ET.SubElement(channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}image",
                          href=image_url)
        category = podcast_cfg.get("category", "Technology")
        ET.SubElement(channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}category",
                      text=category)

        for ep in episodes:
            item = ET.SubElement(channel, "item")
            ET.SubElement(item, "title").text = ep["title"]
            ET.SubElement(item, "pubDate").text = format_datetime(ep["dt"])
            ET.SubElement(item, "guid", isPermaLink="true").text = (
                f"{base_url}/{ep['filename']}")
            ET.SubElement(item, "description").text = ep["description"]
            ET.SubElement(item, "enclosure",
                          url=f"{base_url}/{ep['filename']}",
                          length=str(ep["size"]),
                          type="audio/mpeg")
            ET.SubElement(item, "{http://www.itunes.com/dtds/podcast-1.0.dtd}duration").text = (
                _format_duration(ep["duration_s"]))

        tree = ET.ElementTree(rss)
        ET.indent(tree, space="  ")
        xml_path = podcasts_dir.parent / "podcast.xml"
        tree.write(xml_path, encoding="unicode", xml_declaration=True)
        logger.info(f"[RSS] Feed updated: {xml_path} ({len(episodes)} episodes)")
    except Exception as e:
        logger.error(f"[RSS] Feed generation failed: {e}", exc_info=True)


def _update_landing_page(podcasts_dir: Path, config: dict,
                         logger: logging.Logger) -> None:
    """Update the landing page index.html with the latest episode list."""
    try:
        data_root = config["_data_root"]
        podcast_cfg = config.get("podcast", {})
        sub_cfg = config.get("subscriptions", {})
        pages_base = podcast_cfg.get("pages_base_url",
                                      sub_cfg.get("public_base_url",
                                                   "https://aidailyroundup.com")).rstrip("/")
        base_url = f"{pages_base}/podcasts"

        index_path = data_root / "index.html"
        if not index_path.exists():
            logger.warning("[LANDING] index.html not found, skipping update")
            return

        # Collect episodes
        episodes = []
        for mp3 in sorted(podcasts_dir.glob("*.mp3"), reverse=True):
            ep_date = mp3.stem
            try:
                datetime.strptime(ep_date, "%Y-%m-%d")
            except ValueError:
                continue
            episodes.append(ep_date)

        # Build episode HTML
        ep_html_lines = []
        for ep_date in episodes:
            ep_title = _episode_title(podcasts_dir, ep_date, fallback=ep_date)
            title_html = html.escape(ep_title)
            # Show the date as a muted subtitle only when it isn't already the title
            # (i.e. when a real one-line title exists).
            date_html = ("" if ep_title == ep_date else
                         f'\n    <div class="ep-date" style="color: var(--muted); '
                         f'font-size: 0.85rem; margin-top: 0.15rem;">{ep_date}</div>')
            ep_html_lines.append(
                f'  <div class="episode">\n'
                f'    <div class="ep-title">{title_html}</div>{date_html}\n'
                f'    <audio controls preload="none" '
                f'src="{base_url}/{ep_date}.mp3"></audio>\n'
                f'  </div>'
            )
        ep_block = "\n".join(ep_html_lines)

        page_html = index_path.read_text()
        # Substitute the PUBLIC_BASE_URL placeholder used in the static parts of
        # the template (RSS input field, feedUrl JS constant, etc). Episode MP3
        # URLs are regenerated below from {base_url}, which is also derived from
        # pages_base, so staging deployments with a different public_base_url
        # get a fully self-consistent landing page.
        page_html = page_html.replace("{{PUBLIC_BASE_URL}}", pages_base)
        page_html = re.sub(
            r'<!-- EPISODES_START -->.*?<!-- EPISODES_END -->',
            f'<!-- EPISODES_START -->\n{ep_block}\n  <!-- EPISODES_END -->',
            page_html,
            flags=re.DOTALL,
        )
        index_path.write_text(page_html)
        logger.info(f"[LANDING] Updated index.html with {len(episodes)} episodes")
    except Exception as e:
        logger.error(f"[LANDING] Landing page update failed: {e}", exc_info=True)


def _get_swap_usage() -> str:
    """Return swap usage summary like '1.7G / 4.0G (43%)'."""
    try:
        meminfo = Path("/proc/meminfo").read_text()
        vals = {}
        for line in meminfo.splitlines():
            if line.startswith(("SwapTotal:", "SwapFree:")):
                parts = line.split()
                vals[parts[0].rstrip(":")] = int(parts[1])
        total_kb = vals.get("SwapTotal", 0)
        free_kb = vals.get("SwapFree", 0)
        used_kb = total_kb - free_kb
        total_gb = total_kb / 1048576
        used_gb = used_kb / 1048576
        pct = (used_kb / total_kb * 100) if total_kb else 0
        return f"{used_gb:.1f}G / {total_gb:.1f}G ({pct:.0f}%)"
    except Exception:
        return "unknown"


if __name__ == "__main__":
    main()
