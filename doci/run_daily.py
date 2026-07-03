"""日次オーケストレータ: コーナー選択→台本(opus4.8)→音声(VOICEVOX)→映像(Minimax)→
合成(ffmpeg)→YouTubeアップロード(unlisted)→履歴記録。1回で1本生成。
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from datetime import date as _date
from datetime import datetime
from pathlib import Path

from . import ai_text, assets, compose, config, corners, history, imagegen, routing, voicevox


def _workdir_name(day: str, corner_key: str, hhmmss: str) -> str:
    """workdir名を組み立てる。`{day}_{corner_key}` プレフィックスは検索性のため維持しつつ、
    末尾に実行時刻を足して run ごとに一意にする（同日同コーナーの後続runによる上書き喪失を防ぐ）。"""
    return f"{day}_{corner_key}_{hhmmss}"


def _log(msg: str) -> None:
    print(f"[doci] {msg}", flush=True)


def _credits(corner) -> str:
    """概要欄に付ける素材クレジット。VOICEVOX はキャラ名込みで表記必須（利用規約）。
    Pexels は必須ではないが明記する。"""
    import re as _re

    label = getattr(getattr(corner, "voice", None), "label", "") or ""
    m = _re.search(r"[（(]\s*([^/／）)]+)", label)  # 「メリケンAI (冥鳴ひまり/ノーマル)」→ 冥鳴ひまり
    char = m.group(1).strip() if m else ""
    vv = f"VOICEVOX:{char}" if char else "VOICEVOX"
    return (
        "\n\n──────────\n"
        "■ クレジット / Credits\n"
        f"音声合成: {vv}（https://voicevox.hiroshiba.jp/）\n"
        "背景・映像素材: Pexels（https://www.pexels.com/）"
    )


def run(day: str, corner_key: str | None, do_upload: bool, video_scenes: int) -> dict:
    corner = corners.CORNERS[corner_key] if corner_key else corners.pick_corner(history.last_corner())
    workdir = config.OUTPUT / _workdir_name(day, corner.key, datetime.now().strftime("%H%M%S"))
    workdir.mkdir(parents=True, exist_ok=True)
    _log(f"corner={corner.key} voice={corner.voice_key}(spk{corner.speaker}) workdir={workdir}")

    # 1) 台本
    _log("台本生成 (opus 4.8)…")
    script = ai_text.generate(corner, day, history.recent_topics())
    (workdir / "script.json").write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"title: {script['title']}  (narration {len(script['narration'])}字 / scenes {len(script['scenes'])})")

    # 2) 音声（voices.json の話者＋速度/ピッチ/抑揚/音量を適用: issue #1）
    _log("音声合成 (VOICEVOX)…")
    v = corner.voice
    tts = voicevox.synthesize(
        script["narration"], corner.speaker, workdir / "narration.wav",
        speed=v.speed, pitch=v.pitch, intonation=v.intonation,
        intonation_vary=v.intonation_vary, volume=v.volume,
    )
    _log(f"narration {tts.duration:.1f}s (spk{corner.speaker} speed{v.speed} into{v.intonation})")

    # 2.5) 尺が決まったので向き・サイズを決める。longform(>180s=YouTube通常動画)は横16:9、
    #      ショートは縦9:16。以降の素材取得・合成・AI生成へ同じ寸法/向きを流す。
    route = routing.classify(tts.duration)
    out_w, out_h, orientation = routing.output_spec(route, config.VIDEO_WIDTH, config.VIDEO_HEIGHT)
    aspect = "16:9" if route.landscape else "9:16"
    _log(f"出力: {orientation} {out_w}x{out_h} (tier={route.tier})")

    # 3) 映像（尺連動で画像枚数を増やす: issue #4）
    #    短尺は台本のシーン数のまま。長尺はシーンのプロンプトを順序保存で使い回し、
    #    1枚あたり約 SECONDS_PER_IMAGE 秒になるよう枚数を増やして間延びを防ぐ。
    scenes_meta = script["scenes"]
    n_scenes = len(scenes_meta)
    target = math.ceil(tts.duration / config.SECONDS_PER_IMAGE) if tts.duration > 0 else n_scenes
    n_images = max(n_scenes, min(target, config.MAX_IMAGES))
    if n_images > n_scenes:
        _log(f"映像スケール: {n_scenes}シーン→{n_images}枚 (約{tts.duration / n_images:.0f}s/枚)")
    use_video = config.VIDEO_BACKEND == "minimax" and video_scenes > 0
    scene_objs: list[compose.Scene] = []
    occ: dict[int, int] = {}
    chart_cache: dict[int, bool] = {}  # 図表は si ごとに1シーンに統合（重複スロットをスキップ）
    # 画像スロットの配分をビート重要度(act: 起承転結)で重み付け（issue: 単純比例だと山場も
    # 前振りも同じ枚数になり間延びする）。act未指定(空文字)は重み1.0＝現行の均等配分と完全一致。
    # 図表シーンは1スロットしか実描画されない(chart_cacheで統合)ため重み付けしても無駄なので1.0固定。
    _ACT_WEIGHT = {"起": 0.85, "承": 1.0, "転": 1.3, "結": 0.95}
    weights = [
        1.0 if sm.get("chart") else _ACT_WEIGHT.get(sm.get("act", ""), 1.0)
        for sm in scenes_meta
    ]
    cum_w = [0.0]
    for w in weights:
        cum_w.append(cum_w[-1] + w)
    total_w = cum_w[-1] or float(n_scenes)
    for j in range(n_images):
        # 左端サンプリング（旧 `j * n_scenes // n_images` と同じ基準点）。中心点(+0.5)サンプリングは
        # 均等重み時でも旧式と異なる si を選ぶことがある（実測で確認済み）ため使わない。
        pos = j * total_w / n_images
        si = n_scenes - 1
        for i in range(n_scenes):
            if cum_w[i] <= pos < cum_w[i + 1]:
                si = i
                break
        k = occ.get(si, 0)
        occ[si] = k + 1
        sm = scenes_meta[si]
        # 図表シーン（issue #2）: Pexsels/AIを使わず HTML→画像で描画し、静止表示する。
        if sm.get("chart"):
            # 図表アニメはシーン尺に合わせて compose 側で描画（spec を渡すだけ）。
            # 背景は「テーマ＋内容」から都度選定・取得（キャッシュあり）。
            # 同一図表が複数スロットに割り当たっても1シーンに統合し、再アニメを防ぐ。
            if si in chart_cache:
                continue
            chart_cache[si] = True
            from . import chart_bg
            theme = f"{script.get('title', '')} / {script.get('description', '')}"[:180]
            try:
                spec = chart_bg.ensure(sm["chart"], theme, workdir, si)
            except Exception as e:
                _log(f"図表背景の選定/取得に失敗（背景なしで継続）: {e}")
                spec = sm["chart"]
            scene_objs.append(compose.Scene(path=workdir, is_video=False,
                                            caption=sm.get("caption", ""), chart_spec=spec))
            _log(f"図表シーン (scene{si}, {sm['chart'].get('type')}) ← 背景付き・尺合わせ描画")
            continue
        img = workdir / f"scene_{si:02d}_{k}.png"
        base_prompt = sm.get("visual_prompt") or sm.get("caption") or "abstract background"
        # 1) まず実フリー素材を当てる（issue #9）。variant=k で同一シーンは別候補を選ぶ。
        #    ASSET_MEDIA=mix はシーン主画(k=0)を動画、使い回し(k>0)を写真に。video は全て動画優先。
        #    動画→写真→AI生成 の順に、各段が独立に劣化フォールバックする。
        got_path, is_video = None, False
        if config.ASSET_BACKEND not in ("", "none"):
            want_video = config.ASSET_MEDIA == "video" or (config.ASSET_MEDIA == "mix" and k == 0)
            if want_video:
                try:
                    vid = workdir / f"scene_{si:02d}_{k}.mp4"
                    got = assets.fetch_video(
                        base_prompt, vid, width=out_w, height=out_h, orientation=orientation, variant=k
                    )
                    if got is not None:
                        got_path, is_video = got, True
                        _log(f"素材取得(動画) {j + 1}/{n_images} (scene{si} var{k}, pexels)")
                except Exception as e:  # 動画失敗は写真へ
                    _log(f"動画取得失敗: {e} → 写真へ")
            if got_path is None:  # 写真モード or 動画が無かった/失敗
                try:
                    got = assets.fetch_image(
                        base_prompt, img, width=out_w, height=out_h, orientation=orientation, variant=k
                    )
                    if got is not None:
                        got_path = got
                        _log(f"素材取得(写真) {j + 1}/{n_images} (scene{si} var{k}, pexels)")
                except Exception as e:  # 写真失敗はAI生成へ
                    _log(f"写真取得失敗: {e} → AI生成へ")
        # 2) 素材が無ければAI生成（構図変化語を足して使い回しの単調を避ける）。
        if got_path is None:
            vprompt = base_prompt
            if k > 0:
                vprompt = f"{base_prompt}, alternate camera angle and composition, variation {k + 1}"
            _log(f"画像生成 {j + 1}/{n_images} (scene{si} var{k}, {config.IMAGE_BACKEND})…")
            try:
                imagegen.generate_image(vprompt, img, aspect_ratio=aspect)
                got_path = img
            except Exception as e:  # AI生成も不可(例: Gemini課金停止)→直前の素材を流用して継続
                if scene_objs:
                    _log(f"AI生成失敗: {e} → 直前の素材を流用")
                    prev = scene_objs[-1]
                    got_path, is_video = prev.path, prev.is_video
                else:
                    raise
        path = got_path
        # Minimax動画化は、既にPexsels動画でない静止画(is_video=False)に対してのみ。
        if use_video and not is_video and k == 0 and si < video_scenes:
            try:
                from . import minimax
                _log(f"動画生成 scene{si} (Minimax Hailuo)… 数分かかります")
                mp4 = workdir / f"scene_{si:02d}.mp4"
                vprompt2 = (sm["visual_prompt"] + " " + sm.get("motion", "")).strip()
                minimax.generate_video(vprompt2, mp4, first_frame_image=img)
                path, is_video = mp4, True
            except Exception as e:  # 動画失敗時は静止画にフォールバック
                _log(f"動画生成失敗→静止画にフォールバック: {e}")
        scene_objs.append(compose.Scene(path=path, is_video=is_video, caption=sm.get("caption", ""), motion=sm.get("motion", "")))

    # 4) 合成（2.5で決めた向き・サイズで）
    _log("合成 (ffmpeg)…")
    out_mp4 = workdir / "video.mp4"
    compose.compose(
        scene_objs, tts.wav_path, tts.duration, out_mp4,
        bgm=config.bgm_path(), segments=tts.segments,
        width=out_w, height=out_h,
    )
    _log(f"動画完成: {out_mp4} ({out_mp4.stat().st_size} bytes)")

    # 4.5) 配信ルーティング（route は 2.5 で算出済み: issue #3）
    _log(
        f"ルート: {route.tier} ({tts.duration:.0f}s) {orientation} "
        f"youtube_short={route.is_youtube_short} 推奨={'/'.join(route.platforms)}"
    )

    # 5) アップロード（route.platforms と各 PUBLISH_* で出し分け: issue #3）
    video_id = None
    pub_results: list = []
    if do_upload:
        from . import publish
        _log(f"投稿 (route={route.tier} → {'/'.join(route.platforms)})…")
        pub_results = publish.publish(
            out_mp4,
            title=script["title"],
            description=script["description"] + _credits(corner),
            tags=script.get("tags", []),
            route=route,
        )
        for r in pub_results:
            _log(f"  {r.platform}: {r.status}{(' ' + (r.url or r.detail)) if (r.url or r.detail) else ''}")
            if r.platform == "youtube" and r.status == "ok":
                video_id = r.id
    else:
        _log("アップロードはスキップ (--no-upload)")

    # 6) 履歴
    history.record(
        corner.key,
        script["title"],
        video_id,
        extra={
            "workdir": str(workdir),
            "description": script.get("description", ""),
            "duration_sec": round(tts.duration, 1),
            "tier": route.tier,
            "platforms": route.platforms,
            "publish": [{"platform": r.platform, "status": r.status, "id": r.id} for r in pub_results],
        },
    )
    return {
        "corner": corner.key,
        "title": script["title"],
        "video": str(out_mp4),
        "video_id": video_id,
        "duration_sec": round(tts.duration, 1),
        "tier": route.tier,
        "platforms": route.platforms,
        "publish": [{"platform": r.platform, "status": r.status} for r in pub_results],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="doci 日次生成")
    ap.add_argument("--corner", choices=list(corners.CORNERS), help="指定が無ければ前回と交互")
    ap.add_argument("--date", default=_date.today().isoformat())
    ap.add_argument("--no-upload", action="store_true", help="生成のみ（アップロードしない）")
    ap.add_argument("--video-scenes", type=int, default=config.MINIMAX_VIDEO_SCENES)
    args = ap.parse_args()
    try:
        result = run(args.date, args.corner, do_upload=not args.no_upload, video_scenes=args.video_scenes)
    except Exception as e:
        _log(f"ERROR: {e}")
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
