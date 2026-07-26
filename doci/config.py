"""設定とパス。.env を軽量パースして os.environ にマージ（実環境変数を優先）。"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "doci"
PROMPTS = PKG / "prompts"
ASSETS = ROOT / "assets"
BGM_DIR = ROOT / "channels" / "ideology" / "bgm"  # 後方互換の既定BGM
CONFIG_DIR = ROOT / "config"
OUTPUT = ROOT / "output"
# codex CLI 用の隔離ホーム。ユーザーの ~/.codex には一切触れない(ChatGPTログイン破壊事故を避けるため)。
CODEX_HOME = ROOT / ".codex-doci"


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


def get_bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def legacy_claude_model(model: str) -> str:
    """明示されたClaude旧経路にだけClaude互換モデル名を渡す。"""
    if model.startswith(("claude-", "anthropic/")):
        return model
    return LEGACY_CLAUDE_MODEL


def legacy_claude_research_model(model: str) -> str:
    """明示されたClaudeリサーチ段に、従来の互換モデル名を渡す。"""
    if model.startswith(("claude-", "anthropic/")):
        return model
    return LEGACY_CLAUDE_RESEARCH_MODEL


def legacy_claude_factcheck_model(model: str) -> str:
    """明示されたClaudeファクトチェック段に、従来の互換モデル名を渡す。"""
    if model.startswith(("claude-", "anthropic/")):
        return model
    return LEGACY_CLAUDE_FACTCHECK_MODEL


# --- text ---
OPENCODE_GO_DEFAULT_MODEL = "opencode-go/qwen3.7-plus"
# Claudeは明示された旧経路だけで使う。TEXT_MODEL等の既定値がOpenCode Goモデルでも、
# 旧経路を明示した利用者がモデル名を設定し忘れた場合に不正なモデル名を渡さない。
LEGACY_CLAUDE_MODEL = get("CLAUDE_MODEL", "claude-opus-4-8")
# 旧Claudeの補助段は段ごとの従来既定を維持し、既定OpenCode Goモデルを
# 本文用モデルへ暗黙に丸めて品質やコストを変えない。
LEGACY_CLAUDE_RESEARCH_MODEL = get("CLAUDE_RESEARCH_MODEL", "claude-sonnet-4-6")
LEGACY_CLAUDE_FACTCHECK_MODEL = get(
    "CLAUDE_FACTCHECK_MODEL", LEGACY_CLAUDE_MODEL
)
# 運用の既定経路はOpenCode Go直API。Claudeは明示的に選んだ旧経路以外では呼ばない。
TEXT_BACKEND = get("TEXT_BACKEND", "opencode_go")
TEXT_MODEL = get("TEXT_MODEL", OPENCODE_GO_DEFAULT_MODEL)
# 旧設定との互換性のため値は残すが、本文生成の自動フォールバックには使わない。
FALLBACK_TEXT_MODEL = get("FALLBACK_TEXT_MODEL", "")
OPENCODE_AGENT = get("OPENCODE_AGENT", "")
# provider/model 形式（例: opencode-go/minimax-m3）。指定時は --agent より優先。
OPENCODE_MODEL = get("OPENCODE_MODEL", "")
# 本文生成だけOpenCode CLIを外し、OpenCode GoのAnthropic互換APIへ直接接続する設定。
# APIキーは環境変数を優先し、未指定なら既存のOpenCode認証ストアから読む。
OPENCODE_GO_API_KEY = get("OPENCODE_GO_API_KEY", "")
OPENCODE_AUTH_FILE = get(
    "OPENCODE_AUTH_FILE", str(Path.home() / ".local" / "share" / "opencode" / "auth.json")
)
OPENCODE_GO_BASE_URL = get("OPENCODE_GO_BASE_URL", "https://opencode.ai/zen/go/v1").rstrip("/")
OPENCODE_GO_MAX_TOKENS = get_int("OPENCODE_GO_MAX_TOKENS", 65536)

# --- 台本品質: 前段リサーチ＋後段ファクトチェック (issue #6) ---
# SCRIPT_RESEARCH: 下書き前に OpenCode Goで題材を選び参考資料を整理した
#   「参考事実」を作り、下書きに具体を織り込ませる。SCRIPT_FACTCHECK: 下書き後に
#   同じ経路で事実主張を検証し narration を自動修正する。いずれも既定OFF。
SCRIPT_RESEARCH = get_bool("SCRIPT_RESEARCH", False)
SCRIPT_FACTCHECK = get_bool("SCRIPT_FACTCHECK", False)
# FACTCHECK単独でも従来どおり検証資料を取得するが、明示的に無効化できる。
SCRIPT_FACTCHECK_RESEARCH = get_bool("SCRIPT_FACTCHECK_RESEARCH", True)
RESEARCH_MODEL = get("RESEARCH_MODEL", OPENCODE_GO_DEFAULT_MODEL)
FACTCHECK_MODEL = get("FACTCHECK_MODEL", OPENCODE_GO_DEFAULT_MODEL)
_RESEARCH_MODEL_EXPLICIT = bool(get("RESEARCH_MODEL"))
_FACTCHECK_MODEL_EXPLICIT = bool(get("FACTCHECK_MODEL"))
# リサーチ・検証・図表背景はOpenCode Goを既定にする。codex は明示時の選択肢、
# claude は既存設定を明示した場合だけ使える後方互換経路。補助段の設定を省略した
# 既存ユーザーが TEXT_BACKEND=anthropic/claude_cli を明示している場合だけ、その
# 互換経路へ追随させ、それ以外はClaudeへ暗黙に戻らない。
def _default_aux_backend() -> str:
    if TEXT_BACKEND in {"anthropic", "claude_cli"}:
        return "claude"
    if TEXT_BACKEND == "codex":
        return "codex"
    if TEXT_BACKEND == "opencode":
        return "opencode"
    return "opencode_go"


_AUX_BACKEND_DEFAULT = _default_aux_backend()
RESEARCH_BACKEND = get("RESEARCH_BACKEND", _AUX_BACKEND_DEFAULT)
FACTCHECK_BACKEND = get("FACTCHECK_BACKEND", _AUX_BACKEND_DEFAULT)
CHART_BG_BACKEND = get("CHART_BG_BACKEND", _AUX_BACKEND_DEFAULT)
_RESEARCH_BACKEND_EXPLICIT = bool(get("RESEARCH_BACKEND"))
_FACTCHECK_BACKEND_EXPLICIT = bool(get("FACTCHECK_BACKEND"))


def _migrate_implicit_opencode_model(
    model: str, backend: str, backend_explicit: bool
) -> str:
    """旧Claudeモデルを、暗黙のOpenCode補助経路だけ安全に移行する。

    明示的に選ばれたバックエンドの不整合は ``ai_text`` 側で fail-closed にする。
    一方、旧 .env に残った Claude 補助モデルを、バックエンド未指定の新既定値で
    恒久的に無効化しないため、暗黙の補助段だけQwen既定へ移行する。
    """
    if (
        backend in {"opencode_go", "opencode"}
        and not backend_explicit
        and model.startswith(("claude-", "anthropic/"))
    ):
        return OPENCODE_GO_DEFAULT_MODEL
    return model


# 既存 .env に残る Claude の本文モデルは、OPENCODE_MODEL が空なら新しい
# 既定経路へ移行する。provider-qualified な別モデルなど明示的な不整合は
# _opencode_go_model() で停止し、意図しないモデル実行を防ぐ。
if TEXT_BACKEND == "opencode_go" and not OPENCODE_MODEL:
    TEXT_MODEL = _migrate_implicit_opencode_go_model(
        TEXT_MODEL, TEXT_BACKEND, False
    )
if RESEARCH_BACKEND in {"opencode_go", "opencode"}:
    RESEARCH_MODEL = _migrate_implicit_opencode_model(
        RESEARCH_MODEL, RESEARCH_BACKEND, _RESEARCH_BACKEND_EXPLICIT
    )
if FACTCHECK_BACKEND in {"opencode_go", "opencode"}:
    FACTCHECK_MODEL = _migrate_implicit_opencode_model(
        FACTCHECK_MODEL, FACTCHECK_BACKEND, _FACTCHECK_BACKEND_EXPLICIT
    )
# 構成プラン(plan.make_plan)のバックエンド。値は opencode | codex。直契約MiniMaxを
# opencode-goゲートウェイ経由でなく codex exec 経由で使えるようにする。
PLAN_BACKEND = get("PLAN_BACKEND", "opencode")
CODEX_BIN = get("CODEX_BIN", "codex")
CODEX_MODEL = get("CODEX_MODEL", "MiniMax-M3")
CODEX_MINIMAX_BASE_URL = get("CODEX_MINIMAX_BASE_URL", "https://api.minimax.io/v1")
# リサーチ/チェックは Web検索＋長尺narrationで時間がかかる。0以下は全バックエンド共通で無制限。
SCRIPT_LLM_TIMEOUT = get_int("SCRIPT_LLM_TIMEOUT", 600)
# 執筆(opencode/qwen 等)各試行の上限。0は各試行を明示的に無制限にするモード
# （OpenCode Goの無音は下記idle上限で切る）。下書き全体の上限は別設定で持つ。
WRITE_LLM_TIMEOUT = get_int("WRITE_LLM_TIMEOUT", 900)
# 全体上限を無効にしても、無音の接続を無限に保持しないためのソケット待機上限。
WRITE_LLM_IDLE_TIMEOUT = get_int("WRITE_LLM_IDLE_TIMEOUT", 300)


def script_llm_timeout() -> int | None:
    """リサーチ/ファクトチェック用の subprocess/API 待機上限を返す。"""
    return SCRIPT_LLM_TIMEOUT if SCRIPT_LLM_TIMEOUT > 0 else None


# 下書きの再生成回数。minimax 等は稀に不完全JSONを返すため複数回試す。
SCRIPT_DRAFT_RETRIES = get_int("SCRIPT_DRAFT_RETRIES", 3)
# 下書き再試行を含む執筆段全体の予算。個別試行の残り時間をこの上限で絞り、
# Claudeフォールバックなしでも複数コーナーを長時間占有し続けないようにする。
SCRIPT_DRAFT_TOTAL_TIMEOUT = get_int(
    "SCRIPT_DRAFT_TOTAL_TIMEOUT", 2700
)
# リサーチの再試行回数。外部Web取得が稀に不正JSONを返すため。高価なので控えめ。
SCRIPT_RESEARCH_RETRIES = get_int("SCRIPT_RESEARCH_RETRIES", 2)
# 公開済み/キュー済み題材の再利用を避ける既定期間。channel.toml の
# pipeline.topic_cooldown_days でチャンネル単位に上書きでき、0で無効化する。
TOPIC_COOLDOWN_DAYS = get_int("TOPIC_COOLDOWN_DAYS", 30)
# ファクトチェックの再試行回数。MiniMax等が長い日本語JSONのエスケープを崩すことがあるため再試行する。
SCRIPT_FACTCHECK_RETRIES = get_int("SCRIPT_FACTCHECK_RETRIES", 2)
# --- 構成プラン: 起承転結＋図表策定（issue #2）。minimax が設計し qwen が執筆 ---
SCRIPT_PLAN = get_bool("SCRIPT_PLAN", True)
PLAN_MODEL = get("PLAN_MODEL", "opencode-go/minimax-m3")

# --- 画像/動画バックエンド選択 ---
# IMAGE_BACKEND: gemini (既定) | openrouter | minimax  ← 素材が無い時のAI生成フォールバック
IMAGE_BACKEND = get("IMAGE_BACKEND", "gemini")
# VIDEO_BACKEND: none (既定/v1は動画なし) | minimax
VIDEO_BACKEND = get("VIDEO_BACKEND", "none")

# --- 素材調達バックエンド (issue #9): AI生成の前段で実フリー素材を取得 ---
# ASSET_BACKEND: pexels (既定/縦・大量・関連度良/商用OK・帰属不要) | none(常にAI生成)
# 取得できなければ IMAGE_BACKEND のAI生成へフォールバックする二段構え。
ASSET_BACKEND = get("ASSET_BACKEND", "pexels")
PEXELS_API_KEY = get("PEXELS_API_KEY", "")
PEXELS_ORIENTATION = get("PEXELS_ORIENTATION", "portrait")
# 素材の種別: photo(既定) | video(全シーン動画優先) | mix(シーン主画は動画・使い回しは写真)。
# 動画が無ければ写真へ、写真も無ければAI生成へフォールバック。動画は Pexsels Videos。
ASSET_MEDIA = get("ASSET_MEDIA", "photo")
# 検索1回で取る候補数。同一シーンのバリエーション(使い回し回避)はこの中から別候補を選ぶ。
ASSET_PER_PAGE = get_int("ASSET_PER_PAGE", 30)

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
# 文・句ごとに合成して連結するため継ぎ目の無音を詰める（既定 pre=0.0/post=0.1。
# VOICEVOX既定は各0.1）。詰めすぎると窮屈になるので post で最小限のポーズを残す。
VOICE_PRE_PHONEME = get_float("VOICE_PRE_PHONEME", 0.0)
VOICE_POST_PHONEME = get_float("VOICE_POST_PHONEME", 0.1)
# ナレーション全体の末尾に足す無音（秒）。最終文の post=0.1 だけでは語尾直後に音声と
# BGM が同時にブツッと止まり「末尾が途切れた」と聞こえるため、最後だけ余韻を確保する。
VOICE_TAIL_SILENCE = get_float("VOICE_TAIL_SILENCE", 0.6)

# --- 配信投稿 (issue #3): route.platforms と各 PUBLISH_* で出し分け ---
# do_upload(--no-upload で無効) が大元のスイッチ。その上で各プラットフォームを個別に有効化。
PUBLISH_DRY_RUN = get_bool("PUBLISH_DRY_RUN", False)   # 実投稿せずログのみ（安全確認用）
PUBLISH_YOUTUBE = get_bool("PUBLISH_YOUTUBE", True)    # 既存。既定ON
PUBLISH_TIKTOK = get_bool("PUBLISH_TIKTOK", False)     # 新規。資格情報を入れてON
PUBLISH_INSTAGRAM = get_bool("PUBLISH_INSTAGRAM", False)  # 後回し（公開ホスト方針未定）

# --- youtube ---
YOUTUBE_CLIENT_SECRET_FILE = get("YOUTUBE_CLIENT_SECRET_FILE", "client_secret.json")
YOUTUBE_TOKEN_FILE = get("YOUTUBE_TOKEN_FILE", "youtube_token.json")
YOUTUBE_PRIVACY = get("YOUTUBE_PRIVACY", "unlisted")

# --- tiktok (Content Posting API) ---
# 開発者アプリ: https://developers.tiktok.com/ 。scope=video.publish。
# 審査前アプリは privacy=SELF_ONLY(非公開)のみ可。審査後に PUBLIC_TO_EVERYONE。
TIKTOK_CLIENT_KEY = get("TIKTOK_CLIENT_KEY", "")
TIKTOK_CLIENT_SECRET = get("TIKTOK_CLIENT_SECRET", "")
TIKTOK_TOKEN_FILE = get("TIKTOK_TOKEN_FILE", "tiktok_token.json")
TIKTOK_PRIVACY = get("TIKTOK_PRIVACY", "SELF_ONLY")
TIKTOK_REDIRECT_URI = get("TIKTOK_REDIRECT_URI", "http://127.0.0.1:8723/callback")

# --- instagram (Graph API・後回し) ---
# 動画は公開URLから取得される仕様。INSTAGRAM_HOST_BASE は公開ホストの基底URL（未定）。
INSTAGRAM_USER_ID = get("INSTAGRAM_USER_ID", "")
INSTAGRAM_ACCESS_TOKEN = get("INSTAGRAM_ACCESS_TOKEN", "")
INSTAGRAM_HOST_BASE = get("INSTAGRAM_HOST_BASE", "")

# --- visual density (issue #4): 画像枚数を尺連動で増やす ---
# 画像1枚あたりの目安表示秒。これが密度の実ノブ（小さいほど画が増え間延びしにくい）。
# 実枚数は ceil(尺/SECONDS_PER_IMAGE) で尺に連動する。
SECONDS_PER_IMAGE = get_float("SECONDS_PER_IMAGE", 11.0)
# 1本あたりの画像枚数の絶対上限（暴走防止のバックストップ）。素材はPexsels(無料)なので
# 旧16から引上げ、長尺(最大~600秒)でも尺連動の枚数が活きるようにする。600/11≒55 を覆う。
MAX_IMAGES = get_int("MAX_IMAGES", 60)

# --- video ---
VIDEO_WIDTH = get_int("VIDEO_WIDTH", 1080)
VIDEO_HEIGHT = get_int("VIDEO_HEIGHT", 1920)
VIDEO_FPS = get_int("VIDEO_FPS", 30)
BGM_VOLUME = get_float("BGM_VOLUME", 0.18)
# BGMダッキング(サイドチェイン)。文間ポーズで BGM(ピアノ)が膨らみ、次フレーズの語頭
# （例「1900年」の"せん"）をスペクトル的にマスクして「百年」に聞こえる事象への対策
# （E=BGM込み実音声で再現・耳で確認済）:
# (1) 音量を声優先に下げる、(2) 先読み=BGM を LOOKAHEAD_MS 遅延させ、サイドチェインの
#     検知（遅延しないナレ）が BGM の少し先を見る形にして語頭の手前で先に絞る。
# (3) release を長めに取り、フレーズ間ギャップで BGM がピークまで戻りにくくする。
# 末尾の絞りは compose の afade が別途担当。
BGM_DUCK_THRESHOLD = get_float("BGM_DUCK_THRESHOLD", 0.03)
# ratio=15: 声がある間 BGM を ~24dB 絞る（敷物として残しつつ語頭マスクは十分抑える）。
# ratio=20 は深すぎて BGM がほぼ無音化、ratio=8 は浅すぎて「せん」頭が食われる（実走A/Bで確定）。
BGM_DUCK_RATIO = get_int("BGM_DUCK_RATIO", 15)
# release=1500ms: 文末の post-phoneme(100ms)＋短ポーズ(~300ms)の合計400ms中では
# BGM がほぼ復帰せず、復帰しきるのは1s以上の長いポーズ(段落間)に限られる。
# → 短い無音中にBGMが戻って次文頭と重なり「次が被って聞こえる」事象への対策
# （release=800ms では post-phoneme 100ms 中に BGM が半分近くまで復帰していた実測あり）。
BGM_DUCK_RELEASE = get_int("BGM_DUCK_RELEASE", 1500)
# lookahead=280ms: サイドチェイン検知(遅延しないナレ)が BGM の少し先を見る＝語頭の手前で
# 先に絞る。150ms だと長めのポーズで BGM がピークに戻り切るため不十分だった。
BGM_DUCK_LOOKAHEAD_MS = get_int("BGM_DUCK_LOOKAHEAD_MS", 280)


def bgm_path() -> Path | None:
    """旧単一チャンネルAPI用。新経路は channel.bgm_path() を使う。"""
    if not BGM_DIR.exists():
        return None
    for ext in ("*.mp3", "*.ogg", "*.wav", "*.m4a", "*.flac"):
        files = sorted(BGM_DIR.glob(ext))
        if files:
            return files[0]
    return None
