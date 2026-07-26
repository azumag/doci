"""図表シーンの背景素材を「テーマ＋図表内容」に合わせて都度選定・取得する。

LLM(CHART_BG_BACKEND設定。既定 OpenCode Go、codex/Claudeは旧設定を明示した場合のみ)が各項目の
英語検索クエリと media(image/video)を返し、Pexels から取得。
結果は workdir に scene_NN_chart_bg.json としてキャッシュし、再レンダで使い回す。
timeline は各出来事ごとに1背景（順次切替用）、それ以外は1背景。
"""
from __future__ import annotations

import json
from pathlib import Path

from . import assets, config, llm


class UnsupportedChartBackendError(ValueError):
    """CHART_BG_BACKEND の設定値が未対応であることを示す。"""


def _log(message: str) -> None:
    print(f"[doci] {message}", flush=True)


def _items_desc(spec: dict) -> tuple[str, int]:
    t = spec.get("type")
    if t == "timeline":
        evs = spec.get("events") or []
        lines = "\n".join(f'  {i}: {e.get("year")} {e.get("label")}' for i, e in enumerate(evs))
        return f"timeline（各出来事ごとに背景を1つ、出来事の順に）:\n{lines}", len(evs) or 1
    if t == "stat":
        return f'stat: 値「{spec.get("value")}」 / 説明「{spec.get("caption")}」', 1
    if t == "compare":
        its = spec.get("items") or []
        body = " / ".join(f'{x.get("value")}={x.get("label")}' for x in its)
        return f"compare: {body}", 1
    if t == "donut":
        its = spec.get("items") or []
        body = " / ".join(f'{x.get("label")}={x.get("display") or x.get("value")}' for x in its)
        return f"donut(構成比): {body}", 1
    if t == "line":
        pts = spec.get("points") or []
        body = " → ".join(f'{p.get("x")}={p.get("display") or p.get("y")}' for p in pts)
        return f"line(推移): {body}", 1
    return f'{t}: {spec.get("title", "")}', 1


def select(spec: dict, theme: str) -> list[dict]:
    """各背景の {query(英語), media(image|video)} を LLM で都度選定。"""
    desc, n = _items_desc(spec)
    prompt = (
        "あなたは縦型解説動画の背景素材ディレクター。テーマと図表の内容に強く関連する背景を選ぶ。\n"
        f"テーマ: {theme}\n"
        f"図表タイトル: {spec.get('title', '')}\n{desc}\n\n"
        "各背景について次を決める:\n"
        "- query: Pexelsで検索する英語キーワード(2〜4語)。その項目の情景/物/場所を具体的に。"
        "顔のアップや文字入り画像は避ける。\n"
        "- media: \"image\" か \"video\"。動きが映える題材は video、静物・質感は image。"
        "全体で image と video を適度に混ぜる。\n"
        f"背景は {n} 個必要（順番厳守）。JSON のみ出力する:\n"
        '{"backgrounds":[{"query":"...","media":"image"}]}'
    )
    if config.CHART_BG_BACKEND == "codex":
        # Web取得は不要なタスクなので fetch ガードは無効化(min_web_fetches=0)。
        # timeout はサンドボックス起動オーバーヘッドを見込む。
        txt = llm.run_codex(prompt, config.CODEX_MODEL, timeout=180, min_web_fetches=0)
    elif config.CHART_BG_BACKEND == "opencode_go":
        from . import ai_text

        txt = ai_text._run_opencode_go(
            prompt,
            ai_text._opencode_go_model(config.OPENCODE_MODEL or config.TEXT_MODEL),
            timeout=120,
        )
    elif config.CHART_BG_BACKEND == "opencode":
        from . import ai_text

        txt = ai_text._run_opencode(
            prompt,
            ai_text._opencode_cli_model(config.TEXT_MODEL),
            config.OPENCODE_AGENT,
            timeout=120,
        )
    elif config.CHART_BG_BACKEND == "claude":
        txt = llm.run_claude(
            prompt, config.legacy_claude_model(config.TEXT_MODEL), timeout=120
        )
    else:
        raise UnsupportedChartBackendError(
            f"未対応のCHART_BG_BACKENDです: {config.CHART_BG_BACKEND}"
        )
    data = llm.extract_json(txt)
    raw = data.get("backgrounds") or []
    out: list[dict] = []
    for b in raw[:n]:
        q = str(b.get("query") or "").strip() or theme
        m = "video" if str(b.get("media", "")).lower().startswith("v") else "image"
        out.append({"query": q, "media": m})
    while len(out) < n:
        out.append({"query": theme, "media": "image"})
    return out


def _fetch_one(b: dict, out_base: Path) -> dict:
    """1背景を取得。video が無ければ image にフォールバック。{query,media,path} を返す。"""
    media, query = b["media"], b["query"]
    path = None
    try:
        if media == "video":
            out = out_base.with_suffix(".mp4")
            path = assets.fetch_video(query, out, orientation="portrait")
        else:
            out = out_base.with_suffix(".jpg")
            path = assets.fetch_image(
                query, out, width=config.VIDEO_WIDTH, height=config.VIDEO_HEIGHT, orientation="portrait"
            )
    except Exception:
        path = None
    if path is None and media == "video":
        media = "image"
        try:
            out = out_base.with_suffix(".jpg")
            path = assets.fetch_image(
                query, out, width=config.VIDEO_WIDTH, height=config.VIDEO_HEIGHT, orientation="portrait"
            )
        except Exception:
            path = None
    return {"query": query, "media": media, "path": str(path) if path else None}


def ensure(spec: dict, theme: str, workdir: Path, idx: int) -> dict:
    """背景を選定・取得し、spec に _bg(単一) / _bgs(timeline 用リスト) を付与して返す。
    scene_NN_chart_bg.json があれば再利用（LLM/取得をスキップ＝再レンダ高速化）。"""
    workdir = Path(workdir)
    cache = workdir / f"scene_{idx:02d}_chart_bg.json"
    is_timeline = spec.get("type") == "timeline"
    if cache.exists():
        meta = json.loads(cache.read_text(encoding="utf-8"))
    else:
        try:
            sel = select(spec, theme)
        except UnsupportedChartBackendError:
            _log(
                "エラー: 未対応のCHART_BG_BACKEND="
                f"{config.CHART_BG_BACKEND}。設定を修正して再実行してください"
            )
            raise
        # 単一背景(stat/compare/bar)は Chrome に背景画像として埋め込むため image 固定。
        # 動画背景は timeline(順次フロー=ffmpeg合成)でのみ使う。
        if not is_timeline:
            for b in sel:
                b["media"] = "image"
        meta = [
            _fetch_one(b, workdir / f"scene_{idx:02d}_chart_bg_{k}")
            for k, b in enumerate(sel)
        ]
        cache.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    spec = dict(spec)
    if spec.get("type") == "timeline":
        spec["_bgs"] = meta
    else:
        spec["_bg"] = meta[0]["path"] if meta else None
        spec["_bg_media"] = meta[0]["media"] if meta else "image"
    return spec
