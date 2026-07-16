"""timeline を「1トピック=1背景」の順次フローとして合成する。

各出来事の背景(画像=Ken Burns / 動画=クリップ)を順に全面表示し xfade で切替えた
「背景トラック」を作り、その上に透過の「見出し＋ラベル＋伸びる矢印」オーバーレイ(CDP)を
合成する。出来事ごとに背景が切り替わり、矢印が伸びて次のトピックへ進む。
"""
from __future__ import annotations

import base64
import json
import subprocess
import tempfile
from pathlib import Path

from . import charts, config
from .channel import ChartStyle

_XFADE = 0.6  # 背景切替のクロスフェード秒


def _slot_len(dur: float, n: int) -> float:
    # xfade で重なる分を足して、合計が dur になる1トピックの尺。
    return (dur + (n - 1) * _XFADE) / n


def _bg_clip(bg: dict, length: float, W: int, H: int, out: Path) -> Path:
    """1トピックの背景を length 秒の WxH クリップに。image=Ken Burns / video=ループ＆クロップ。"""
    path = bg.get("path")
    media = bg.get("media")
    fps = config.VIDEO_FPS
    if media == "video" and path and Path(path).exists():
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
              f"fps={fps},setsar=1,format=yuv420p")
        cmd = ["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(path), "-t", f"{length:.3f}",
               "-an", "-vf", vf]
    elif path and Path(path).exists():
        frames = max(1, round(length * fps))
        # Ken Burns: ゆっくりズーム。
        vf = (f"scale={W * 2}:-1,zoompan=z='min(zoom+0.0009,1.22)':d={frames}:"
              f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={fps},"
              f"trim=duration={length:.3f},setsar=1,format=yuv420p")
        cmd = ["ffmpeg", "-y", "-loop", "1", "-t", f"{length:.3f}", "-i", str(path), "-vf", vf]
    else:
        # 背景が取れなかった: 暗色単色。
        cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i",
               f"color=c=0x0d0b08:s={W}x{H}:d={length:.3f}:r={fps}", "-vf", "format=yuv420p"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(out)]
    subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return out


def _bg_track(bgs: list[dict], dur: float, W: int, H: int, td: Path) -> Path:
    """各トピック背景を作り、xfade で連結した背景トラックを返す。"""
    n = len(bgs) or 1
    L = _slot_len(dur, n)
    clips = [_bg_clip(b, L, W, H, td / f"bg_{i}.mp4") for i, b in enumerate(bgs)]
    if n == 1:
        return clips[0]
    # xfade を順に連結。offset_k = k*(L - _XFADE)
    inputs = []
    for c in clips:
        inputs += ["-i", str(c)]
    parts = []
    prev = "0:v"
    for k in range(1, n):
        off = k * (L - _XFADE)
        lab = f"x{k}"
        parts.append(
            f"[{prev}][{k}:v]xfade=transition=fade:duration={_XFADE}:offset={off:.3f}[{lab}]"
        )
        prev = lab
    filt = ";".join(parts)
    out = td / "bgtrack.mp4"
    cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", filt, "-map", f"[{prev}]",
           "-r", str(config.VIDEO_FPS), "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "20", "-pix_fmt", "yuv420p", str(out)]
    subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    return out


def _overlay_html(
    spec: dict,
    w: int,
    h: int,
    style: ChartStyle | None = None,
) -> str:
    """順次フローの透過オーバーレイ HTML（背景は無し・scrim＋見出し＋ラベル＋矢印）。
    spec の "place"(起承転結) は受け取っても画面には表示しない。"""
    evs = spec.get("events") or []
    events_json = json.dumps(
        [{"year": charts._esc(e.get("year")), "label": charts._esc(e.get("label"))} for e in evs],
        ensure_ascii=False,
    )
    # 文字サイズ(vh)は縦9:16(h=1920)を基準にチューニング済み。横16:9等 h<1920 では
    # vh の実pxが縮むため、charts._scale で絶対px相当を揃える（issue #12: 長尺のフォント縮小対策）。
    s = charts._scale(w, h)
    year_fs, label_fs, stem_w, head_w, head_h, counter_fs = (
        round(13.0 * s, 2), round(4.4 * s, 2), round(0.6 * s, 3),
        round(1.6 * s, 3), round(2.0 * s, 3), round(2.1 * s, 3),
    )
    page = (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        "*{margin:0;padding:0;box-sizing:border-box}"
        "html,body{width:100%;height:100%;overflow:hidden;background:transparent}"
        "body{font-family:'Hiragino Sans','Hiragino Kaku Gothic ProN',sans-serif;color:#f3ecdd}"
        ".scrim{position:fixed;inset:0;background:linear-gradient(180deg,"
        "rgba(8,7,9,.62) 0%,rgba(8,7,9,.34) 42%,rgba(8,7,9,.72) 100%)}"
        ".frame{position:fixed;inset:2.4vh 4.2vw;border:.18vh solid rgba(232,182,90,.3);border-radius:.6vh}"
        ".stage{position:fixed;inset:0;display:flex;flex-direction:column;justify-content:center;"
        "align-items:center;text-align:center;padding:0 9vw 30vh 9vw}"
        f".year{{font-family:'Hiragino Mincho ProN',serif;font-weight:700;font-size:{year_fs}vh;"
        "color:#f4c25c;line-height:1;text-shadow:0 .5vh 2.4vh rgba(0,0,0,.85);letter-spacing:.01em}"
        f".label{{font-size:{label_fs}vh;line-height:1.45;color:#f5efe2;margin-top:2.6vh;max-width:80vw;"
        "text-shadow:0 .3vh 1.8vh rgba(0,0,0,.9);font-feature-settings:'palt'}"
        ".arrow{position:fixed;left:50%;top:62vh;transform:translateX(-50%);"
        "display:flex;flex-direction:column;align-items:center}"
        f".stem{{width:{stem_w}vh;background:linear-gradient(180deg,#f0b450,#caa05a);"
        "box-shadow:0 0 1.6vh rgba(240,180,80,.6);border-radius:.6vh}"
        f".head{{width:0;height:0;border-left:{head_w}vh solid transparent;"
        f"border-right:{head_w}vh solid transparent;border-top:{head_h}vh solid #f0b450;margin-top:-.1vh}}"
        f".counter{{position:fixed;bottom:6vh;left:0;right:0;text-align:center;color:#caa05a;"
        f"font-size:{counter_fs}vh;letter-spacing:.4em}}"
        "</style></head><body>"
        "<div class='scrim'></div><div class='frame'></div>"
        "<div class='stage' id='stage'></div>"
        "<div class='arrow' id='arrow'><div class='stem' id='stem'></div><div class='head'></div></div>"
        "<div class='counter' id='counter'></div>"
        "<script>"
        f"const EVENTS={events_json};const N=EVENTS.length;"
        "const cl=x=>Math.max(0,Math.min(1,x));const eo=x=>1-Math.pow(1-x,3);"
        "const stage=document.getElementById('stage'),arrow=document.getElementById('arrow'),"
        "stem=document.getElementById('stem'),counter=document.getElementById('counter');"
        "window.__apply=function(p){p=cl(p);const fp=p*N;let ti=Math.min(N-1,Math.floor(fp));"
        "let lo=fp-ti;const e=EVENTS[ti];"
        "const fin=eo(cl(lo/0.2));const fout=1-eo(cl((lo-0.84)/0.16));const op=Math.min(fin,fout);"
        "stage.innerHTML='<div class=\"year\">'+e.year+'</div><div class=\"label\">'+e.label+'</div>';"
        "stage.style.opacity=op;stage.style.transform='translateY('+((1-fin)*3)+'vh)';"
        "if(ti<N-1){const ap=cl((lo-0.66)/0.28);stem.style.height=(eo(ap)*15)+'vh';"
        "arrow.style.opacity=ap>0?Math.min(1,ap*2)*fout:0;}else{arrow.style.opacity=0;stem.style.height='0';}"
        "counter.textContent=(ti+1)+' / '+N;void document.body.offsetHeight;};"
        "window.__apply(1);"
        "</script></body></html>"
    )
    return charts._apply_style_html(page, style)


def _overlay_frames(
    spec: dict,
    dur: float,
    fps: int,
    W: int,
    H: int,
    td: Path,
    style: ChartStyle | None = None,
) -> Path:
    """透過オーバーレイを CDP で1セッション撮影し、PNG(アルファ)連番ディレクトリを返す。"""
    html = _overlay_html(spec, W, H, style)
    hp = td / "overlay.html"
    hp.write_text(html, encoding="utf-8")
    fdir = td / "ov"
    fdir.mkdir()
    frames = max(2, round(dur * fps))
    cdp = charts._CDP(W, H)
    try:
        cdp.call("Browser.getVersion")
        tgt = cdp.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        sid = cdp.call("Target.attachToTarget", {"targetId": tgt, "flatten": True})["sessionId"]
        cdp.call("Page.enable", sid=sid)
        cdp.call("Runtime.enable", sid=sid)
        cdp.call("Emulation.setDeviceMetricsOverride",
                 {"width": W, "height": H, "deviceScaleFactor": 1, "mobile": False}, sid=sid)
        # 透過背景で撮る（合成の下地＝背景トラックを透かす）。
        cdp.call("Emulation.setDefaultBackgroundColorOverride",
                 {"color": {"r": 0, "g": 0, "b": 0, "a": 0}}, sid=sid)
        cdp.call("Page.navigate", {"url": f"file://{hp}"}, sid=sid)
        cdp.wait_event("Page.loadEventFired", sid=sid)
        for i in range(frames):
            p = i / (frames - 1)
            cdp.call("Runtime.evaluate", {"expression": f"window.__apply({p:.5f})"}, sid=sid)
            shot = cdp.call("Page.captureScreenshot", {"format": "png"}, sid=sid)
            (fdir / f"f{i:04d}.png").write_bytes(base64.b64decode(shot["data"]))
    finally:
        cdp.close()
    return fdir


def render(
    spec: dict,
    out_mp4: Path,
    duration: float,
    width: int | None = None,
    height: int | None = None,
    fps: int = 12,
    style: ChartStyle | None = None,
) -> Path:
    """順次フロー timeline を out_mp4 に合成して返す。"""
    W = width or config.VIDEO_WIDTH
    H = height or config.VIDEO_HEIGHT
    out_mp4 = Path(out_mp4)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    dur = max(2.0, float(duration))
    bgs = spec.get("_bgs") or [{"media": "image", "path": None} for _ in (spec.get("events") or [1])]
    with tempfile.TemporaryDirectory(prefix="doci_tlseq_") as tds:
        td = Path(tds)
        bgtrack = _bg_track(bgs, dur, W, H, td)
        ovdir = _overlay_frames(spec, dur, fps, W, H, td, style)
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(bgtrack), "-framerate", str(fps), "-i", str(ovdir / "f%04d.png"),
             "-filter_complex", "[0:v][1:v]overlay=0:0:shortest=1,format=yuv420p[v]",
             "-map", "[v]", "-r", str(config.VIDEO_FPS), "-c:v", "libx264", "-preset", "medium",
             "-crf", "19", "-t", f"{dur:.3f}", str(out_mp4)],
            capture_output=True, text=True, timeout=300,
        )
    if not out_mp4.exists():
        raise RuntimeError(f"timeline 順次フロー合成失敗: {r.stderr[-400:]}")
    return out_mp4
