# Shared constants (no environment reads).

# Whisper hard limit is ~25 MB; stay below it.
MAX_AUDIO_BYTES = 24 * 1024 * 1024  # 24 MB

# Bitrate tiers for re-encoding (kbps). Start decent, then drop until < MAX_AUDIO_BYTES.
AUDIO_BITRATE_TIERS_KBPS = [48, 32, 24, 16, 12, 8]

RECIPE_DESC_MAX = 512
RECIPE_NAME_MAX = 128
STEP_NAME_MAX = 128
ING_NOTE_MAX = 256

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) "
    "Gecko/20100101 Firefox/115.0"
)
