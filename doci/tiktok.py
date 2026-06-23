"""TikTok Content Posting API v2 アップロード (issue #3)。

初回のみ OAuth 同意:
    python -m doci.tiktok --auth     # ブラウザで認可→token保存

依存: 標準ライブラリのみ（urllib）。資格情報は config の TIKTOK_CLIENT_KEY/SECRET。

注意:
- scope は `video.publish`（Direct Post）。アプリ審査前は privacy=SELF_ONLY(非公開)のみ可。
  審査後に TIKTOK_PRIVACY=PUBLIC_TO_EVERYONE。
- 動画はチャンクで直接アップロード（FILE_UPLOAD）。≤64MBは1チャンク、超過は分割。
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import config

_AUTHORIZE = "https://www.tiktok.com/v2/auth/authorize/"
_TOKEN = "https://open.tiktokapis.com/v2/oauth/token/"
_INIT = "https://open.tiktokapis.com/v2/post/publish/video/init/"
_STATUS = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
_SCOPE = "video.publish"
_MB = 1024 * 1024


class TikTokError(RuntimeError):
    pass


def _post_form(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def _post_json(url: str, token: str, body: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


# ---------------- OAuth / token ----------------
def _save_token(d: dict) -> None:
    Path(config.TIKTOK_TOKEN_FILE).write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def _load_token() -> dict | None:
    p = Path(config.TIKTOK_TOKEN_FILE)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _exchange(fields: dict) -> dict:
    fields = {"client_key": config.TIKTOK_CLIENT_KEY, "client_secret": config.TIKTOK_CLIENT_SECRET, **fields}
    d = _post_form(_TOKEN, fields)
    if "access_token" not in d:
        raise TikTokError(f"トークン取得失敗: {json.dumps(d)[:300]}")
    d["expires_at"] = time.time() + int(d.get("expires_in", 0)) - 60
    _save_token(d)
    return d


def _access_token() -> str:
    tok = _load_token()
    if not tok:
        raise TikTokError("TikTok未認証です。`python -m doci.tiktok --auth` を実行してください。")
    if tok.get("expires_at", 0) <= time.time():
        if not tok.get("refresh_token"):
            raise TikTokError("TikTokトークン期限切れ・refresh不可。再認証してください。")
        tok = _exchange({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
    return tok["access_token"]


def _oauth_interactive() -> None:
    import http.server
    import secrets
    import threading
    import webbrowser

    if not (config.TIKTOK_CLIENT_KEY and config.TIKTOK_CLIENT_SECRET):
        raise TikTokError("TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET を設定してください。")
    redirect = config.TIKTOK_REDIRECT_URI
    parsed = urllib.parse.urlparse(redirect)
    state = secrets.token_urlsafe(16)
    code_box: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code_box["code"] = (q.get("code") or [None])[0]
            code_box["state"] = (q.get("state") or [None])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write("認可完了。ターミナルに戻ってください。".encode())

        def log_message(self, *a):  # 静音
            pass

    srv = http.server.HTTPServer((parsed.hostname, parsed.port or 80), Handler)
    threading.Thread(target=srv.handle_request, daemon=True).start()

    auth_url = _AUTHORIZE + "?" + urllib.parse.urlencode(
        {
            "client_key": config.TIKTOK_CLIENT_KEY,
            "scope": _SCOPE,
            "response_type": "code",
            "redirect_uri": redirect,
            "state": state,
        }
    )
    print("ブラウザで認可してください:\n", auth_url)
    webbrowser.open(auth_url)
    for _ in range(120):
        if "code" in code_box:
            break
        time.sleep(1)
    srv.server_close()
    if not code_box.get("code"):
        raise TikTokError("認可コードを取得できませんでした（タイムアウト）。")
    if code_box.get("state") != state:
        raise TikTokError("state 不一致（CSRFの疑い）。中止します。")
    _exchange(
        {"grant_type": "authorization_code", "code": code_box["code"], "redirect_uri": redirect}
    )
    print(f"認証完了: {config.TIKTOK_TOKEN_FILE}")


# ---------------- 投稿 ----------------
def _caption(title: str, tags: list[str]) -> str:
    hash_part = " ".join(f"#{t.lstrip('#')}" for t in (tags or [])[:5] if t.strip())
    return (f"{title} {hash_part}").strip()[:2200]


def upload(video_path: Path, title: str, description: str, tags: list[str]) -> dict:
    """Direct Post で動画を投稿。{publish_id, status} を返す。"""
    token = _access_token()
    video_path = Path(video_path)
    size = video_path.stat().st_size
    # ≤64MBは1チャンク。超過は ~32MB 分割（各 5–64MB 制約を満たす）。
    if size <= 64 * _MB:
        chunk_size, total = size, 1
    else:
        chunk_size = 32 * _MB
        total = (size + chunk_size - 1) // chunk_size

    init = _post_json(
        _INIT,
        token,
        {
            "post_info": {
                "title": _caption(title, tags),
                "privacy_level": config.TIKTOK_PRIVACY,
                "disable_comment": False,
                "disable_duet": False,
                "disable_stitch": False,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": chunk_size,
                "total_chunk_count": total,
            },
        },
    )
    data = init.get("data") or {}
    publish_id = data.get("publish_id")
    upload_url = data.get("upload_url")
    if not (publish_id and upload_url):
        raise TikTokError(f"init失敗: {json.dumps(init)[:300]}")

    with video_path.open("rb") as f:
        for idx in range(total):
            start = idx * chunk_size
            f.seek(start)
            buf = f.read(chunk_size if idx < total - 1 else size - start)
            end = start + len(buf) - 1
            req = urllib.request.Request(upload_url, data=buf, method="PUT")
            req.add_header("Content-Type", "video/mp4")
            req.add_header("Content-Range", f"bytes {start}-{end}/{size}")
            with urllib.request.urlopen(req, timeout=300):
                pass

    # ステータス確認（数回ポーリング）
    status = ""
    for _ in range(10):
        try:
            st = _post_json(_STATUS, token, {"publish_id": publish_id})
            status = ((st.get("data") or {}).get("status")) or ""
        except Exception:  # noqa: BLE001
            break
        if status in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX", "FAILED"):
            break
        time.sleep(3)
    print(f"tiktok: publish_id={publish_id} status={status} privacy={config.TIKTOK_PRIVACY}")
    return {"publish_id": publish_id, "status": status}


def main() -> None:
    ap = argparse.ArgumentParser(description="TikTok 投稿")
    ap.add_argument("--auth", action="store_true", help="初回OAuth同意してtokenを保存")
    ap.add_argument("--video")
    ap.add_argument("--title", default="doci test")
    args = ap.parse_args()
    if args.auth:
        _oauth_interactive()
        return
    if args.video:
        print(upload(Path(args.video), args.title, "", []))


if __name__ == "__main__":
    main()
