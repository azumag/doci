"""既存 workdir の素材/台本/ナレーションを再利用して、音声と合成だけ回し直す。

用途: BGM ダッキングや末尾 afade などのオーディオ系パラメータを config.py / .env で
調整したときに、台本生成（LLM）・素材取得（Pexsels）・図表描画（Chrome）をスキップして
mp4 だけ最短で再レンダするための検証ツール。

  python -m tools.recompose --workdir output/2026-06-25_capitalism

- script.json と既存 scene_*.png/mp4/_chart.png を前提とする。
- ナレーションは voicevox で再合成（同じ text/voices 設定なら同じ wav が得られる）。
- 出力は workdir/video.mp4（上書き）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# repo root を sys.path に積む（tools/ から直接 / からも import できるように）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from doci import compose, config, voicevox  # noqa: E402
from doci.corners import CORNERS  # noqa: E402


def _log(msg: str) -> None:
    print(f"[recompose] {msg}", flush=True)


def _build_scene_objs(workdir: Path, script: dict) -> list[compose.Scene]:
    """run_daily.run() と同じ scene_objs を既存ファイルから組み立てる。

    workdir に scene_{si:02d}_chart.png があれば chart シーン、
    scene_{si:02d}_{k}.mp4/png があれば asset シーンとして拾う。
    各シーンの "main" は k=0 相当（最初の variant）を使う。
    """
    objs: list[compose.Scene] = []
    for si, sm in enumerate(script["scenes"]):
        caption = sm.get("caption", "")
        if sm.get("chart"):
            # 図表アニメはシーン尺に合わせて compose 側で描画する（spec を渡すだけ）。
            objs.append(compose.Scene(path=workdir, is_video=False, caption=caption,
                                      chart_spec=sm["chart"]))
            continue
        # 最初の variant (k=0) を探す（.mp4 → .png の優先順）
        for ext, is_v in ((".mp4", True), (".png", False), (".jpg", False)):
            cand = workdir / f"scene_{si:02d}_0{ext}"
            if cand.exists():
                objs.append(compose.Scene(path=cand, is_video=is_v, caption=caption))
                break
        else:
            raise FileNotFoundError(
                f"scene {si:02d} の main ファイルが見つからない: {workdir}/scene_{si:02d}_0.{{mp4,png,jpg}}"
            )
    return objs


def main() -> None:
    ap = argparse.ArgumentParser(description="既存 workdir を素材に音声と合成だけ回し直す")
    ap.add_argument("--workdir", required=True, help="例: output/2026-06-25_capitalism")
    ap.add_argument("--corner", default=None, help="コーナーキー (省略時: script.json の _corner)")
    ap.add_argument("--no-tts", action="store_true",
                    help="音声再合成をスキップし既存 narration.wav を使う（segments は付かない＝シーン見出し字幕にフォールバック）")
    args = ap.parse_args()

    workdir = Path(args.workdir).resolve()
    if not workdir.is_dir():
        raise SystemExit(f"workdir が見つからない: {workdir}")

    script_path = workdir / "script.json"
    if not script_path.exists():
        raise SystemExit(f"script.json が見つからない: {script_path}")
    script = json.loads(script_path.read_text(encoding="utf-8"))

    corner_key = args.corner or script.get("_corner")
    if not corner_key or corner_key not in CORNERS:
        raise SystemExit(f"corner が判別できない (_corner={script.get('_corner')!r})")
    corner = CORNERS[corner_key]
    v = corner.voice
    _log(f"corner={corner.key} voice={corner.voice_key}(spk{corner.speaker} "
         f"speed{v.speed} pitch{v.pitch} into{v.intonation})")

    # 1) 音声再合成（segments 取得のため）
    tts = None
    if args.no_tts:
        narration_wav = workdir / "narration.wav"
        if not narration_wav.exists():
            raise SystemExit(f"--no-tts 指定だが narration.wav がない: {narration_wav}")
        # duration を wav から推定
        import wave
        with wave.open(str(narration_wav), "rb") as w:
            dur = w.getnframes() / float(w.getframerate())
        _log(f"--no-tts: 既存 narration.wav を使用 (duration={dur:.2f}s, segments なし)")
    else:
        _log("VOICEVOX 再合成 (既存 script.json の narration)…")
        tts = voicevox.synthesize(
            script["narration"], corner.speaker, workdir / "narration.wav",
            speed=v.speed, pitch=v.pitch, intonation=v.intonation,
            intonation_vary=v.intonation_vary, volume=v.volume,
        )
        narration_wav = tts.wav_path
        _log(f"narration {tts.duration:.2f}s, segments={len(tts.segments)}")

    # 2) scene_objs 組み立て
    scene_objs = _build_scene_objs(workdir, script)
    _log(f"scenes: {len(scene_objs)} objs (chart={sum(1 for s in scene_objs if s.static)}, "
         f"video={sum(1 for s in scene_objs if s.is_video and not s.static)}, "
         f"image={sum(1 for s in scene_objs if not s.is_video and not s.static)})")

    # 3) 合成
    duration = tts.duration if tts else dur
    segments = tts.segments if tts else None
    out_mp4 = workdir / "video.mp4"
    # バックアップ
    if out_mp4.exists():
        bak = workdir / "video.prev.mp4"
        if not bak.exists():
            out_mp4.rename(bak)
            _log(f"旧 video.mp4 → video.prev.mp4 に退避")
        else:
            out_mp4.unlink()
    _log(f"compose → {out_mp4} (BGM={config.bgm_path()})…")
    compose.compose(
        scene_objs, narration_wav, duration, out_mp4,
        bgm=config.bgm_path(), segments=segments,
        width=config.VIDEO_WIDTH, height=config.VIDEO_HEIGHT,
    )
    _log(f"完了: {out_mp4} ({out_mp4.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
