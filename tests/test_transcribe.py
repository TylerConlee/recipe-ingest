from __future__ import annotations

from types import SimpleNamespace

from app import transcribe as stt
from app.constants import MAX_AUDIO_BYTES
from app.settings import Settings


def _s(**kw):
    return Settings(anthropic_api_key="k", tandoor_url="http://t", tandoor_api_token="t", ingest_api_key="i", **kw)


def test_transcript_formatting():
    t = stt.Transcript([stt.Segment(0.0, 1.0, " Add salt "), stt.Segment(12.3, 14, "  "), stt.Segment(15, 16, "stir")])
    assert t.text == "Add salt stir"
    assert t.timestamped() == "[   0.0s] Add salt\n[  15.0s] stir"


def test_transcribe_none_and_missing_audio():
    assert stt.transcribe(None, _s()) is stt.EMPTY
    assert stt.transcribe("a.wav", _s(transcriber="none")) is stt.EMPTY


def test_transcribe_local_caches_model(tmp_path):
    stt._model_cache.clear()
    created = []

    class FakeModel:
        def __init__(self, name, **kw):
            created.append((name, kw))

        def transcribe(self, path, **kw):
            assert kw["vad_filter"] is True
            segs = iter([SimpleNamespace(start=0.5, end=2.0, text="two cups flour")])
            return segs, SimpleNamespace(language="en")

    s = _s(whisper_model="tiny", whisper_threads=2)
    t1 = stt.transcribe("a.m4a", s, factory=FakeModel)
    stt.transcribe("a.m4a", s, factory=FakeModel)
    assert len(created) == 1
    assert created[0][1]["compute_type"] == "int8" and created[0][1]["cpu_threads"] == 2
    assert t1.language == "en" and t1.segments[0].text == "two cups flour"
    stt._model_cache.clear()


def test_transcribe_openai_segments(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    calls = {}

    class Client:
        class audio:
            class transcriptions:
                @staticmethod
                def create(**kw):
                    calls.update(kw)
                    return SimpleNamespace(segments=[{"start": 1, "end": 2, "text": "hi"},
                                                     SimpleNamespace(start=3, end=4, text="there")],
                                           text="hi there", language="english")

    t = stt.transcribe(str(audio), _s(transcriber="openai", openai_api_key="o"), client=Client)
    assert [s.text for s in t.segments] == ["hi", "there"]
    assert calls["response_format"] == "verbose_json"


def test_transcribe_openai_text_only_and_too_big(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")

    class Client:
        class audio:
            class transcriptions:
                @staticmethod
                def create(**kw):
                    return SimpleNamespace(segments=None, text="plain", language="")

    s = _s(transcriber="openai", openai_api_key="o")
    assert stt.transcribe_openai(str(audio), s, client=Client).text == "plain"
    big = tmp_path / "big.mp3"
    big.write_bytes(b"x" * (MAX_AUDIO_BYTES + 1))
    assert stt.transcribe_openai(str(big), s, client=Client) is stt.EMPTY


def test_load_wav_16k_mono(tmp_path):
    import wave

    import pytest

    p = tmp_path / "a.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x40" * 160)
    arr = stt.load_wav_16k_mono(str(p))
    assert arr.shape == (160,) and abs(float(arr[0]) - 0.5) < 1e-6
    bad = tmp_path / "b.wav"
    with wave.open(str(bad), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00" * 4)
    with pytest.raises(ValueError):
        stt.load_wav_16k_mono(str(bad))
