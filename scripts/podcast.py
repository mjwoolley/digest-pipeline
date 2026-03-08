"""Podcast pipeline: generate a two-host audio podcast from a daily digest."""
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PIPELINE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PIPELINE_DIR / "scripts"))

from config import load_config, render_prompt, get_voice_map, get_speaker_tags
import cartesia
import llm
import log
import delivery


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

    archive_dir = data_root / "archive"
    audio_dir = archive_dir / "audio"

    logger = log.setup_logger(date, data_root / "logs")
    logger.info(f"[PODCAST] Starting for {date} (dry_run={dry_run})")

    audio_dir.mkdir(parents=True, exist_ok=True)

    try:
        # ── Stage 7: SCRIPTGEN ──────────────────────────────────────────
        digest_path = archive_dir / f"{date}.md"
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
        script_path = audio_dir / f"{date}.txt"
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
        mp3_path = audio_dir / f"{date}.mp3"
        voice_map = get_voice_map(config, tts_backend)

        t0 = time.time()
        if tts_backend == "kokoro":
            import kokoro_tts
            kokoro_tts.configure()
            audio_usage = kokoro_tts.synthesize_script(turns, mp3_path,
                                                       voice_map=voice_map)
        else:
            cartesia_key = _load_cartesia_key()
            cartesia.configure(cartesia_key, voice_map=voice_map)
            audio_usage = cartesia.synthesize_script(turns, mp3_path,
                                                     voice_map=voice_map)
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
        else:
            logger.error("[DELIVER] Audio send failed")
            delivery.send_alert("DELIVER", "Failed to send podcast audio",
                                config)

    except Exception as e:
        logger.error(f"[PODCAST] Fatal error: {e}", exc_info=True)
        delivery.send_alert("PODCAST", str(e)[:500], config)
        sys.exit(1)


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
    """Load Cartesia API key from auth-profiles.json."""
    data = json.loads(llm.AUTH_PROFILES.read_text())
    profile = data["profiles"].get("cartesia:default")
    if not profile:
        raise RuntimeError("No cartesia:default profile in auth-profiles.json")
    return profile["token"]


if __name__ == "__main__":
    main()
