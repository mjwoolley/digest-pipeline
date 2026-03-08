"""Cartesia TTS client for podcast audio generation."""
import json
import logging
import struct
import subprocess
import tempfile
import time
import urllib.request
import urllib.error
import wave
from pathlib import Path

logger = logging.getLogger("digest")

TTS_URL = "https://api.cartesia.ai/tts/bytes"
CARTESIA_VERSION = "2024-06-10"
MAX_CHUNK_CHARS = 480  # API limit is 500, leave margin
SAMPLE_RATE = 44100
SAMPLE_WIDTH = 2  # 16-bit
CHANNELS = 1

# Default voice IDs — can be overridden via voice_map parameter
DEFAULT_VOICES = {
    "ALEX": "794f9389-aac1-45b6-b726-9d9369183238",
    "SARAH": "a0e99841-438c-4a64-b679-ae501e7d6091",
}

TIMEOUT = 60
MAX_RETRIES = 1
RETRY_BACKOFF = 5

_api_key: str = ""
_voice_map: dict = {}


def configure(api_key: str, voice_map: dict = None):
    """Set the Cartesia API key and optional voice mapping."""
    global _api_key, _voice_map
    _api_key = api_key
    _voice_map = voice_map or DEFAULT_VOICES
    logger.info("[CARTESIA] Configured")


def synthesize_script(turns: list[tuple[str, str]], output_path: Path,
                      voice_map: dict = None) -> dict:
    """Synthesize a parsed script into an MP3 file.

    Args:
        turns: List of (speaker, text) tuples
        output_path: Path for the output MP3 file
        voice_map: Optional speaker->voice_id mapping (overrides configure())

    Returns:
        Usage dict with character count and duration info
    """
    voices = voice_map or _voice_map or DEFAULT_VOICES
    all_pcm = bytearray()
    total_chars = 0
    pause_samples = int(SAMPLE_RATE * 0.4)  # 400ms pause between turns
    pause_bytes = b"\x00\x00" * pause_samples

    for i, (speaker, text) in enumerate(turns):
        voice_id = voices.get(speaker, list(voices.values())[0])
        chunks = _chunk_text(text)
        logger.info(f"[AUDIO] Turn {i+1}/{len(turns)}: {speaker} ({len(chunks)} chunk(s), {len(text)} chars)")

        turn_pcm = _synthesize_turn(voice_id, chunks)
        all_pcm.extend(turn_pcm)

        # Add pause between turns (not after last)
        if i < len(turns) - 1:
            all_pcm.extend(pause_bytes)

        total_chars += len(text)

    # Write WAV then convert to MP3
    wav_path = output_path.with_suffix(".wav")
    _write_wav(wav_path, bytes(all_pcm))
    _convert_to_mp3(wav_path, output_path)

    # Clean up temp WAV
    wav_path.unlink(missing_ok=True)

    duration_seconds = len(all_pcm) / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)
    logger.info(f"[AUDIO] Complete: {total_chars} chars, {duration_seconds:.0f}s, saved to {output_path}")

    return {
        "total_chars": total_chars,
        "total_turns": len(turns),
        "duration_seconds": duration_seconds,
    }


def _synthesize_turn(voice_id: str, chunks: list[str]) -> bytes:
    """Synthesize one speaker turn, using continuations for multi-chunk turns."""
    pcm_parts = []
    context_id = None

    for i, chunk in enumerate(chunks):
        is_last = (i == len(chunks) - 1)

        body: dict = {
            "model_id": "sonic-2",
            "transcript": chunk,
            "voice": {"mode": "id", "id": voice_id},
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": SAMPLE_RATE,
            },
            "language": "en",
        }

        if context_id:
            body["context_id"] = context_id
        if len(chunks) > 1 and not is_last:
            body["continue"] = True

        pcm_data, new_context_id = _tts_request(body)
        pcm_parts.append(pcm_data)

        if new_context_id:
            context_id = new_context_id

    return b"".join(pcm_parts)


def _tts_request(body: dict) -> tuple[bytes, str | None]:
    """Make a single TTS request. Returns (pcm_bytes, context_id)."""
    payload = json.dumps(body).encode()
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": _api_key,
        "Cartesia-Version": CARTESIA_VERSION,
    }

    last_err = None
    for attempt in range(1 + MAX_RETRIES):
        req = urllib.request.Request(TTS_URL, data=payload, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                context_id = resp.headers.get("X-Context-Id")
                return resp.read(), context_id
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace")
            logger.warning(f"[CARTESIA] HTTP {e.code} (attempt {attempt+1}): {err_body[:200]}")
            if e.code >= 500 and attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF)
                last_err = e
                continue
            raise RuntimeError(f"Cartesia API error {e.code}: {err_body[:500]}") from e
        except urllib.error.URLError as e:
            logger.warning(f"[CARTESIA] URL error (attempt {attempt+1}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF)
                last_err = e
                continue
            raise RuntimeError(f"Cartesia connection error: {e}") from e

    raise RuntimeError(f"Cartesia request failed after retries: {last_err}")


def _chunk_text(text: str) -> list[str]:
    """Split text into chunks of <=MAX_CHUNK_CHARS on sentence boundaries."""
    text = text.strip()
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]

    chunks = []
    while text:
        if len(text) <= MAX_CHUNK_CHARS:
            chunks.append(text)
            break

        # Find last sentence boundary before limit
        cut = -1
        for sep in [". ", "! ", "? ", ".\n", "!\n", "?\n"]:
            pos = text.rfind(sep, 0, MAX_CHUNK_CHARS)
            if pos > cut:
                cut = pos + len(sep) - 1

        if cut <= 0:
            cut = text.rfind(" ", 0, MAX_CHUNK_CHARS)
            if cut <= 0:
                cut = MAX_CHUNK_CHARS

        chunks.append(text[:cut + 1].strip())
        text = text[cut + 1:].strip()

    return [c for c in chunks if c]


def _write_wav(path: Path, pcm_data: bytes):
    """Write raw PCM data to a WAV file."""
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_data)
    logger.info(f"[AUDIO] WAV written: {path} ({len(pcm_data) / 1024 / 1024:.1f} MB)")


def _convert_to_mp3(wav_path: Path, mp3_path: Path):
    """Convert WAV to MP3 using ffmpeg."""
    cmd = [
        "ffmpeg", "-y", "-i", str(wav_path),
        "-codec:a", "libmp3lame", "-b:a", "128k",
        str(mp3_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[:500]}")
    logger.info(f"[AUDIO] MP3 written: {mp3_path} ({mp3_path.stat().st_size / 1024 / 1024:.1f} MB)")


def parse_script(script_text: str, speaker_tags: list[str] = None) -> list[tuple[str, str]]:
    """Parse a script with SPEAKER: tags into (speaker, text) turns.

    Args:
        script_text: Raw script text
        speaker_tags: Valid speaker tags (e.g. ["ALEX", "SARAH"]).
                      If None, uses DEFAULT_VOICES keys.
    """
    if speaker_tags is None:
        speaker_tags = list(DEFAULT_VOICES.keys())

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
