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
from pathlib import Path

from . import ai_text, assets, compose, config, corners, history, imagegen, routing, voicevox


def _log(msg: str) -> None:
    print(f"[doci] {msg}", flush=True)


def run(day: str, corner_key: str | None, do_upload: bool, video_scenes: int) -> dict:
    corner = corners.CORNERS[corner_key] if corner_key else corners.pick_corner(history.last_corner())
    workdir = config.OUTPUT / f"{day}_{corner.key}"
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
    for j in range(n_images):
        si = j * n_scenes // n_images  # スロット→元シーンへ順序保存でマップ
        k = occ.get(si, 0)
        occ[si] = k + 1
        sm = scenes_meta[si]
        img = workdir / f"scene_{si:02d}_{k}.png"
        base_prompt = sm["visual_prompt"]
        # 1) まず実フリー素材を当てる（issue #9）。variant=k で同一シーンは別候補を選ぶ。
        got = None
        if config.ASSET_BACKEND not in ("", "none"):
            try:
                got = assets.fetch_image(base_prompt, img, aspect_ratio="9:16", variant=k)
                if got is not None:
                    _log(f"素材取得 {j + 1}/{n_images} (scene{si} var{k}, {config.ASSET_BACKEND})")
            except Exception as e:  # 素材失敗時はAI生成へフォールバック
                _log(f"素材取得失敗({config.ASSET_BACKEND}): {e} → AI生成にフォールバック")
        # 2) 素材が無ければAI生成（構図変化語を足して使い回しの単調を避ける）。
        if got is None:
            vprompt = base_prompt
            if k > 0:
                vprompt = f"{base_prompt}, alternate camera angle and composition, variation {k + 1}"
            _log(f"画像生成 {j + 1}/{n_images} (scene{si} var{k}, {config.IMAGE_BACKEND})…")
            imagegen.generate_image(vprompt, img, aspect_ratio="9:16")
        path, is_video = img, False
        if use_video and k == 0 and si < video_scenes:
            try:
                from . import minimax
                _log(f"動画生成 scene{si} (Minimax Hailuo)… 数分かかります")
                mp4 = workdir / f"scene_{si:02d}.mp4"
                vprompt2 = (sm["visual_prompt"] + " " + sm.get("motion", "")).strip()
                minimax.generate_video(vprompt2, mp4, first_frame_image=img)
                path, is_video = mp4, True
            except Exception as e:  # 動画失敗時は静止画にフォールバック
                _log(f"動画生成失敗→静止画にフォールバック: {e}")
        scene_objs.append(compose.Scene(path=path, is_video=is_video, caption=sm.get("caption", "")))

    # 4) 合成
    _log("合成 (ffmpeg)…")
    out_mp4 = workdir / "video.mp4"
    compose.compose(
        scene_objs, tts.wav_path, tts.duration, out_mp4,
        bgm=config.bgm_path(), segments=tts.segments,
    )
    _log(f"動画完成: {out_mp4} ({out_mp4.stat().st_size} bytes)")

    # 4.5) 尺→配信ルーティング（issue #3）
    route = routing.classify(tts.duration)
    _log(
        f"ルート: {route.tier} ({tts.duration:.0f}s) "
        f"youtube_short={route.is_youtube_short} 推奨={'/'.join(route.platforms)}"
    )

    # 5) アップロード（現状 YouTube のみ実投稿。tier で Short/長尺を出し分け）
    video_id = None
    if do_upload:
        from . import youtube
        _log("YouTube アップロード…")
        desc = script["description"] + (f"\n\n{route.hashtag}" if route.hashtag else "")
        tags = script.get("tags", [])
        if route.is_youtube_short and "Shorts" not in tags:
            tags = tags + ["Shorts"]
        video_id = youtube.upload(out_mp4, script["title"], desc, tags)
    else:
        _log("アップロードはスキップ (--no-upload)")

    # 6) 履歴
    history.record(
        corner.key,
        script["title"],
        video_id,
        extra={
            "workdir": str(workdir),
            "duration_sec": round(tts.duration, 1),
            "tier": route.tier,
            "platforms": route.platforms,
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
