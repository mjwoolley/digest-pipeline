"""Kokoro-82M local TTS backend for podcast audio generation."""
import logging
import subprocess
import numpy as np
from pathlib import Path

import cartesia  # reuse parse_script

logger = logging.getLogger("digest")

SAMPLE_RATE = 24000

# Default voices — can be overridden via voice_map parameter
DEFAULT_VOICES = {
    "ALEX": "am_michael",
    "SARAH": "af_heart",
}

_pipeline = None


def configure():
    """Initialize KPipeline (no API key needed)."""
    global _pipeline
    from kokoro import KPipeline
    _pipeline = KPipeline(lang_code='a')
    logger.info("[KOKORO] Pipeline initialized")


def parse_script(script_text: str, speaker_tags: list[str] = None) -> list[tuple[str, str]]:
    """Parse script — delegates to cartesia module."""
    return cartesia.parse_script(script_text, speaker_tags=speaker_tags)


def synthesize_script(turns: list[tuple[str, str]], output_path: Path,
                      voice_map: dict = None) -> dict:
    """Synthesize a parsed script into an MP3 file.

    Writes audio incrementally to a WAV file to avoid accumulating
    all samples in memory (important for low-RAM servers).

    Args:
        turns: List of (speaker, text) tuples
        output_path: Path for the output MP3 file
        voice_map: Optional speaker->voice_name mapping (overrides defaults)

    Returns:
        Usage dict with total_chars, total_turns, duration_seconds
    """
    import wave

    voices = voice_map or DEFAULT_VOICES
    total_chars = 0
    total_frames = 0
    pause = np.zeros(int(SAMPLE_RATE * 0.4), dtype=np.float32)  # 400ms silence

    wav_path = output_path.with_suffix(".wav")
    wf = wave.open(str(wav_path), "wb")
    wf.setnchannels(1)
    wf.setsampwidth(2)  # 16-bit
    wf.setframerate(SAMPLE_RATE)

    try:
        for i, (speaker, text) in enumerate(turns):
            voice = voices.get(speaker, list(voices.values())[0])
            logger.info(f"[AUDIO] Turn {i+1}/{len(turns)}: {speaker} ({len(text)} chars)")

            generator = _pipeline(text, voice=voice, speed=1.0)
            for _, _, audio in generator:
                pcm = _float_to_pcm16(audio)
                wf.writeframes(pcm)
                total_frames += len(audio)
                del audio, pcm

            # Add pause between turns (not after last)
            if i < len(turns) - 1:
                wf.writeframes(_float_to_pcm16(pause))
                total_frames += len(pause)

            total_chars += len(text)
    finally:
        wf.close()

    logger.info(f"[AUDIO] WAV written: {wav_path} ({wav_path.stat().st_size / 1024 / 1024:.1f} MB)")

    _convert_to_mp3(wav_path, output_path)
    wav_path.unlink(missing_ok=True)

    duration_seconds = total_frames / SAMPLE_RATE
    logger.info(f"[AUDIO] Complete: {total_chars} chars, {duration_seconds:.0f}s, saved to {output_path}")

    return {
        "total_chars": total_chars,
        "total_turns": len(turns),
        "duration_seconds": duration_seconds,
    }


def _float_to_pcm16(audio) -> bytes:
    """Convert float32 audio [-1, 1] to 16-bit PCM bytes. Accepts tensor or ndarray."""
    if not isinstance(audio, np.ndarray):
        audio = audio.cpu().numpy()
    pcm = np.clip(audio, -1.0, 1.0)
    return (pcm * 32767).astype(np.int16).tobytes()


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
