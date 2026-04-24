from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.constants import MAX_AUDIO_BYTES
from app.recipe_ai import extract_recipe_with_gpt, transcribe_audio


def test_transcribe_skips_when_file_too_large(tmp_path):
    path = tmp_path / "big.mp3"
    path.write_bytes(b"x" * (MAX_AUDIO_BYTES + 1))
    client = MagicMock()
    assert transcribe_audio(client, str(path), MAX_AUDIO_BYTES) == ""
    client.audio.transcriptions.create.assert_not_called()


def test_transcribe_calls_whisper(tmp_path):
    path = tmp_path / "small.mp3"
    path.write_bytes(b"tiny")
    client = MagicMock()
    client.audio.transcriptions.create.return_value = SimpleNamespace(text="hello world", output_text=None)
    out = transcribe_audio(client, str(path), MAX_AUDIO_BYTES)
    assert out == "hello world"
    client.audio.transcriptions.create.assert_called_once()


def test_transcribe_prefers_output_text(tmp_path):
    path = tmp_path / "small.mp3"
    path.write_bytes(b"t")
    client = MagicMock()
    client.audio.transcriptions.create.return_value = SimpleNamespace(text=None, output_text="alt")
    assert transcribe_audio(client, str(path), MAX_AUDIO_BYTES) == "alt"


def test_extract_recipe_with_gpt_parses_json():
    client = MagicMock()
    msg = MagicMock()
    msg.content = '{"title": "Soup", "steps": []}'
    choice = MagicMock()
    choice.message = msg
    client.chat.completions.create.return_value = MagicMock(choices=[choice])

    data = extract_recipe_with_gpt(client, "cap", "tr", "https://u", "Manual")
    assert data["title"] == "Soup"
    client.chat.completions.create.assert_called_once()
    call_kw = client.chat.completions.create.call_args.kwargs
    assert call_kw["model"] == "gpt-4o"
    assert call_kw["response_format"] == {"type": "json_object"}


def test_extract_recipe_empty_content():
    client = MagicMock()
    msg = MagicMock()
    msg.content = "   "
    client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=msg)])
    data = extract_recipe_with_gpt(client, "", "", "https://u", None)
    assert data == {}
