"""doci.admin.server のHTTP層E2Eテスト。

`doci/tiktok.py` の一度限りOAuthコールバック受けと同じ「ポート0でデーモンスレッド
起動→実HTTPクライアントで叩く」パターンを使う。読み取り専用の実リポジトリに対して
起動するため、書き込み系は行わない(書き込みは各store個別のテストが担当)。
"""
from __future__ import annotations

import http.client
import json
import socket
import threading
import unittest
from unittest import mock

from doci.admin import server


class AdminServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.token = "test-token-abc123"
        cls.srv = server.serve(
            "127.0.0.1", 0, enable_code_prompts=False, token=cls.token
        )
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.shutdown()
        cls.srv.server_close()

    def _request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=hdrs)
        res = conn.getresponse()
        raw = res.read()
        conn.close()
        content_type = res.getheader("Content-Type", "")
        payload = json.loads(raw) if "json" in content_type and raw else raw
        return res.status, dict(res.getheaders()), payload

    def test_index_serves_with_token_embedded(self) -> None:
        status, headers, body = self._request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(self.token, body.decode("utf-8"))
        self.assertEqual(headers.get("Content-Security-Policy"), "default-src 'self'")

    def test_api_without_token_is_forbidden(self) -> None:
        status, _, payload = self._request("GET", "/api/status")
        self.assertEqual(status, 403)

    def test_api_with_correct_token_succeeds(self) -> None:
        status, _, payload = self._request(
            "GET", "/api/status", headers={"X-Doci-Token": self.token}
        )
        self.assertEqual(status, 200)
        self.assertIn("channels", payload)

    def test_api_with_wrong_token_is_forbidden(self) -> None:
        status, _, _ = self._request("GET", "/api/status", headers={"X-Doci-Token": "wrong"})
        self.assertEqual(status, 403)

    def test_bad_host_header_is_forbidden(self) -> None:
        status, _, _ = self._request(
            "GET",
            "/api/status",
            headers={"X-Doci-Token": self.token, "Host": "evil.example.com"},
        )
        self.assertEqual(status, 403)

    def test_code_prompts_gated_off_by_default(self) -> None:
        status, _, payload = self._request(
            "GET", "/api/code-prompts", headers={"X-Doci-Token": self.token}
        )
        self.assertEqual(status, 404)

    def test_unexpected_exception_returns_500_json_not_dropped_connection(self) -> None:
        # 修正前は api.dispatch() 内の未捕捉例外でTCP接続がそのまま切れ、クライアント
        # 側は RemoteDisconnected になっていた(cronバナーの根拠となる/api/statusで
        # 実際に再現した)。ここでは任意の関数に例外を注入し、接続が切れず
        # JSON 500が返ることを確認する。
        with mock.patch("doci.admin.api.dispatch", side_effect=RuntimeError("boom")):
            status, _, payload = self._request(
                "GET", "/api/status", headers={"X-Doci-Token": self.token}
            )
        self.assertEqual(status, 500)
        self.assertIn("error", payload)
        # スタックトレース等の内部詳細をレスポンスへ漏らさない。
        self.assertNotIn("RuntimeError", json.dumps(payload))

    def test_malformed_content_length_header_does_not_drop_connection(self) -> None:
        # `int(Content-Length)` は以前 _handle_api の try/except より手前
        # (ボディ読み込み時点)で実行されており、不正なヘッダ(例: 数値でない)を
        # 受けるとValueErrorが素通りして接続が無言で切れていた
        # (リポジトリ側Claude Actionのレビューで指摘・実際に再現した)。
        # http.clientはContent-Lengthを自動計算し直してしまうため、生ソケットで
        # 意図的に壊れたヘッダを送る。修正後は不正なヘッダを「ボディ無し」として
        # 扱うため、空ボディを許容する/api/env/validateはむしろ200で正常応答する
        # (=クラッシュせず、意味のある応答を返せている)。
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            request = (
                f"POST /api/env/validate HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                f"X-Doci-Token: {self.token}\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: not-a-number\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("utf-8")
            sock.sendall(request)
            response = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
        finally:
            sock.close()
        self.assertTrue(response, "接続が無言で切れ、何も返ってこなかった")
        status_line = response.split(b"\r\n", 1)[0]
        status_code = int(status_line.split()[1])
        # 200(ボディ無し扱いで正常応答)・500(何らかの理由で失敗)のいずれでも、
        # 「接続が無言で切れる」ことさえなければ良い。重要なのはレスポンスが
        # 実際に届くこと自体。
        self.assertIn(status_code, (200, 500))
        self.assertIn(b'"', response)  # JSON応答が返っている(空でクラッシュしていない)

    def test_post_with_foreign_origin_is_forbidden(self) -> None:
        status, _, _ = self._request(
            "POST",
            "/api/env/validate",
            {"changes": {}},
            headers={"X-Doci-Token": self.token, "Origin": "http://evil.example.com"},
        )
        self.assertEqual(status, 403)

    def test_post_with_same_origin_succeeds(self) -> None:
        status, _, payload = self._request(
            "POST",
            "/api/env/validate",
            {"changes": {}},
            headers={
                "X-Doci-Token": self.token,
                "Origin": f"http://127.0.0.1:{self.port}",
            },
        )
        self.assertEqual(status, 200, payload)

    def test_static_traversal_rejected(self) -> None:
        status, _, _ = self._request("GET", "/static/../server.py")
        self.assertIn(status, (400, 404))

    def test_static_whitelisted_file_served(self) -> None:
        status, headers, body = self._request("GET", "/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers.get("Content-Type", ""))

    def test_static_non_whitelisted_file_rejected(self) -> None:
        status, _, _ = self._request("GET", "/static/app.py")
        self.assertEqual(status, 404)


class AdminServerHostValidationTest(unittest.TestCase):
    def test_serve_rejects_non_loopback_host(self) -> None:
        with self.assertRaises(ValueError):
            server.serve("0.0.0.0", 0)


class AdminServerCodePromptsEnabledTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.token = "enabled-token"
        cls.srv = server.serve("127.0.0.1", 0, enable_code_prompts=True, token=cls.token)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_code_prompts_reachable_when_enabled(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/code-prompts", headers={"X-Doci-Token": self.token})
        res = conn.getresponse()
        payload = json.loads(res.read())
        conn.close()
        self.assertEqual(res.status, 200)
        self.assertEqual(len(payload["code_prompts"]), 11)


if __name__ == "__main__":
    unittest.main()
