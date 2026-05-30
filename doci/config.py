"""設定とパス。.env を軽量パースして os.environ にマージ（実環境変数を優先）。"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "doci"
PROMPTS = PKG / "prompts"
ASSETS = ROOT / "assets"
BGM_DIR = ASSETS / "bgm"
CONFIG_DIR = ROOT / "config"
OUTPUT = ROOT / "output"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        # 実環境変数を優先（クラウドの Secrets を上書きしない）
        os.environ.setdefault(key, val)


_load_dotenv(ROOT / ".env")


def get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def get_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def get_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


# --- text ---
TEXT_BACKEND = get("TEXT_BACKEND", "claude_cli")
TEXT_MODEL = get("TEXT_MODEL", "claude-opus-4-8")
OPENCODE_AGENT = get("OPENCODE_AGENT", "")

# --- 画像/動画バックエンド選択 ---
# IMAGE_BACKEND: gemini (既定) | openrouter | minimax
IMAGE_BACKEND = get("IMAGE_BACKEND", "gemini")
# VIDEO_BACKEND: none (既定/v1は動画なし) | minimax
VIDEO_BACKEND = get("VIDEO_BACKEND", "none")

# --- gemini (画像生成: nano banana) ---
GEMINI_API_KEY = get("GEMINI_API_KEY", "")
GEMINI_IMAGE_MODEL = get("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")
GEMINI_API_VERSION = get("GEMINI_API_VERSION", "v1beta")

# --- openrouter (画像生成の代替) ---
OPENROUTER_API_KEY = get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
OPENROUTER_IMAGE_MODEL = get("OPENROUTER_IMAGE_MODEL", "google/gemini-2.5-flash-image")

# --- minimax (画像/動画。メディアトークン枠が要る) ---
MINIMAX_API_KEY = get("MINIMAX_API_KEY", "")
MINIMAX_MEDIA_BASE_URL = get("MINIMAX_MEDIA_BASE_URL", "https://api.minimax.io/v1").rstrip("/")
MINIMAX_IMAGE_MODEL = get("MINIMAX_IMAGE_MODEL", "image-01")
MINIMAX_VIDEO_MODEL = get("MINIMAX_VIDEO_MODEL", "MiniMax-Hailuo-2.3")
MINIMAX_VIDEO_SCENES = get_int("MINIMAX_VIDEO_SCENES", 0)

# --- voicevox ---
VOICEVOX_URL = get("VOICEVOX_URL", "http://192.168.11.13:50021").rstrip("/")
VOICEVOX_URL_FALLBACK = get("VOICEVOX_URL_FALLBACK", "http://127.0.0.1:50021").rstrip("/")
VOICE_CHINESE_AI = get_int("VOICE_CHINESE_AI", 3)
VOICE_AMERICAN_AI = get_int("VOICE_AMERICAN_AI", 14)

# --- youtube ---
YOUTUBE_CLIENT_SECRET_FILE = get("YOUTUBE_CLIENT_SECRET_FILE", "client_secret.json")
YOUTUBE_TOKEN_FILE = get("YOUTUBE_TOKEN_FILE", "youtube_token.json")
YOUTUBE_PRIVACY = get("YOUTUBE_PRIVACY", "unlisted")

# --- video ---
VIDEO_WIDTH = get_int("VIDEO_WIDTH", 1080)
VIDEO_HEIGHT = get_int("VIDEO_HEIGHT", 1920)
VIDEO_FPS = get_int("VIDEO_FPS", 30)
BGM_VOLUME = get_float("BGM_VOLUME", 0.12)


def bgm_path() -> Path | None:
    """assets/bgm 配下の最初の音声ファイルを返す。"""
    if not BGM_DIR.exists():
        return None
    for ext in ("*.mp3", "*.ogg", "*.wav", "*.m4a", "*.flac"):
        files = sorted(BGM_DIR.glob(ext))
        if files:
            return files[0]
    return None
