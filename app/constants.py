# Shared constants (no environment reads).

# OpenAI Whisper API hard limit is ~25 MB; only relevant when TRANSCRIBER=openai.
MAX_AUDIO_BYTES = 24 * 1024 * 1024  # 24 MB

RECIPE_DESC_MAX = 512
RECIPE_NAME_MAX = 128
STEP_NAME_MAX = 128
ING_NOTE_MAX = 256

# Tandoor's UnitSerializer rejects blank names; use a generic unit for countables / headers.
TANDOOR_DEFAULT_UNIT_NAME = "each"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) "
    "Gecko/20100101 Firefox/115.0"
)

# --- Download -------------------------------------------------------------
# 720p is plenty to read on-screen text overlays; "best" can be 1080p+ and 3-5x the bytes.
YTDLP_FORMAT = "b[height<=720][vcodec!=none]/bv*[height<=720]+ba/b"

# Comments are fetched by a separate, killable yt-dlp process so a viral video with
# 50k comments can't stall the whole import.
COMMENTS_TIMEOUT_S = 25
MAX_TOP_COMMENTS = 15
# yt-dlp's TikTok extractor has no comment support (checked against 2026.10.26), so asking
# for TikTok comments only costs an extra request and always returns nothing.
COMMENTLESS_PLATFORMS = {"tiktok"}

# Transient network failures (DNS blips after a reboot, resets, 5xx/429) get retried.
DOWNLOAD_ATTEMPTS = 3
RETRY_DELAYS_S = (3, 10)

# --- Frames ---------------------------------------------------------------
FRAME_WIDTH = 540  # 540x960 for vertical video, ~700 image tokens per frame
MAX_SAMPLED_FRAMES = 60  # frames read by the cheap OCR model
FRAME_DEDUPE_HAMMING = 4  # dHash distance under which consecutive frames are "the same shot"
FRAME_BATCH_SIZE = 8  # frames per Haiku OCR request
MAX_EXTRACTION_FRAMES = 24  # frames the extraction model sees directly

# --- Models ---------------------------------------------------------------
DEFAULT_EXTRACT_MODEL = "claude-opus-5-5"
DEFAULT_VISION_MODEL = "claude-haiku-5-5"
DEFAULT_EXTRACT_EFFORT = "high"
EXTRACT_MAX_TOKENS = 16000
VISION_MAX_TOKENS = 4000

DEFAULT_WHISPER_MODEL = "large-v3-turbo"

# Values that must never be accepted as a real INGEST_API_KEY.
PLACEHOLDER_SECRETS = {
    "some-long-random-string",
    "changeme",
    "change-me",
    "replace_me",
    "replace-me",
    "secret",
}
