"""Podcast pipeline: generate a two-host audio podcast from a daily digest."""
import logging
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

from .config import load_config, render_prompt, get_voice_map, get_speaker_tags
from .run_log import RunLog
from . import llm
from . import log
from . import delivery


# Default speaker tags for parse_script when none are provided
_DEFAULT_SPEAKERS = ["ALEX", "SARAH"]


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

        # ── Stage 8: AUDIO ──────────────────────────────────────────────
        mp3_path = podcasts_dir / f"{date}.mp3"
        voice_map = get_voice_map(config, "kokoro")

        t0 = time.time()
        from . import kokoro_tts
        kokoro_tts.configure()
        audio_usage = kokoro_tts.synthesize_script(turns, mp3_path,
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
        landing_url = f"{pages_base}/"
        ET.SubElement(channel, "link").text = landing_url
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


def _update_landing_page(podcasts_dir: Path, config: dict,
                         logger: logging.Logger) -> None:
    """Update the landing page index.html with the latest episode list."""
    try:
        data_root = config["_data_root"]
        podcast_cfg = config.get("podcast", {})
        sub_cfg = config.get("subscriptions", {})
        pages_base = podcast_cfg.get("pages_base_url",
                                      sub_cfg.get("public_base_url",
                                                   "https://aidailyroundup.com"))
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
            ep_html_lines.append(
                f'  <div class="episode">\n'
                f'    <div class="ep-title">{ep_date}</div>\n'
                f'    <audio controls preload="none" '
                f'src="{base_url}/{ep_date}.mp3"></audio>\n'
                f'  </div>'
            )
        ep_block = "\n".join(ep_html_lines)

        html = index_path.read_text()
        html = re.sub(
            r'<!-- EPISODES_START -->.*?<!-- EPISODES_END -->',
            f'<!-- EPISODES_START -->\n{ep_block}\n  <!-- EPISODES_END -->',
            html,
            flags=re.DOTALL,
        )
        index_path.write_text(html)
        logger.info(f"[LANDING] Updated index.html with {len(episodes)} episodes")
    except Exception as e:
        logger.error(f"[LANDING] Landing page update failed: {e}", exc_info=True)


def _git_publish(podcasts_dir: Path, date: str, config: dict,
                 logger: logging.Logger) -> None:
    """Commit and push podcast MP3, script, and RSS feed to the git remote.

    This makes the new episode available via GitHub Pages for podcast app
    subscriptions. Failures are logged but non-fatal.
    """
    try:
        data_root = config["_data_root"]
        repo_root = data_root.parent.parent

        mp3_file = podcasts_dir / f"{date}.mp3"
        txt_file = podcasts_dir / f"{date}.txt"
        xml_file = data_root / "podcast.xml"
        html_file = data_root / "index.html"

        # Stage only the podcast-related files that exist
        files_to_add = [f for f in [mp3_file, txt_file, xml_file, html_file]
                        if f.exists()]
        if not files_to_add:
            logger.warning("[GIT] No podcast files to publish")
            return

        rel_paths = [str(f.relative_to(repo_root)) for f in files_to_add]

        subprocess.run(
            ["git", "add"] + rel_paths,
            cwd=repo_root, capture_output=True, text=True, check=True, timeout=30
        )

        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=repo_root, capture_output=True, timeout=10
        )
        if result.returncode == 0:
            logger.info("[GIT] No changes to commit")
            return

        subprocess.run(
            ["git", "commit", "-m", f"Add podcast episode {date}"],
            cwd=repo_root, capture_output=True, text=True, check=True, timeout=30
        )

        push_result = subprocess.run(
            ["git", "push"],
            cwd=repo_root, capture_output=True, text=True, timeout=60
        )
        if push_result.returncode == 0:
            logger.info(f"[GIT] Podcast {date} published to remote")
        else:
            logger.error(f"[GIT] Push failed: {push_result.stderr[:300]}")
            delivery.send_alert("PUBLISH",
                                f"Git push failed: {push_result.stderr[:200]}", config)
    except Exception as e:
        logger.error(f"[GIT] Publish failed: {e}", exc_info=True)


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
