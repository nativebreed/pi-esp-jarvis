"""All settings come from environment variables (set them in the .env file)."""
import os
from pathlib import Path


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value if value else default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


# --- Storage -----------------------------------------------------------------
DATA_DIR = Path(_env("DATA_DIR", "/data"))

# --- Network -----------------------------------------------------------------
PORT = int(_env("PORT", "8000"))
# Leave empty to auto-detect from the address the device used to reach us.
PUBLIC_WS_URL = _env("PUBLIC_WS_URL", "")
# Old optional shared secret, handed to anyone who asks the OTA endpoint, so it protects nothing on your
# network. Use DEVICE_KEY (below) instead; AUTH_TOKEN is only checked when DEVICE_KEY isn't set.
AUTH_TOKEN = _env("AUTH_TOKEN", "")

# --- Assistant personality -----------------------------------------------------
ASSISTANT_NAME = _env("ASSISTANT_NAME", "Jarvis")

# --- Local model (Ollama) --------------------------------------------------------
OLLAMA_URL = _env("OLLAMA_URL", "http://ollama:11434").rstrip("/")
LOCAL_MODEL = _env("LOCAL_MODEL", "llama3.2:3b")

# --- Claude (cloud) ----------------------------------------------------------------
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = _env("CLAUDE_MODEL", "claude-sonnet-5-5")
CLAUDE_MAX_TOKENS = int(_env("CLAUDE_MAX_TOKENS", "700"))
# Say a short phrase while waiting for Claude ("" to disable).
CLAUDE_FILLER = _env("CLAUDE_FILLER", "Let me ask Claude.")

# --- Speech ---------------------------------------------------------------------------
WHISPER_MODEL = _env("WHISPER_MODEL", "base.en")  # tiny.en / base.en / small.en
WHISPER_LANGUAGE = _env("WHISPER_LANGUAGE", "en")
PIPER_VOICE = _env("PIPER_VOICE", "en_US-lessac-medium")
SPEECH_SPEED = _env_float("SPEECH_SPEED", 1.0)  # >1 = faster

# --- Turn taking ------------------------------------------------------------------------
SILENCE_MS = int(_env("SILENCE_MS", "800"))          # pause that ends your question
MAX_UTTERANCE_S = _env_float("MAX_UTTERANCE_S", 20)  # hard cap on one question
IDLE_TIMEOUT_S = _env_float("IDLE_TIMEOUT_S", 10)    # hang up after this much silence
VAD_AGGRESSIVENESS = int(_env("VAD_AGGRESSIVENESS", "2"))  # 0-3
HISTORY_TURNS = int(_env("HISTORY_TURNS", "6"))
HISTORY_RESET_S = _env_float("HISTORY_RESET_S", 300)

# --- Music ---------------------------------------------------------------------------
MUSIC_DIR = Path(_env("MUSIC_DIR", "/music"))
DEFAULT_RADIO_GENRE = _env("DEFAULT_RADIO_GENRE", "soul")  # for a plain "play music"
MUSIC_GAIN = _env_float("MUSIC_GAIN", 0.6)                 # music loudness vs. voice
MAX_PLAY_MINUTES = _env_float("MAX_PLAY_MINUTES", 180)

# --- Everyday skills -------------------------------------------------------------------
WEATHER_LOCATION = _env("WEATHER_LOCATION", "")        # e.g. "London, GB"; empty = guess
WEATHER_UNITS = _env("WEATHER_UNITS", "metric").lower()  # metric | uk | imperial
NEWS_FEEDS = _env("NEWS_FEEDS", "https://feeds.bbci.co.uk/news/rss.xml")
CLAUDE_WEB_SEARCH = _env("CLAUDE_WEB_SEARCH", "true").lower() in ("1", "true", "yes", "on")
AUTO_WEB_SEARCH = _env("AUTO_WEB_SEARCH", "true").lower() in ("1", "true", "yes", "on")
BRIEFING_AFTER_ALARM = _env("BRIEFING_AFTER_ALARM", "true").lower() in ("1", "true", "yes", "on")
PI_HELPER_URL = _env("PI_HELPER_URL", "http://host.docker.internal:8766").rstrip("/")
PI_HELPER_TOKEN = _env("PI_HELPER_TOKEN", "")

# --- Camera (optional USB webcam; see docker-compose.camera.yml) ------------------------
CAMERA_ENABLED = _env("CAMERA_ENABLED", "true").lower() in ("1", "true", "yes", "on")
CAMERA_DEVICE = _env("CAMERA_DEVICE_IN_CONTAINER", "/dev/video0")
CAMERA_RESOLUTION = _env("CAMERA_RESOLUTION", "960x720")   # "" = camera default
CAMERA_WARMUP_S = _env_float("CAMERA_WARMUP_S", 1.5)      # let the exposure settle
CAMERA_PAN_INVERT = _env("CAMERA_PAN_INVERT", "false").lower() in ("1", "true", "yes", "on")

# --- Voice notes ---------------------------------------------------------------------------
NOTES_DIR = Path(_env("NOTES_DIR", str(DATA_DIR / "voice-notes")))  # docker-compose maps ./voice-notes here
NOTES_CLAUDE = _env("NOTES_CLAUDE", "false").lower() in ("1", "true", "yes", "on")  # Claude tidies + titles
NOTES_SILENCE_STOP_S = _env_float("NOTES_SILENCE_STOP_S", 120)  # stop recording after this much quiet
NOTES_MAX_MINUTES = _env_float("NOTES_MAX_MINUTES", 30)         # longest single note
NOTES_IDLE_CLOSE_S = _env_float("NOTES_IDLE_CLOSE_S", 600)      # close the recorder when left alone

# --- Private library (answered by the local model only, never sent to Claude) ----------------
LIBRARY_ENABLED = _env("LIBRARY_ENABLED", "true").lower() in ("1", "true", "yes", "on")
LIBRARY_DIR = Path(_env("LIBRARY_DIR", str(DATA_DIR / "library-files")))   # docker-compose maps ./library here
LIBRARY_EMBED_MODEL = _env("LIBRARY_EMBED_MODEL", "nomic-embed-text")      # finds passages (Ollama, on the Pi)
LIBRARY_VISION_MODEL = _env("LIBRARY_VISION_MODEL", "moondream")           # describes pictures; "off" = don't
if LIBRARY_VISION_MODEL.lower() in ("off", "none", "false", "no"):
    LIBRARY_VISION_MODEL = ""
LIBRARY_MIN_SCORE = _env_float("LIBRARY_MIN_SCORE", 0.60)        # how close a passage must be to answer by itself
LIBRARY_ASKED_SCORE = _env_float("LIBRARY_ASKED_SCORE", 0.40)    # ... when you said "check my files"
LIBRARY_CLAUDE_SCORE = _env_float("LIBRARY_CLAUDE_SCORE", 0.68)  # "use Claude" questions this close stay on the Pi
LIBRARY_SCAN_S = _env_float("LIBRARY_SCAN_S", 60)                # look for new or changed files this often
LIBRARY_MAX_MB = _env_float("LIBRARY_MAX_MB", 50)                # skip files bigger than this
LIBRARY_OCR_PAGES = int(_env("LIBRARY_OCR_PAGES", "20"))         # pages of a scanned PDF to read
LIBRARY_VOICE_NOTES = _env("LIBRARY_VOICE_NOTES", "true").lower() in ("1", "true", "yes", "on")
# true = memories and voice-note transcripts never go to Claude either (the library never does, whatever this says)
KEEP_PRIVATE_LOCAL = _env("KEEP_PRIVATE_LOCAL", "true").lower() in ("1", "true", "yes", "on")
# Optional password for the web pages (lists, notes, library, Wi-Fi). User name: jarvis
WEB_PASSWORD = _env("WEB_PASSWORD", "")
# Other names you open the web pages by (comma-separated; ".example.com" allows a whole domain).
# Local names (raspi5.local, raspi5) and IP addresses always work.
ALLOWED_HOSTS = [h.strip().lower() for h in _env("ALLOWED_HOSTS", "").split(",") if h.strip()]
# A secret built into the board's firmware by flash-esp32.sh (which also writes it here). When set, only a
# board flashed with it can connect, wake on alarms or send gestures. Empty = the older, open behaviour.
DEVICE_KEY = _env("DEVICE_KEY", "")

LOG_LEVEL = _env("LOG_LEVEL", "INFO")
