"""doci設定・プロンプト管理UIのHTTPサーバ（stdlibのみ、localhost限定）。

`http.server.ThreadingHTTPServer` を使う。`doci/tiktok.py` の一度限りOAuthコール
バック受けと同じ stdlib パターンで、Flask等の新規依存は追加しない（本番cronが
使う同じvenvに依存が乗るのを避けるため。詳細はモジュール群のdocstring参照）。

ハンドラは薄く保つ: ソケットI/O・ヘッダ検査(Host/Origin/CSRFトークン)・JSON
エンコード/デコードだけを担当し、実際の業務ロジックは全て `api.dispatch()`
（純粋関数）に委譲する。
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import api, security

STATIC_DIR = Path(__file__).resolve().parent / "static"

_CODE_PROMPT_PREFIX = "/api/code-prompts"


class AdminHandler(BaseHTTPRequestHandler):
    server_version = "doci-admin/0"

    # サブクラスから見えるサーバ設定（ThreadingHTTPServerのインスタンス属性経由）
    @property
    def token(self) -> str:
        return self.server.admin_token  # type: ignore[attr-defined]

    @property
    def enable_code_prompts(self) -> bool:
        return self.server.enable_code_prompts  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlibのシグネチャに合わせる
        # 既定のstderrログはURL(クエリ含む)をそのまま出すため、秘密情報を含みうる
        # POSTボディは元々出ない設計だが、念のためログ自体は静音にする。
        pass

    # --- 共通ガード ---

    def _expected_port(self) -> int:
        return self.server.server_address[1]  # type: ignore[attr-defined]

    def _host_ok(self) -> bool:
        return security.check_host(
            self.headers.get("Host"), self.server.server_address[0], self._expected_port()  # type: ignore[attr-defined]
        )

    def _origin_ok(self) -> bool:
        return security.check_origin(self.headers.get("Origin"), self._expected_port())

    def _token_ok(self) -> bool:
        return security.check_token(self.headers.get("X-Doci-Token"), self.token)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'")
        self.send_header("Referrer-Policy", "no-referrer")

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            # 不正なクライアント/プロキシが送るヘッダ(例: "Content-Length: abc")。
            # ここは _handle_api の `except Exception` より手前(ボディ読み込み時点)
            # なので、ここで捕まえないと接続が無言で切れてしまう
            # (リポジトリ側Claude Actionのレビューで指摘・実際に再現した)。
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    # --- ルーティング ---

    def do_GET(self) -> None:  # noqa: N802 - stdlibのシグネチャ
        if not self._host_ok():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "invalid host"})
            return
        if self.path == "/" or self.path.startswith("/?"):
            self._serve_index()
            return
        if self.path.startswith("/static/"):
            self._serve_static(self.path[len("/static/") :])
            return
        if self.path.startswith("/api/"):
            self._handle_api("GET")
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "invalid host"})
            return
        if not self._origin_ok():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "invalid origin"})
            return
        if self.path.startswith("/api/"):
            self._handle_api("POST")
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _handle_api(self, method: str) -> None:
        if not self._token_ok():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "invalid or missing X-Doci-Token"})
            return
        path = self.path
        if path.startswith(_CODE_PROMPT_PREFIX) and not self.enable_code_prompts:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "code-prompts機能は --enable-code-prompts 未指定のため無効です"},
            )
            return
        try:
            # ボディ読み込み(_read_json_body)とdispatchの両方をここで囲む。
            # 以前はdispatchだけを囲んでおり、その手前のボディ読み込み側の
            # 想定外の例外(例: 不正なContent-Lengthヘッダ)は素通りして接続が
            # 無言で切れていた(リポジトリ側Claude Actionのレビューで指摘・
            # 実際に再現した。_read_json_body自体も併せて修正済みだが、
            # トークン検証より後で起こりうる例外はここで一括して受け止める)。
            body = self._read_json_body() if method == "POST" else None
            status, payload = api.dispatch(method, path, body)
        except Exception:  # noqa: BLE001 - 想定外の例外でも接続を無言で落とさず、
            # JSONで500を返す。内部の例外詳細はレスポンスに含めず(スタック情報の
            # 露出を避ける)stderrにだけ出す。api.dispatch自体は既知の例外を
            # 個別に処理しているが、ここは最後の砦。
            traceback.print_exc(file=sys.stderr)
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal server error"}
            )
            return
        self._send_json(status, payload)

    def _serve_index(self) -> None:
        index_path = STATIC_DIR / "index.html"
        if not index_path.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "index.html not found"})
            return
        html = index_path.read_text(encoding="utf-8").replace("__DOCI_ADMIN_TOKEN__", self.token)
        html = html.replace(
            "__DOCI_ADMIN_ENABLE_CODE_PROMPTS__", "1" if self.enable_code_prompts else "0"
        )
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, name: str) -> None:
        resolved = security.resolve_static_name(name)
        if resolved is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        path = STATIC_DIR / resolved
        if not path.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        content_type = {
            "index.html": "text/html; charset=utf-8",
            "app.js": "application/javascript; charset=utf-8",
            "app.css": "text/css; charset=utf-8",
        }[resolved]
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)


class AdminServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, *, admin_token: str, enable_code_prompts: bool) -> None:
        super().__init__(address, handler)
        self.admin_token = admin_token
        self.enable_code_prompts = enable_code_prompts


def serve(
    host: str = "127.0.0.1",
    port: int = 8787,
    *,
    enable_code_prompts: bool = False,
    token: str | None = None,
) -> AdminServer:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("host は 127.0.0.1 または localhost のみ許可されます")
    admin_token = token or security.make_token()
    server = AdminServer(
        (host, port), AdminHandler, admin_token=admin_token, enable_code_prompts=enable_code_prompts
    )
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="doci設定・プロンプト管理UI（localhost限定・単独運用者向け）"
    )
    parser.add_argument("--host", choices=["127.0.0.1", "localhost"], default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--enable-code-prompts",
        action="store_true",
        help="Python内蔵プロンプト定数(doci/*.py)の編集を許可する。既定は無効"
        "（tools/migrate_channels.py 等と同じ、既定は安全側・明示指定でだけ危険な操作を許可する規約）。",
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="起動時にブラウザを自動で開かない"
    )
    args = parser.parse_args(argv)

    server = serve(args.host, args.port, enable_code_prompts=args.enable_code_prompts)
    url = f"http://{args.host}:{server.server_address[1]}/"
    print(f"doci admin UI: {url}", file=sys.stderr)
    if args.enable_code_prompts:
        print(
            "警告: --enable-code-prompts が有効です。doci/*.py 内のプロンプト定数への"
            "変更は git のレビューフローを経由せず即座に書き込まれます。",
            file=sys.stderr,
        )
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - ブラウザ起動失敗はサーバ稼働を妨げない
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
