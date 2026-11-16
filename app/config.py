import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _path(env: str, default: Path) -> Path:
    value = os.getenv(env)
    if not value:
        return default
    p = Path(value)
    return p if p.is_absolute() else BASE_DIR / p


DATA_DIR = _path("DATA_DIR", BASE_DIR / "data")
VIDEOS_DIR = DATA_DIR / "videos"
DB_URL = f"sqlite:///{DATA_DIR / 'channelbet.db'}"

CLIENT_SECRETS_FILE = _path("GOOGLE_CLIENT_SECRETS", BASE_DIR / "client_secret.json")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
OAUTH_REDIRECT_URI = f"{BASE_URL}/oauth/callback"

MPT_URL = os.getenv("MPT_URL", "http://127.0.0.1:8080").rstrip("/")

WORKER_INTERVAL_SECONDS = int(os.getenv("WORKER_INTERVAL_SECONDS", "30"))
STATS_REFRESH_HOURS = int(os.getenv("STATS_REFRESH_HOURS", "6"))

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

# OAuth via http só é aceitável em localhost.
if BASE_URL.startswith(("http://localhost", "http://127.0.0.1")):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
# O Google pode devolver os escopos em ordem diferente.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

DATA_DIR.mkdir(parents=True, exist_ok=True)
VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
