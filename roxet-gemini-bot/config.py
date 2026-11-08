"""Configuration loaded from .env / environment variables."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_env_file(path: Path) -> None:
    """Tiny .env loader (used when python-dotenv is not installed)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:  # pragma: no cover
    _load_env_file(BASE_DIR / ".env")


def _parse_ids(value: str) -> set:
    ids = set()
    for part in value.replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = _parse_ids(os.getenv("ADMIN_IDS", "") or os.getenv("ADMIN_ID", ""))

# Optional: the Gemini key can (and normally should) be set from the admin
# panel inside the bot. If you put one here it is used as a fallback.
ENV_GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
DEFAULT_TEXT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
DEFAULT_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image").strip()
DEFAULT_TTS_MODEL = os.getenv("GEMINI_TTS_MODEL", "gemini-3.8-flash-tts").strip()
DEFAULT_VIDEO_MODEL = os.getenv("GEMINI_VIDEO_MODEL", "gemini-omni-1.1-flash").strip()

DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "bot.db"

BOT_NAME = os.getenv("BOT_NAME", "RoXeT AI").strip()

# Telegram bots can download files up to 20 MB; Gemini inline request limit is
# also about 20 MB, so stay a bit below it.
MAX_MEDIA_BYTES = 15 * 1024 * 1024
MAX_TEXT_FILE_CHARS = 120_000
HISTORY_TURNS = 24  # messages kept as context per chat


def validate() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set (see .env)")
    if not ADMIN_IDS:
        raise RuntimeError("ADMIN_IDS is not set (see .env)")
