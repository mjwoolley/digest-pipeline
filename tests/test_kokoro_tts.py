"""Tests for digest_pipeline.kokoro_tts — float-to-PCM conversion."""
import pytest

np = pytest.importorskip("numpy")

from digest_pipeline.kokoro_tts import _float_to_pcm16


def test_float_to_pcm16_silence():
    audio = np.zeros(100, dtype=np.float32)
    pcm = _float_to_pcm16(audio)
    assert len(pcm) == 200  # 100 samples * 2 bytes each
    assert pcm == b"\x00" * 200


def test_float_to_pcm16_max():
    audio = np.array([1.0], dtype=np.float32)
    pcm = _float_to_pcm16(audio)
    value = int.from_bytes(pcm, byteorder="little", signed=True)
    assert value == 32767


def test_float_to_pcm16_min():
    audio = np.array([-1.0], dtype=np.float32)
    pcm = _float_to_pcm16(audio)
    value = int.from_bytes(pcm, byteorder="little", signed=True)
    assert value == -32767


def test_float_to_pcm16_clipping():
    audio = np.array([2.0, -2.0], dtype=np.float32)
    pcm = _float_to_pcm16(audio)
    values = np.frombuffer(pcm, dtype=np.int16)
    assert values[0] == 32767
    assert values[1] == -32767


def test_float_to_pcm16_round_trip_shape():
    audio = np.random.uniform(-1.0, 1.0, 1000).astype(np.float32)
    pcm = _float_to_pcm16(audio)
    assert len(pcm) == 2000
    decoded = np.frombuffer(pcm, dtype=np.int16)
    assert decoded.shape == (1000,)
