"""前段リサーチ (issue #6)。

OpenCode Goで提示された候補・一次資料URLを整理する（codexは明示時のみWeb取得）経路で、
コーナーに合う「きょうの題材」を1つ選び、提示資料から確認できる具体事実（人名・年・数字・定義・具体例）を
参考事実として返す。下書き(OpenCode Go)はこの「参考事実」を具体として織り込む。
出典は本文には出さない。
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from urllib.request import Request, urlopen

from . import config, llm
from .channel import ChannelSpec, CornerSpec

# バックエンドごとの「Webで確認する」手順の言い回し。OpenCode Goは候補・一次資料URLを
# 参照して整理し、codex はシェルの curl 等での取得を明示的に指示する。
_WEB_HOWTO = {
    "opencode_go": "提示された候補・一次資料URLを参照し、確認できた範囲だけを採用して",
    "claude": "WebSearch / WebFetch で確認し、",
    "codex": (
        "シェルで curl 等を使い、Web検索（例: https://duckduckgo.com/html/?q=... や "
        "Wikipedia API、ニュースAPI等）と実ページの取得で確認し、"
    ),
}

_PROMPT = """\
あなたは日本語ショート動画の構成リサーチャーです。次のコーナー向けに、きょう扱う題材を1つ選び、Web検索で裏取りした具体的事実を集めてください。

コーナー: {label}
チャンネル固有の方針:
{channel_guidance}
最近すでに扱った題材（重複を避ける）: {past}
このチャンネル自身の実績から得た形式仮説（空なら利用しない）:
{performance_guidance}
YouTube Data APIで取得した公開動画候補（YouTube系チャンネルの場合のみ）:
{video_candidates}
取得済み一次資料（本文抜粋。これらのURL以外を出典にしない）:
{reference_materials}

やること:
1. このコーナーに合う、具体的で語り甲斐のある題材を1つ選ぶ（抽象概念そのものでなく、出来事・人物・制度・数字に落ちるもの）。
2. {web_howto}台本に織り込める「検証済みの具体事実」を5〜7個集める。
   - 人名・年号・数値・定義・固有の出来事・印象的な具体例を優先。
   - 不確かなものは入れない。各事実に出典URLを付ける。
   - 公式ドキュメント、運営主体の発表、論文、公的統計などの一次資料を最優先する。
     一次資料で確認できる内容を、まとめブログやSEO記事だけで裏付けたことにしない。
   - プラットフォームの推薦ロジック、アルゴリズム内部、万能な成功基準、
     「○%を超えれば拡散される」のような数値閾値は、公式の一次資料に明記されていない限り採用しない。
{video_case_study_rule}
{extra_rules}
出力は **有効な JSON オブジェクトのみ**（前後に説明やコードフェンスを付けない）。文字列内の引用符・改行は必ずエスケープし、各 claim は1文に収める:
{{"topic": "きょうの題材（短い日本語）",
  "angle": "視聴者がハッとする切り口（1文）",
  "youtube_creator_audience": "対象者。YouTube系企画では必ず「YouTube制作者」と明記し、それ以外は空文字",
  "youtube_creator_problem": "解決する具体的なYouTube上の課題または指標（1文。該当しなければ空文字）",
  "viewer_action": "視聴後にYouTube Studioや次の動画で取れる具体的な操作（1文。該当しなければ空文字）",
  "theme_fit": "clear | ambiguous | off_topic",
  "theme_fit_reason": "主題適合判定の理由（1文）",
  "facts": [{{"claim": "検証済みの具体事実（日本語・1文）", "source_url": "...", "source_title": "..."}}],
  "examples": [{{"title": "公開動画のタイトル", "channel": "チャンネル名", "url": "YouTube動画URL", "published_at": "公開日（確認できる場合）", "observed": "冒頭・構成・見せ方など公開画面から直接観察できたこと（日本語・1文）"}}]}}
"""

_YOUTUBE_CASE_STUDY_RULE = """\
3. 「YouTubeの伸ばし方」を実際に解説している公開YouTube動画・運営者から、
   今回の題材を扱う動画を2〜3本調べ、比較事例として examples に入れる。
   - 検索結果の断片だけでなく、実際の動画ページ、説明欄、字幕など確認できた公開情報に基づく。
     候補の description はYouTube Data APIで取得した動画の全文説明欄、transcript_excerpt は公開字幕である。
   - タイトル、チャンネル、動画URL、公開日（確認できる場合）に加え、主張の共通点、
     冒頭の見せ方、説明の順序、具体例の出し方など、公開内容から直接観察できる点を記録する。
   - view_count と like_count は調査時点で変動する参考値にすぎない。数字だけから成功理由や推薦アルゴリズムの因果を断定しない。
   - 公開動画は構成の事例であり、プラットフォーム仕様の根拠には使わない。仕様は上記の一次資料で裏付ける。
   - 上に候補がある場合は実在確認済みの入口として使ってよいが、タイトルや説明文だけで内容を推測せず、
     実際の動画内容を確認できたものだけを examples に採用する。
4. 企画の主題ガードとして、次の3点を別々のフィールドへ具体的に明記する。
   - youtube_creator_audience は必ず「YouTube制作者」とする。
   - youtube_creator_problem は、その制作者が解決したいYouTube上の具体的な課題または指標を1文で書く。
   - viewer_action は、視聴後にYouTube Studioまたは次の動画制作で実行できる操作を1文で書く。
5. theme_fit は、YouTube運用が主題の中心で、題材・切り口・想定タイトルからも明確な場合だけ clear とする。
   他分野の比喩は説明手段に限定し、比喩自体が主題やタイトルの中心になる場合は ambiguous または
   off_topic とする。迷った場合は必ず ambiguous にする。
"""

# codex は内部知識だけで済ませがちなため、実際に取得したページに基づけと念押しする一文を足す。
# OpenCode Goは候補URLの内容に限定する。
_EXTRA_RULES = {
    "opencode_go": "   - 提示されたURLや候補の内容だけを根拠にし、確認できない事実・URLは作らないこと。\n",
    "claude": "",
    "codex": "   - 内部知識だけで書いてはいけない。必ず取得したページの内容に基づくこと。\n",
}


class _SearchResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if "result__a" in classes and values.get("href"):
            self._href = values["href"] or ""
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            title = " ".join("".join(self._text).split())
            if title:
                self.results.append({"url": self._href, "title": title})
            self._href = ""
            self._text = []


def _decode_search_url(url: str) -> str:
    parsed = urlparse(url if not url.startswith("//") else f"https:{url}")
    if (parsed.hostname or "").endswith("duckduckgo.com"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        url = unquote(target)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    return url


def _page_excerpt(url: str) -> str:
    try:
        request = Request(url, headers={"User-Agent": "doci/1.0"})
        with urlopen(request, timeout=8) as response:
            body = response.read(12000).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - source discovery is best effort
        return ""
    text = re.sub(r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>", " ", body, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())[:1800]


def _search_reference_materials(label: str) -> list[dict[str, str]]:
    """非Claude経路用に、検索結果ではなく取得ページの短い本文を渡す。"""
    search_url = "https://html.duckduckgo.com/html/?q=" + quote_plus(f"{label} 公式 一次資料")
    try:
        with urlopen(Request(search_url, headers={"User-Agent": "doci/1.0"}), timeout=12) as response:
            html = response.read(180000).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - research falls back safely when search is unavailable
        return []
    parser = _SearchResultParser()
    parser.feed(html)
    materials: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in parser.results[:8]:
        url = _decode_search_url(row["url"])
        if not url or url in seen:
            continue
        excerpt = _page_excerpt(url)
        if not excerpt:
            continue
        seen.add(url)
        materials.append({"url": url, "title": row["title"], "excerpt": excerpt})
        if len(materials) >= 4:
            break
    return materials


def _log(msg: str) -> None:
    print(f"[doci] {msg}", flush=True)


def _attempt(
    prompt: str,
    *,
    require_youtube_examples: bool = False,
    allowed_source_urls: set[str] | None = None,
) -> dict:
    backend = config.RESEARCH_BACKEND
    if backend == "codex":
        raw = llm.run_codex(
            prompt,
            config.CODEX_MODEL,
            timeout=config.SCRIPT_LLM_TIMEOUT,
            min_web_fetches=2,
        )
    elif backend == "opencode_go":
        from . import ai_text

        if not allowed_source_urls:
            raise ValueError(
                "OpenCode Goリサーチは、実取得済みの候補URLがないため安全側にスキップします"
            )
        raw = ai_text._run_opencode_go(
            prompt,
            ai_text._opencode_go_model(config.RESEARCH_MODEL),
            timeout=config.SCRIPT_LLM_TIMEOUT,
        )
    elif backend == "claude":
        raw = llm.run_claude(
            prompt,
            config.RESEARCH_MODEL,
            allowed_tools=["WebSearch", "WebFetch"],
            timeout=config.SCRIPT_LLM_TIMEOUT,
        )
    else:
        raise ValueError(f"未対応のRESEARCH_BACKENDです: {backend}")
    data = llm.extract_json(raw)
    facts = data.get("facts")
    if not data.get("topic") or not isinstance(facts, list) or not facts:
        raise ValueError(f"リサーチ結果が不十分です: {str(data)[:300]}")
    # 出典の無い事実は除外（裏取り済みのみ採用）
    data["facts"] = [f for f in facts if isinstance(f, dict) and f.get("claim") and f.get("source_url")]
    if backend == "opencode_go":
        data["facts"] = [
            fact
            for fact in data["facts"]
            if _normalized_source_url(str(fact.get("source_url"))) in allowed_source_urls
        ]
    if not data["facts"]:
        if backend == "opencode_go":
            raise ValueError("許可済みURLに紐づく出典付きの事実がありませんでした")
        raise ValueError("出典付きの事実がありませんでした")
    examples = data.get("examples", [])
    if not isinstance(examples, list):
        examples = []
    rejected_observations = ("タイトルから", "タイトルで", "検索結果", "推測")
    data["examples"] = [
        example
        for example in examples
        if isinstance(example, dict)
        and example.get("title")
        and example.get("channel")
        and "確認できず" not in str(example.get("channel"))
        and example.get("observed")
        and not any(
            marker in str(example.get("observed")) for marker in rejected_observations
        )
        and _is_youtube_video_url(str(example.get("url", "")))
    ]
    if require_youtube_examples and len(data["examples"]) < 2:
        raise ValueError(
            "実際の内容を確認できたYouTube解説動画の比較事例が2本未満です"
        )
    if require_youtube_examples:
        data["facts"] = [
            fact
            for fact in data["facts"]
            if _is_official_youtube_source(str(fact.get("source_url", "")))
        ]
        if len(data["facts"]) < 3:
            raise ValueError("YouTube公式一次資料に基づく事実が3件未満です")
    return data


def _is_youtube_video_url(url: str) -> bool:
    """公開事例には YouTube の動画URLだけを残す。"""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")


def _normalized_source_url(url: str) -> str:
    """比較用にクエリ・表記揺れを除いた許可済みURLキーを返す。"""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/", 1)[0]
        return f"youtube:{video_id}" if video_id else ""
    if host == "youtube.com" or host.endswith(".youtube.com"):
        video_id = parse_qs(parsed.query).get("v", [""])[0]
        return f"youtube:{video_id}" if video_id else ""
    if not parsed.scheme or not host:
        return ""
    return f"{parsed.scheme.lower()}://{host}{parsed.path.rstrip('/')}"


def _is_official_youtube_source(url: str) -> bool:
    """YouTubeの仕様根拠として採用できる公式ドメインだけを判定する。"""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    return (
        host == "blog.youtube"
        or host.endswith(".blog.youtube")
        or (host == "support.google.com" and parsed.path.startswith("/youtube/"))
    )


def _needs_youtube_case_studies(channel_guidance: str) -> bool:
    guidance = channel_guidance.casefold()
    return "youtube" in guidance or "ショート" in channel_guidance


def _youtube_video_candidates(
    spec: ChannelSpec | None,
    corner: CornerSpec,
    enabled: bool,
) -> list[dict[str, str]]:
    if not enabled or spec is None:
        return []
    try:
        from . import youtube

        candidates = youtube.search_public_videos(
            f"YouTube {corner.label} 伸ばし方",
            token_file=spec.publish.youtube.token,
            client_secret_file=spec.publish.youtube.client_secret,
        )
        enriched = youtube.add_public_transcripts(candidates)
        transcript_count = sum(bool(row.get("transcript_excerpt")) for row in enriched)
        _log(
            f"YouTube公開動画候補 {len(enriched)}本 / 公開字幕取得 {transcript_count}本"
        )
        return enriched
    except Exception as exc:  # noqa: BLE001 - Web検索へフォールバックできる補助入力
        _log(f"YouTube公開動画候補のAPI取得失敗→Web検索で継続: {str(exc)[:160]}")
        return []


def web_research(
    corner: CornerSpec,
    past_topics: list[str],
    spec: ChannelSpec | None = None,
    performance_guidance: str = "",
) -> dict | None:
    """題材選定＋Web裏取り。不正JSON等は再試行し、尽きたら例外（呼び出し側がリサーチ無しで続行）。"""
    past = "、".join(past_topics[-20:]) if past_topics else "（まだありません）"
    backend = config.RESEARCH_BACKEND
    guidance_parts = []
    for path in (corner.persona_path, corner.corner_path):
        try:
            guidance_parts.append(path.read_text(encoding="utf-8"))
        except OSError:
            continue
    channel_guidance = "\n\n".join(guidance_parts) or "（追加方針なし）"
    needs_youtube_examples = _needs_youtube_case_studies(channel_guidance)
    video_candidates = _youtube_video_candidates(
        spec, corner, needs_youtube_examples
    )
    allowed_source_urls = {
        normalized
        for row in video_candidates
        if isinstance(row, dict) and row.get("url")
        for normalized in [_normalized_source_url(str(row.get("url")))]
        if normalized
    }
    reference_materials = _search_reference_materials(corner.label) if backend == "opencode_go" else []
    allowed_source_urls.update(
        normalized
        for row in reference_materials
        if row.get("url")
        for normalized in [_normalized_source_url(str(row.get("url")))]
        if normalized
    )
    if backend == "opencode_go" and not allowed_source_urls:
        _log("OpenCode Goリサーチ: 実取得済み候補・資料がないため安全側にスキップ")
        return None
    prompt = _PROMPT.format(
        label=corner.label,
        channel_guidance=channel_guidance,
        past=past,
        performance_guidance=performance_guidance or "（比較可能な実績なし）",
        video_candidates=(
            json.dumps(video_candidates, ensure_ascii=False, indent=2)
            if video_candidates
            else "（候補なし。Web検索で探す）"
        ),
        reference_materials=(
            json.dumps(reference_materials, ensure_ascii=False, indent=2)
            if reference_materials
            else "（取得できた資料なし）"
        ),
        web_howto=_WEB_HOWTO.get(backend, _WEB_HOWTO["claude"]),
        video_case_study_rule=(
            _YOUTUBE_CASE_STUDY_RULE if needs_youtube_examples else ""
        ),
        extra_rules=_EXTRA_RULES.get(backend, _EXTRA_RULES["claude"]),
    )
    last_err: Exception | None = None
    for attempt in range(1, config.SCRIPT_RESEARCH_RETRIES + 1):
        try:
            return _attempt(
                prompt,
                require_youtube_examples=needs_youtube_examples,
                allowed_source_urls=allowed_source_urls,
            )
        except (ValueError, RuntimeError) as e:  # JSON不正/不十分/CLI失敗を再試行
            last_err = e
            if attempt < config.SCRIPT_RESEARCH_RETRIES:
                _log(f"リサーチ不良(試行{attempt}/{config.SCRIPT_RESEARCH_RETRIES})→再試行: {str(e)[:120]}")
    raise last_err or ValueError("リサーチに失敗しました")


def brief_for_prompt(research: dict) -> str:
    """下書きプロンプトへ差し込む参考事実ブロックを組み立てる。"""
    lines = [
        "## きょうの題材（リサーチ済み・これで書く。テーマ選定は不要）",
        f"題材: {research.get('topic', '')}",
    ]
    if research.get("angle"):
        lines.append(f"切り口: {research['angle']}")
    if research.get("youtube_creator_audience"):
        lines.append(f"対象者: {research['youtube_creator_audience']}")
    if research.get("youtube_creator_problem"):
        lines.append(f"解決する課題・指標: {research['youtube_creator_problem']}")
    if research.get("viewer_action"):
        lines.append(f"視聴後の操作: {research['viewer_action']}")
    lines.append(
        "\n## 参考事実（Webで裏取り済み。最低2つを具体として自然に本文へ織り込む。"
        "年・数値・固有名は正確に。これらは検証済みなので事実として述べてよい。出典は本文に書かない）。"
        "確かな具体が多いので、薄める必要はなく、内容に見合う自然な長さ（やや長めも可）で構わない:"
    )
    for f in research.get("facts", []):
        lines.append(f"- {f.get('claim', '')}")
    examples = research.get("examples", [])
    if examples:
        lines.append(
            "\n## 公開YouTube動画の比較事例（公開画面から観察した構成例。"
            "仕様の根拠や成功原因の証明ではない）"
        )
        for example in examples:
            details = [
                str(example.get("title", "")),
                str(example.get("channel", "")),
                str(example.get("url", "")),
            ]
            if example.get("published_at"):
                details.append(str(example["published_at"]))
            lines.append(f"- {' / '.join(part for part in details if part)}: {example.get('observed', '')}")
    return "\n".join(lines)
