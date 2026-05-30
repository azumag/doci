"""日次オーケストレータ: コーナー選択→台本(opus4.8)→音声(VOICEVOX)→映像(Minimax)→
合成(ffmpeg)→YouTubeアップロード(unlisted)→履歴記録。1回で1本生成。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date as _date
from pathlib import Path

from . import ai_text, compose, config, corners, history, imagegen, voicevox


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

    # 2) 音声
    _log("音声合成 (VOICEVOX)…")
    tts = voicevox.synthesize(script["narration"], corner.speaker, workdir / "narration.wav")
    _log(f"narration {tts.duration:.1f}s")

    # 3) 映像
    scenes_meta = script["scenes"]
    scene_objs: list[compose.Scene] = []
    use_video = config.VIDEO_BACKEND == "minimax" and video_scenes > 0
    for i, sm in enumerate(scenes_meta):
        img = workdir / f"scene_{i:02d}.png"
        _log(f"画像生成 scene{i} ({config.IMAGE_BACKEND})…")
        imagegen.generate_image(sm["visual_prompt"], img, aspect_ratio="9:16")
        path, is_video = img, False
        if use_video and i < video_scenes:
            try:
                from . import minimax
                _log(f"動画生成 scene{i} (Minimax Hailuo)… 数分かかります")
                mp4 = workdir / f"scene_{i:02d}.mp4"
                vprompt = (sm["visual_prompt"] + " " + sm.get("motion", "")).strip()
                minimax.generate_video(vprompt, mp4, first_frame_image=img)
                path, is_video = mp4, True
            except Exception as e:  # 動画失敗時は静止画にフォールバック
                _log(f"動画生成失敗→静止画にフォールバック: {e}")
        scene_objs.append(compose.Scene(path=path, is_video=is_video, caption=sm.get("caption", "")))

    # 4) 合成
    _log("合成 (ffmpeg)…")
    out_mp4 = workdir / "video.mp4"
    compose.compose(scene_objs, tts.wav_path, tts.duration, out_mp4, bgm=config.bgm_path())
    _log(f"動画完成: {out_mp4} ({out_mp4.stat().st_size} bytes)")

    # 5) アップロード
    video_id = None
    if do_upload:
        from . import youtube
        _log("YouTube アップロード…")
        desc = script["description"] + "\n\n#Shorts"
        video_id = youtube.upload(out_mp4, script["title"], desc, script.get("tags", []))
    else:
        _log("アップロードはスキップ (--no-upload)")

    # 6) 履歴
    history.record(corner.key, script["title"], video_id, extra={"workdir": str(workdir)})
    return {"corner": corner.key, "title": script["title"], "video": str(out_mp4), "video_id": video_id}


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
