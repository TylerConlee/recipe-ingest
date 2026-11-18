"""Speech-to-text. Claude has no audio input, so this is the one non-Claude model in the pipeline.

``local``  — faster-whisper on CPU inside the container (no per-call cost, timestamps, VAD to skip music)
``openai`` — OpenAI whisper-1 API (fallback; needs OPENAI_API_KEY)
``none``   — skip; extraction relies on caption, comments and frames
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, List, Optional

from app.constants import MAX_AUDIO_BYTES

if TYPE_CHECKING:
    from app.settings import Settings


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Transcript:
    segments: List[Segment]
    language: str = ""

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments if s.text.strip())

    def timestamped(self) -> str:
        return "\n".join(f"[{s.start:6.1f}s] {s.text.strip()}" for s in self.segments if s.text.strip())


EMPTY = Transcript(segments=[])

_model_lock = threading.Lock()
_model_cache: dict = {}

# Biases decoding toward kitchen vocabulary (units, fractions) that generic ASR mangles.
INITIAL_PROMPT = (
    "A cooking video recipe. Tablespoon, teaspoon, cup, grams, ounces, 1/2, 1/4, "
    "gochujang, mirin, parmesan, sauté, julienne."
)


def _load_local_model(settings: "Settings", factory: Optional[Callable[..., Any]] = None):
    key = (settings.whisper_model, settings.whisper_threads)
    with _model_lock:
        if key not in _model_cache:
            if factory is None:  # pragma: no cover - real import exercised only in the container
                from faster_whisper import WhisperModel as factory
            _model_cache[key] = factory(
                settings.whisper_model,
                device="cpu",
                compute_type="int8",
                cpu_threads=settings.whisper_threads,
                download_root=settings.whisper_model_dir,
            )
        return _model_cache[key]


def load_wav_16k_mono(path: str):
    """Decode our own 16 kHz mono PCM WAV without PyAV (faster-whisper's PyAV path breaks on new av releases)."""
    import wave

    import numpy as np

    with wave.open(path, "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != 16000:
            raise ValueError("expected 16 kHz mono 16-bit WAV")
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe_local(audio_path: str, settings: "Settings", factory: Optional[Callable[..., Any]] = None) -> Transcript:
    model = _load_local_model(settings, factory)
    audio: Any = load_wav_16k_mono(audio_path) if audio_path.endswith(".wav") else audio_path
    segments, info = model.transcribe(
        audio,
        beam_size=5,
        vad_filter=True,
        initial_prompt=INITIAL_PROMPT,
        condition_on_previous_text=False,
    )
    segs = [Segment(float(s.start), float(s.end), s.text) for s in segments]
    return Transcript(segments=segs, language=getattr(info, "language", "") or "")


def transcribe_openai(audio_path: str, settings: "Settings", client: Any = None) -> Transcript:
    if os.path.getsize(audio_path) > MAX_AUDIO_BYTES:
        print("Audio too large for OpenAI transcription; skipping.")
        return EMPTY
    if client is None:  # pragma: no cover
        from openai import OpenAI

        client = OpenAI(api_key=settings.openai_api_key)
    with open(audio_path, "rb") as f:
        res = client.audio.transcriptions.create(
            model="whisper-1", file=f, response_format="verbose_json", prompt=INITIAL_PROMPT
        )
    raw_segments = getattr(res, "segments", None) or []
    segs = []
    for s in raw_segments:
        get = s.get if isinstance(s, dict) else lambda k, _s=s: getattr(_s, k, None)
        segs.append(Segment(float(get("start") or 0), float(get("end") or 0), get("text") or ""))
    if not segs and getattr(res, "text", ""):
        segs = [Segment(0.0, 0.0, res.text)]
    return Transcript(segments=segs, language=getattr(res, "language", "") or "")


def transcribe(audio_path: Optional[str], settings: "Settings", **kw) -> Transcript:
    if not audio_path or settings.transcriber == "none":
        return EMPTY
    if settings.transcriber == "openai":
        return transcribe_openai(audio_path, settings, client=kw.get("client"))
    return transcribe_local(audio_path, settings, factory=kw.get("factory"))
