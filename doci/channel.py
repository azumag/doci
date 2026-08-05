"""チャンネル定義（channels/<id>/channel.toml）のロードと検証。"""
from __future__ import annotations

import hashlib
import os
import re
import tomllib
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config, style_themes, voices


class ChannelConfigError(ValueError):
    """channel.toml の内容が不正なときのエラー。"""


def _repo_path(value: str | Path) -> Path:
    """資格情報パスをリポジトリルート相対で解決する。存在は認証時まで要求しない。"""
    path = Path(value)
    return path.resolve() if path.is_absolute() else (config.ROOT / path).resolve()


@dataclass(frozen=True)
class CornerSpec:
    key: str
    label: str
    persona_path: Path
    corner_path: Path
    voice_key: str


@dataclass(frozen=True)
class SubtitleStyle:
    font: Path | None = None
    fill: str = "#ffffff"
    stroke: str = "#000000"
    box_color: str = "#000000"
    box_alpha: float = 0.45
    position_ratio: float = 0.64
    box_radius: float = 0.35


@dataclass(frozen=True)
class ThumbnailStyle:
    font_family: str = "'Hiragino Mincho ProN','Hiragino Mincho Pro',serif"
    title_color: str = "#f6efe1"
    theme: str = "classic"


@dataclass(frozen=True)
class ChartStyle:
    palette: tuple[str, ...] = ()
    font: Path | None = None
    theme: str = "classic"


@dataclass(frozen=True)
class VideoStyle:
    pad_color: str = "0x0a0a0c"
    filter: str = ""


@dataclass(frozen=True)
class BgmStyle:
    dir: Path = field(default_factory=lambda: config.BGM_DIR)
    volume: float = field(default_factory=lambda: config.BGM_VOLUME)
    rotation: str = "fixed"


@dataclass(frozen=True)
class CreditsStyle:
    template: str = ""


@dataclass(frozen=True)
class StyleSpec:
    """チャンネルの見た目・聞こえ方。全フィールドは現行値が既定。

    `theme`(issue #76)はチャンネル別デザインテーマの選択で、`doci.style_themes`が
    定義するCSS追記とスタイル既定値のセットを指す。`subtitle`/`video`は個別キーの
    まま(既定値のみテーマから注入)、`thumbnail`/`chart`は実際にテーマCSSを適用する
    レンダラへ渡るよう各自`theme`を保持する。
    """

    subtitle: SubtitleStyle = field(default_factory=SubtitleStyle)
    thumbnail: ThumbnailStyle = field(default_factory=ThumbnailStyle)
    chart: ChartStyle = field(default_factory=ChartStyle)
    video: VideoStyle = field(default_factory=VideoStyle)
    bgm: BgmStyle = field(default_factory=BgmStyle)
    credits: CreditsStyle = field(default_factory=CreditsStyle)
    theme: str = "classic"


@dataclass(frozen=True)
class YouTubeReviewSpec:
    """主題適合の自動判定で公開設定を決めるかどうかの運用設定。"""

    enabled: bool = False


@dataclass(frozen=True)
class YouTubePublishSpec:
    privacy: str = field(default_factory=lambda: config.YOUTUBE_PRIVACY)
    client_secret: Path = field(
        default_factory=lambda: _repo_path(config.YOUTUBE_CLIENT_SECRET_FILE)
    )
    token: Path = field(default_factory=lambda: _repo_path(config.YOUTUBE_TOKEN_FILE))
    review: YouTubeReviewSpec = field(default_factory=YouTubeReviewSpec)


@dataclass(frozen=True)
class TikTokPublishSpec:
    token: Path = field(default_factory=lambda: _repo_path(config.TIKTOK_TOKEN_FILE))
    privacy: str = field(default_factory=lambda: config.TIKTOK_PRIVACY)


@dataclass(frozen=True)
class InstagramPublishSpec:
    user_id: str = field(default_factory=lambda: config.INSTAGRAM_USER_ID)
    access_token_env: str = "INSTAGRAM_ACCESS_TOKEN"


@dataclass(frozen=True)
class PublishSpec:
    """チャンネル別の投稿先と資格情報参照。既定値は従来のグローバル設定。"""

    platforms: tuple[str, ...] = ("youtube", "tiktok", "instagram")
    youtube: YouTubePublishSpec = field(default_factory=YouTubePublishSpec)
    tiktok: TikTokPublishSpec = field(default_factory=TikTokPublishSpec)
    instagram: InstagramPublishSpec = field(default_factory=InstagramPublishSpec)


@dataclass(frozen=True)
class ChannelSpec:
    id: str
    name: str
    root: Path
    corners: dict[str, CornerSpec]
    rotation: list[str]
    voices_path: Path
    style: StyleSpec = field(default_factory=StyleSpec)
    publish: PublishSpec = field(default_factory=PublishSpec)
    pipeline: dict[str, Any] = field(default_factory=dict)

    @property
    def output_dir(self) -> Path:
        return config.OUTPUT / self.id

    @property
    def history_file(self) -> Path:
        return self.output_dir / "history.jsonl"

    def pipeline_get(self, key: str, default: Any = None) -> Any:
        return self.pipeline.get(key, default)

    def voice_for(self, corner: CornerSpec | str) -> voices.VoiceCfg:
        """チャンネル固有 voices.json からコーナーの声を解決する。"""
        corner_spec = self.corners[corner] if isinstance(corner, str) else corner
        loaded = voices.load(self.voices_path)
        try:
            return loaded[corner_spec.voice_key]
        except KeyError as exc:  # load() 時の検証後にファイルが変わった場合も明示する
            raise ChannelConfigError(
                f"voice key disappeared from {self.voices_path}: {corner_spec.voice_key}"
            ) from exc


_TOP_LEVEL_KEYS = {"channel", "corners", "voices", "style", "publish", "pipeline"}
_CHANNEL_KEYS = {"id", "name", "rotation"}
_CORNER_KEYS = {"label", "persona", "corner", "voice"}
_PIPELINE_KEYS = {
    "seconds_per_image",
    "max_images",
    "research",
    "factcheck",
    "plan",
    "asset_media",
    "topic_cooldown_days",
    "performance_feedback",
    "research_requires_youtube_case_studies",
    "title_pattern_check",
    "narration_opening_guard",
    "narration_pattern_check",
    "ambiguous_date_title_check",
    "plan_topic_retries",
    "max_uploads_per_day",
    "feedback_repository",
    "youtube_auto_playlist",
    "youtube_auto_engagement_comment",
    "tactic_issues",
}
_STYLE_KEYS = {"theme", "subtitle", "thumbnail", "chart", "video", "bgm", "credits"}
_SUBTITLE_STYLE_KEYS = {
    "font",
    "fill",
    "stroke",
    "box_color",
    "box_alpha",
    "position_ratio",
    "box_radius",
}
_THUMBNAIL_STYLE_KEYS = {"font_family", "title_color"}
_CHART_STYLE_KEYS = {"palette", "font"}
_VIDEO_STYLE_KEYS = {"pad_color", "filter"}
_BGM_STYLE_KEYS = {"dir", "volume", "rotation"}
_CREDITS_STYLE_KEYS = {"template"}
_PUBLISH_KEYS = {"platforms", "youtube", "tiktok", "instagram"}
_YOUTUBE_PUBLISH_KEYS = {"privacy", "client_secret", "token", "review"}
_YOUTUBE_REVIEW_KEYS = {"enabled"}
_TIKTOK_PUBLISH_KEYS = {"token", "privacy"}
_INSTAGRAM_PUBLISH_KEYS = {"user_id", "access_token_env"}
_PUBLISH_PLATFORMS = {"youtube", "tiktok", "instagram"}
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CHANNEL_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_AUDIO_SUFFIXES = {".mp3", ".ogg", ".wav", ".m4a", ".flac"}


def _warn_unknown(data: dict[str, Any], allowed: set[str], location: str) -> None:
    for key in sorted(set(data) - allowed):
        warnings.warn(
            f"unknown channel setting: {location}{key}",
            UserWarning,
            stacklevel=3,
        )


def _required_str(data: dict[str, Any], key: str, location: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ChannelConfigError(f"missing or invalid required key: {location}{key}")
    return value


def _resolve_path(channel_root: Path, value: str, key: str) -> Path:
    """チャンネル相対を優先し、移行期間はリポジトリルート相対も許容。"""
    raw = Path(value)
    if raw.is_absolute():
        candidate = raw
    else:
        candidate = channel_root / raw
        if not candidate.exists():
            candidate = config.ROOT / raw
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise ChannelConfigError(f"referenced file does not exist: {key}={value}")
    return candidate


def _optional_file(channel_root: Path, value: Any, key: str) -> Path | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ChannelConfigError(f"{key} must be a path string")
    return _resolve_path(channel_root, value, key)


def _style_table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ChannelConfigError(f"style.{key} must be a table")
    return value


def _publish_table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ChannelConfigError(f"publish.{key} must be a table")
    return value


def _string(data: dict[str, Any], key: str, default: str, location: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ChannelConfigError(f"{location}{key} must be a string")
    return value


def _number(
    data: dict[str, Any],
    key: str,
    default: float,
    location: str,
    *,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ChannelConfigError(f"{location}{key} must be a number")
    result = float(value)
    if result < minimum or (maximum is not None and result > maximum):
        bounds = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
        raise ChannelConfigError(f"{location}{key} must be {bounds}")
    return result


def _resolve_style_dir(channel_root: Path, value: str) -> Path:
    raw = Path(value)
    if raw.is_absolute():
        return raw.resolve()
    local = channel_root / raw
    if local.exists() or not (config.ROOT / raw).exists():
        return local.resolve()
    return (config.ROOT / raw).resolve()


def _load_style(data: dict[str, Any], channel_root: Path) -> StyleSpec:
    _warn_unknown(data, _STYLE_KEYS, "style.")
    theme_key = _string(data, "theme", "classic", "style.")
    if theme_key not in style_themes.THEMES:
        raise ChannelConfigError(
            f"style.theme must be one of {sorted(style_themes.THEMES)}: {theme_key!r}"
        )
    theme = style_themes.get(theme_key)
    subtitle = _style_table(data, "subtitle")
    thumbnail = _style_table(data, "thumbnail")
    chart = _style_table(data, "chart")
    video = _style_table(data, "video")
    bgm = _style_table(data, "bgm")
    credits = _style_table(data, "credits")
    _warn_unknown(subtitle, _SUBTITLE_STYLE_KEYS, "style.subtitle.")
    _warn_unknown(thumbnail, _THUMBNAIL_STYLE_KEYS, "style.thumbnail.")
    _warn_unknown(chart, _CHART_STYLE_KEYS, "style.chart.")
    _warn_unknown(video, _VIDEO_STYLE_KEYS, "style.video.")
    _warn_unknown(bgm, _BGM_STYLE_KEYS, "style.bgm.")
    _warn_unknown(credits, _CREDITS_STYLE_KEYS, "style.credits.")

    # 未指定ならテーマ既定のパレットを使う。明示指定は常にテーマより優先する。
    palette = chart.get("palette", list(theme.chart_palette))
    if not isinstance(palette, list) or not all(isinstance(item, str) for item in palette):
        raise ChannelConfigError("style.chart.palette must be a list of colors")
    rotation = _string(bgm, "rotation", "fixed", "style.bgm.")
    if rotation not in {"fixed", "daily", "per_corner"}:
        raise ChannelConfigError(
            "style.bgm.rotation must be fixed, daily, or per_corner"
        )
    bgm_dir_value = _string(bgm, "dir", "", "style.bgm.")

    return StyleSpec(
        subtitle=SubtitleStyle(
            font=_optional_file(
                channel_root, subtitle.get("font", ""), "style.subtitle.font"
            ),
            fill=_string(subtitle, "fill", "#ffffff", "style.subtitle."),
            stroke=_string(subtitle, "stroke", "#000000", "style.subtitle."),
            box_color=_string(
                subtitle, "box_color", "#000000", "style.subtitle."
            ),
            box_alpha=_number(
                subtitle,
                "box_alpha",
                0.45,
                "style.subtitle.",
                maximum=1.0,
            ),
            position_ratio=_number(
                subtitle,
                "position_ratio",
                0.64,
                "style.subtitle.",
                maximum=1.0,
            ),
            box_radius=_number(
                subtitle,
                "box_radius",
                theme.subtitle_box_radius,
                "style.subtitle.",
                maximum=1.0,
            ),
        ),
        thumbnail=ThumbnailStyle(
            font_family=_string(
                thumbnail,
                "font_family",
                theme.thumbnail_font_family,
                "style.thumbnail.",
            ),
            title_color=_string(
                thumbnail, "title_color", theme.thumbnail_title_color, "style.thumbnail."
            ),
            theme=theme_key,
        ),
        chart=ChartStyle(
            palette=tuple(palette),
            font=_optional_file(
                channel_root, chart.get("font", ""), "style.chart.font"
            ),
            theme=theme_key,
        ),
        video=VideoStyle(
            pad_color=_string(video, "pad_color", theme.video_pad_color, "style.video."),
            filter=_string(video, "filter", "", "style.video."),
        ),
        bgm=BgmStyle(
            dir=(
                _resolve_style_dir(channel_root, bgm_dir_value)
                if bgm_dir_value
                else config.BGM_DIR
            ),
            volume=_number(
                bgm, "volume", config.BGM_VOLUME, "style.bgm.", maximum=1.0
            ),
            rotation=rotation,
        ),
        credits=CreditsStyle(
            template=_string(credits, "template", "", "style.credits.")
        ),
        theme=theme_key,
    )


def _publish_path(data: dict[str, Any], key: str, default: str | Path) -> Path:
    value = data.get(key, default)
    if not isinstance(value, (str, Path)):
        raise ChannelConfigError(f"publish credential path must be a string: {key}")
    if not str(value).strip():
        raise ChannelConfigError(f"publish credential path must not be empty: {key}")
    return _repo_path(value)


def _ideology_credential_fallback(
    channel_id: str,
    configured: Path,
    legacy_value: str | Path,
    key: str,
) -> Path:
    """移行前の ideology 資格情報だけを旧ルートから継続利用する。"""
    legacy = _repo_path(legacy_value)
    if channel_id == "ideology" and not configured.exists() and legacy.is_file():
        warnings.warn(
            f"using legacy {key}; run tools/migrate_channels.py --apply: {legacy}",
            UserWarning,
            stacklevel=3,
        )
        return legacy
    return configured


def _load_publish(data: dict[str, Any], channel_id: str) -> PublishSpec:
    _warn_unknown(data, _PUBLISH_KEYS, "publish.")
    youtube = _publish_table(data, "youtube")
    tiktok = _publish_table(data, "tiktok")
    instagram = _publish_table(data, "instagram")
    _warn_unknown(youtube, _YOUTUBE_PUBLISH_KEYS, "publish.youtube.")
    _warn_unknown(tiktok, _TIKTOK_PUBLISH_KEYS, "publish.tiktok.")
    _warn_unknown(instagram, _INSTAGRAM_PUBLISH_KEYS, "publish.instagram.")
    review = _publish_table(youtube, "review")
    _warn_unknown(
        review,
        _YOUTUBE_REVIEW_KEYS,
        "publish.youtube.review.",
    )

    review_enabled = review.get("enabled", False)
    if not isinstance(review_enabled, bool):
        raise ChannelConfigError("publish.youtube.review.enabled must be a boolean")

    platforms = data.get("platforms", ["youtube", "tiktok", "instagram"])
    if not isinstance(platforms, list) or not all(
        isinstance(item, str) for item in platforms
    ):
        raise ChannelConfigError("publish.platforms must be a list of platform names")
    unknown = sorted(set(platforms) - _PUBLISH_PLATFORMS)
    if unknown:
        raise ChannelConfigError(
            "publish.platforms contains unsupported platforms: " + ", ".join(unknown)
        )
    if len(platforms) != len(set(platforms)):
        raise ChannelConfigError("publish.platforms must not contain duplicates")

    access_token_env = _string(
        instagram,
        "access_token_env",
        "INSTAGRAM_ACCESS_TOKEN",
        "publish.instagram.",
    )
    if access_token_env and not _ENV_NAME_RE.fullmatch(access_token_env):
        raise ChannelConfigError(
            "publish.instagram.access_token_env must be an environment variable name"
        )

    youtube_client_secret = _publish_path(
        youtube, "client_secret", config.YOUTUBE_CLIENT_SECRET_FILE
    )
    youtube_token = _publish_path(youtube, "token", config.YOUTUBE_TOKEN_FILE)
    youtube_client_secret = _ideology_credential_fallback(
        channel_id,
        youtube_client_secret,
        config.YOUTUBE_CLIENT_SECRET_FILE,
        "YouTube client secret",
    )
    youtube_token = _ideology_credential_fallback(
        channel_id,
        youtube_token,
        config.YOUTUBE_TOKEN_FILE,
        "YouTube token",
    )
    youtube_privacy = _string(
        youtube,
        "privacy",
        "unlisted" if review_enabled else config.YOUTUBE_PRIVACY,
        "publish.youtube.",
    )

    return PublishSpec(
        platforms=tuple(platforms),
        youtube=YouTubePublishSpec(
            privacy=youtube_privacy,
            client_secret=youtube_client_secret,
            token=youtube_token,
            review=YouTubeReviewSpec(enabled=review_enabled),
        ),
        tiktok=TikTokPublishSpec(
            token=_publish_path(tiktok, "token", config.TIKTOK_TOKEN_FILE),
            privacy=_string(
                tiktok, "privacy", config.TIKTOK_PRIVACY, "publish.tiktok."
            ),
        ),
        instagram=InstagramPublishSpec(
            user_id=_string(
                instagram, "user_id", config.INSTAGRAM_USER_ID, "publish.instagram."
            ),
            access_token_env=access_token_env,
        ),
    )


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ChannelConfigError(f"channel config not found: {path}") from exc
    except OSError as exc:
        raise ChannelConfigError(f"failed to read channel config: {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ChannelConfigError(f"invalid TOML: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ChannelConfigError(f"channel config must be a TOML table: {path}")
    return data


def load(channel_id: str, *, channels_dir: Path | None = None) -> ChannelSpec:
    """``channels/<id>/channel.toml`` をロードし、参照を検証する。"""
    if not channel_id or not _CHANNEL_ID_RE.fullmatch(channel_id):
        raise ChannelConfigError(f"invalid channel id: {channel_id!r}")
    base = (channels_dir or (config.ROOT / "channels")).resolve()
    root = (base / channel_id).resolve()
    path = root / "channel.toml"
    data = _read_toml(path)
    _warn_unknown(data, _TOP_LEVEL_KEYS, "")

    channel_data = data.get("channel")
    if not isinstance(channel_data, dict):
        raise ChannelConfigError("missing or invalid required table: channel")
    _warn_unknown(channel_data, _CHANNEL_KEYS, "channel.")
    spec_id = _required_str(channel_data, "id", "channel.")
    if spec_id != channel_id or root.name != channel_id:
        raise ChannelConfigError(
            f"channel.id must match directory name: {spec_id!r} != {channel_id!r}"
        )
    name = _required_str(channel_data, "name", "channel.")

    corners_data = data.get("corners")
    if not isinstance(corners_data, dict) or not corners_data:
        raise ChannelConfigError("missing or invalid required table: corners")

    voices_value = data.get("voices", "voices.json")
    if not isinstance(voices_value, str) or not voices_value.strip():
        raise ChannelConfigError("voices must be a non-empty path string")
    voices_path = _resolve_path(root, voices_value, "voices")
    try:
        channel_voices = voices.load(voices_path)
    except ValueError as exc:
        raise ChannelConfigError(str(exc)) from exc

    corners: dict[str, CornerSpec] = {}
    for key, raw in corners_data.items():
        if not isinstance(key, str) or not isinstance(raw, dict):
            raise ChannelConfigError(f"corners.{key} must be a table")
        _warn_unknown(raw, _CORNER_KEYS, f"corners.{key}.")
        voice_key = _required_str(raw, "voice", f"corners.{key}.")
        if voice_key not in channel_voices:
            raise ChannelConfigError(
                f"corners.{key}.voice references missing voices key: {voice_key}"
            )
        corners[key] = CornerSpec(
            key=key,
            label=_required_str(raw, "label", f"corners.{key}."),
            persona_path=_resolve_path(
                root,
                _required_str(raw, "persona", f"corners.{key}."),
                f"corners.{key}.persona",
            ),
            corner_path=_resolve_path(
                root,
                _required_str(raw, "corner", f"corners.{key}."),
                f"corners.{key}.corner",
            ),
            voice_key=voice_key,
        )

    rotation = channel_data.get("rotation", list(corners))
    if not isinstance(rotation, list) or not all(isinstance(item, str) for item in rotation):
        raise ChannelConfigError("channel.rotation must be a list of corner keys")
    if not rotation:
        raise ChannelConfigError("channel.rotation must not be empty")
    missing = [item for item in rotation if item not in corners]
    if missing:
        raise ChannelConfigError(
            f"channel.rotation references missing corners: {', '.join(missing)}"
        )

    pipeline = data.get("pipeline", {})
    if not isinstance(pipeline, dict):
        raise ChannelConfigError("pipeline must be a table")
    _warn_unknown(pipeline, _PIPELINE_KEYS, "pipeline.")
    cooldown_days = pipeline.get("topic_cooldown_days")
    if cooldown_days is not None and (
        isinstance(cooldown_days, bool)
        or not isinstance(cooldown_days, int)
        or cooldown_days < 0
    ):
        raise ChannelConfigError("pipeline.topic_cooldown_days must be a non-negative integer")
    performance_feedback = pipeline.get("performance_feedback")
    if performance_feedback is not None and not isinstance(performance_feedback, bool):
        raise ChannelConfigError("pipeline.performance_feedback must be a boolean")
    research_requires_youtube_case_studies = pipeline.get(
        "research_requires_youtube_case_studies"
    )
    if research_requires_youtube_case_studies is not None and not isinstance(
        research_requires_youtube_case_studies, bool
    ):
        raise ChannelConfigError(
            "pipeline.research_requires_youtube_case_studies must be a boolean"
        )
    youtube_auto_playlist = pipeline.get("youtube_auto_playlist")
    if youtube_auto_playlist is not None and not isinstance(youtube_auto_playlist, bool):
        raise ChannelConfigError("pipeline.youtube_auto_playlist must be a boolean")
    youtube_auto_engagement_comment = pipeline.get("youtube_auto_engagement_comment")
    if youtube_auto_engagement_comment is not None and not isinstance(
        youtube_auto_engagement_comment, bool
    ):
        raise ChannelConfigError(
            "pipeline.youtube_auto_engagement_comment must be a boolean"
        )
    tactic_issues = pipeline.get("tactic_issues")
    if tactic_issues is not None and not isinstance(tactic_issues, bool):
        raise ChannelConfigError("pipeline.tactic_issues must be a boolean")
    title_pattern_check = pipeline.get("title_pattern_check")
    if title_pattern_check is not None and not isinstance(title_pattern_check, bool):
        raise ChannelConfigError("pipeline.title_pattern_check must be a boolean")
    narration_opening_guard = pipeline.get("narration_opening_guard")
    if narration_opening_guard is not None and not isinstance(
        narration_opening_guard, bool
    ):
        raise ChannelConfigError("pipeline.narration_opening_guard must be a boolean")
    narration_pattern_check = pipeline.get("narration_pattern_check")
    if narration_pattern_check is not None and not isinstance(
        narration_pattern_check, bool
    ):
        raise ChannelConfigError("pipeline.narration_pattern_check must be a boolean")
    ambiguous_date_title_check = pipeline.get("ambiguous_date_title_check")
    if ambiguous_date_title_check is not None and not isinstance(
        ambiguous_date_title_check, bool
    ):
        raise ChannelConfigError("pipeline.ambiguous_date_title_check must be a boolean")
    plan_topic_retries = pipeline.get("plan_topic_retries")
    if plan_topic_retries is not None and (
        isinstance(plan_topic_retries, bool)
        or not isinstance(plan_topic_retries, int)
        or plan_topic_retries < 1
    ):
        raise ChannelConfigError("pipeline.plan_topic_retries must be a positive integer")
    max_uploads_per_day = pipeline.get("max_uploads_per_day")
    if max_uploads_per_day is not None and (
        isinstance(max_uploads_per_day, bool)
        or not isinstance(max_uploads_per_day, int)
        or max_uploads_per_day < 0
    ):
        raise ChannelConfigError(
            "pipeline.max_uploads_per_day must be a non-negative integer"
        )
    feedback_repository = pipeline.get("feedback_repository", "")
    if not isinstance(feedback_repository, str):
        raise ChannelConfigError("pipeline.feedback_repository must be a string")
    if feedback_repository and not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
        feedback_repository,
    ):
        raise ChannelConfigError(
            "pipeline.feedback_repository must be owner/name"
        )
    style = data.get("style", {})
    publish = data.get("publish", {})
    if not isinstance(style, dict):
        raise ChannelConfigError("style must be a table")
    if not isinstance(publish, dict):
        raise ChannelConfigError("publish must be a table")
    publish_spec = _load_publish(publish, spec_id)

    return ChannelSpec(
        id=spec_id,
        name=name,
        root=root,
        corners=corners,
        rotation=list(rotation),
        voices_path=voices_path,
        style=_load_style(style, root),
        publish=publish_spec,
        pipeline=dict(pipeline),
    )


def discover(*, channels_dir: Path | None = None) -> list[str]:
    """channel.toml を持つチャンネル ID を安定順で列挙する。"""
    base = channels_dir or (config.ROOT / "channels")
    if not base.is_dir():
        return []
    return sorted(
        child.name
        for child in base.iterdir()
        if child.is_dir() and (child / "channel.toml").is_file()
    )


def default_channel(*, channels_dir: Path | None = None) -> str:
    """環境変数または発見結果から既定チャンネルを決める。"""
    configured = os.environ.get("DOCI_CHANNEL", "").strip()
    if configured:
        return configured
    available = discover(channels_dir=channels_dir)
    if len(available) == 1:
        return available[0]
    if not available:
        raise ChannelConfigError("no channels found; set DOCI_CHANNEL after adding one")
    raise ChannelConfigError(
        "multiple channels found; set DOCI_CHANNEL or pass --channel: "
        + ", ".join(available)
    )


def bgm_path(spec: ChannelSpec, corner: CornerSpec | str, day: str) -> Path | None:
    """チャンネルの BGM rotation に従って決定的に1曲を選ぶ。"""
    corner_spec = spec.corners[corner] if isinstance(corner, str) else corner
    base = spec.style.bgm.dir
    search_dir = base / corner_spec.key if spec.style.bgm.rotation == "per_corner" else base
    if not search_dir.is_dir():
        return None
    files = sorted(
        path
        for path in search_dir.iterdir()
        if path.is_file() and path.suffix.lower() in _AUDIO_SUFFIXES
    )
    if not files:
        return None
    if spec.style.bgm.rotation in {"fixed", "per_corner"}:
        return files[0]
    digest = hashlib.sha256(f"{spec.id}:{day}".encode("utf-8")).digest()
    return files[int.from_bytes(digest[:8], "big") % len(files)]
