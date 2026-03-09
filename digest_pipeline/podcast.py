"""Podcast pipeline: generate a two-host audio podcast from a daily digest."""
import logging
import os
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

from .config import load_config, render_prompt, get_voice_map, get_speaker_tags
from . import cartesia
from . import llm
from . import log
from . import delivery


def main():
    # Parse args
    dry_run = False
    config_path = None
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

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
    tts_backend = podcast_cfg.get("tts_backend", "kokoro")

    if not podcast_cfg.get("enabled", True):
        print("Podcast disabled in config")
        return

    podcasts_dir = data_root / "podcasts"

    logger = log.setup_logger(date, data_root / "logs")
    logger.info(f"[PODCAST] Starting for {date} (dry_run={dry_run})")

    podcasts_dir.mkdir(parents=True, exist_ok=True)

    try:
        # ── Stage 7: SCRIPTGEN ──────────────────────────────────────────
        digest_path = data_root / f"{date}.md"
        if not digest_path.exists():
            raise FileNotFoundError(f"No digest found: {digest_path}")

        digest_text = digest_path.read_text()
        logger.info(f"[SCRIPTGEN] Loaded digest: {len(digest_text)} chars")

        llm.configure(provider)
        prompt = render_prompt("podcast_script.md", config,
                               {"DIGEST": digest_text})

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

        # Parse and validate
        speaker_tags = get_speaker_tags(config)
        turns = cartesia.parse_script(script_text, speaker_tags=speaker_tags)
        if not turns:
            raise ValueError("Script parsing produced no turns")
        logger.info(f"[SCRIPTGEN] Parsed {len(turns)} turns")

        delivery.send_progress("SCRIPTGEN",
                               f"{len(turns)} turns, {len(script_text)} chars",
                               config, {**usage, "duration": scriptgen_duration})

        if dry_run:
            logger.info("[PODCAST] Dry run — skipping TTS and delivery")
            print(f"Script saved to {script_path}")
            print(f"Turns: {len(turns)}")
            for speaker, text in turns:
                print(f"  {speaker}: {text[:80]}...")
            return

        # ── Stage 8: AUDIO ──────────────────────────────────────────────
        mp3_path = podcasts_dir / f"{date}.mp3"
        voice_map = get_voice_map(config, tts_backend)

        # Progress callback: notify every 10 turns
        _audio_start = time.time()
        def _on_tts_progress(turn_num, total, speaker):
            if turn_num == 1 or turn_num % 10 == 0 or turn_num == total:
                elapsed = time.time() - _audio_start
                delivery.send_progress(
                    "AUDIO",
                    f"TTS: turn {turn_num}/{total} ({speaker}) — {elapsed:.0f}s elapsed",
                    config)

        t0 = time.time()
        if tts_backend == "kokoro":
            from . import kokoro_tts
            kokoro_tts.configure()
            audio_usage = kokoro_tts.synthesize_script(turns, mp3_path,
                                                       voice_map=voice_map,
                                                       on_progress=_on_tts_progress)
        else:
            cartesia_key = _load_cartesia_key()
            cartesia.configure(cartesia_key, voice_map=voice_map)
            audio_usage = cartesia.synthesize_script(turns, mp3_path,
                                                     voice_map=voice_map,
                                                     on_progress=_on_tts_progress)
        audio_duration = time.time() - t0

        swap_usage = _get_swap_usage()
        logger.info(f"[AUDIO] Complete in {audio_duration:.1f}s: {audio_usage}")
        delivery.send_progress("AUDIO",
                               f"{audio_usage['duration_seconds']:.0f}s audio in {audio_duration:.0f}s wall time\n"
                               f"TTS backend: {tts_backend}, chars: {audio_usage['total_chars']:,}\n"
                               f"Swap: {swap_usage}",
                               config)

        # ── Stage 9: DELIVER ────────────────────────────────────────────
        t0 = time.time()
        podcast_name = podcast_cfg.get("name", "Daily Brief")
        caption = f"{podcast_name} — {date}"
        success = delivery.send_audio(str(mp3_path), caption=caption,
                                      config=config)
        deliver_duration = time.time() - t0

        if success:
            logger.info(f"[DELIVER] Podcast sent ({deliver_duration:.1f}s)")
            delivery.send_progress("DELIVER",
                                   f"Podcast delivered ({deliver_duration:.1f}s)",
                                   config)
            # Generate/update RSS feed
            _update_rss_feed(podcasts_dir, date, audio_usage, config, logger)
        else:
            logger.error("[DELIVER] Audio send failed")
            delivery.send_alert("DELIVER", "Failed to send podcast audio",
                                config)

    except Exception as e:
        logger.error(f"[PODCAST] Fatal error: {e}", exc_info=True)
        delivery.send_alert("PODCAST", str(e)[:500], config)
        sys.exit(1)


def _update_rss_feed(podcasts_dir: Path, date: str, audio_usage: dict,
                     config: dict, logger: logging.Logger) -> None:
    """Generate/update podcast RSS feed XML from all episodes in podcasts_dir."""
    try:
        podcast_cfg = config.get("podcast", {})
        digest_cfg = config.get("digest", {})
        # Derive relative path from data_root to podcasts_dir for URL construction
        data_root = config["_data_root"]
        rel_podcasts = podcasts_dir.relative_to(data_root.parent.parent)

        # Use GitHub Pages for proper MIME types (audio/mpeg for MP3s)
        pages_base = podcast_cfg.get("pages_base_url",
                                      "https://mjwoolley.github.io/digest-pipeline")
        base_url = f"{pages_base}/{rel_podcasts}"
        feed_url = f"{pages_base}/{rel_podcasts.parent}/podcast.xml"

        title = podcast_cfg.get("name", digest_cfg.get("name", "Daily Brief"))
        description = podcast_cfg.get("description",
                                       f"AI-generated podcast from {title}")
        language = podcast_cfg.get("language", "en")

        # Collect all episodes
        episodes = []
        for mp3 in sorted(podcasts_dir.glob("*.mp3"), reverse=True):
            ep_date = mp3.stem  # e.g. "2026-03-09"
            try:
                ep_dt = datetime.strptime(ep_date, "%Y-%m-%d").replace(
                    hour=12, tzinfo=timezone.utc)
            except ValueError:
                continue
            size = mp3.stat().st_size
            episodes.append({
                "date": ep_date,
                "dt": ep_dt,
                "filename": mp3.name,
                "size": size,
            })

        if not episodes:
            logger.warning("[RSS] No episodes found, skipping feed generation")
            return

        # Build XML
        ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
        ET.register_namespace("itunes", ITUNES_NS)
        rss = ET.Element("rss", version="2.0")
        channel = ET.SubElement(rss, "channel")
        ET.SubElement(channel, "title").text = title
        ET.SubElement(channel, "description").text = description
        ET.SubElement(channel, "language").text = language
        ET.SubElement(channel, "link").text = feed_url
        ET.SubElement(channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}author").text = title
        ET.SubElement(channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}explicit").text = "false"
        image_url = podcast_cfg.get("image_url", "")
        if image_url:
            ET.SubElement(channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}image",
                          href=image_url)
        category = podcast_cfg.get("category", "Technology")
        ET.SubElement(channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}category",
                      text=category)

        for ep in episodes:
            item = ET.SubElement(channel, "item")
            ET.SubElement(item, "title").text = f"{title} — {ep['date']}"
            ET.SubElement(item, "pubDate").text = format_datetime(ep["dt"])
            ET.SubElement(item, "guid", isPermaLink="true").text = (
                f"{base_url}/{ep['filename']}")
            ET.SubElement(item, "enclosure",
                          url=f"{base_url}/{ep['filename']}",
                          length=str(ep["size"]),
                          type="audio/mpeg")

        tree = ET.ElementTree(rss)
        ET.indent(tree, space="  ")
        xml_path = podcasts_dir.parent / "podcast.xml"
        tree.write(xml_path, encoding="unicode", xml_declaration=True)
        logger.info(f"[RSS] Feed updated: {xml_path} ({len(episodes)} episodes)")
    except Exception as e:
        logger.error(f"[RSS] Feed generation failed: {e}", exc_info=True)


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


def _load_cartesia_key() -> str:
    """Load Cartesia API key from environment variable."""
    key = os.environ.get("CARTESIA_API_KEY")
    if not key:
        raise RuntimeError("CARTESIA_API_KEY environment variable not set")
    return key


if __name__ == "__main__":
    main()
