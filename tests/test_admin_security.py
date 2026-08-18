"""doci.admin.security のテスト: 秘密判定・CSRF/Host/Origin・静的配信の安全性。"""
from __future__ import annotations

import unittest

from doci.admin import security


class IsSecretTest(unittest.TestCase):
    def test_known_secret_keys(self) -> None:
        for key in (
            "ANTHROPIC_API_KEY",
            "OPENCODE_GO_API_KEY",
            "PEXELS_API_KEY",
            "GEMINI_API_KEY",
            "OPENROUTER_API_KEY",
            "MINIMAX_API_KEY",
            "TIKTOK_CLIENT_KEY",
            "TIKTOK_CLIENT_SECRET",
            "INSTAGRAM_ACCESS_TOKEN",
        ):
            self.assertTrue(security.is_secret(key), key)

    def test_suffix_heuristic_is_fail_closed(self) -> None:
        # 将来追加される未知のキーも自動でマスクされる(fail-closed)。
        self.assertTrue(security.is_secret("SOME_NEW_SERVICE_API_KEY"))
        self.assertTrue(security.is_secret("FOO_SECRET"))
        self.assertTrue(security.is_secret("FOO_TOKEN"))
        self.assertTrue(security.is_secret("FOO_PASSWORD"))

    def test_path_keys_excluded(self) -> None:
        for key in (
            "YOUTUBE_CLIENT_SECRET_FILE",
            "YOUTUBE_TOKEN_FILE",
            "YOUTUBE_ANALYTICS_TOKEN_FILE",
            "TIKTOK_TOKEN_FILE",
            "OPENCODE_AUTH_FILE",
        ):
            self.assertFalse(security.is_secret(key), key)

    def test_ordinary_keys_not_secret(self) -> None:
        for key in ("TEXT_BACKEND", "SCRIPT_LLM_TIMEOUT", "VIDEO_WIDTH"):
            self.assertFalse(security.is_secret(key), key)


class FingerprintScrubTest(unittest.TestCase):
    def test_fingerprint_is_stable_and_short(self) -> None:
        fp1 = security.fingerprint("hello")
        fp2 = security.fingerprint("hello")
        self.assertEqual(fp1, fp2)
        self.assertEqual(len(fp1), 8)

    def test_fingerprint_differs_for_different_values(self) -> None:
        self.assertNotEqual(security.fingerprint("a"), security.fingerprint("b"))

    def test_scrub_masks_known_values(self) -> None:
        text = "error: bad key SUPERSECRET123 was rejected"
        scrubbed = security.scrub(text, ["SUPERSECRET123"])
        self.assertNotIn("SUPERSECRET123", scrubbed)
        self.assertIn("***", scrubbed)

    def test_scrub_ignores_empty_values(self) -> None:
        text = "no secrets here"
        self.assertEqual(security.scrub(text, ["", None or ""]), text)


class TokenTest(unittest.TestCase):
    def test_make_token_is_random_and_long(self) -> None:
        t1 = security.make_token()
        t2 = security.make_token()
        self.assertNotEqual(t1, t2)
        self.assertGreater(len(t1), 20)

    def test_check_token_matches(self) -> None:
        self.assertTrue(security.check_token("abc", "abc"))
        self.assertFalse(security.check_token("abc", "abd"))
        self.assertFalse(security.check_token(None, "abc"))
        self.assertFalse(security.check_token("", "abc"))


class HostOriginTest(unittest.TestCase):
    def test_check_host_accepts_loopback(self) -> None:
        self.assertTrue(security.check_host("127.0.0.1:8787", "127.0.0.1", 8787))
        self.assertTrue(security.check_host("localhost:8787", "127.0.0.1", 8787))
        self.assertTrue(security.check_host("127.0.0.1", "127.0.0.1", 80))

    def test_check_host_rejects_foreign_host(self) -> None:
        self.assertFalse(security.check_host("evil.example.com", "127.0.0.1", 8787))
        self.assertFalse(security.check_host(None, "127.0.0.1", 8787))

    def test_check_host_rejects_wrong_port(self) -> None:
        self.assertFalse(security.check_host("127.0.0.1:9999", "127.0.0.1", 8787))

    def test_check_origin_allows_missing_origin(self) -> None:
        # ブラウザ以外(curl等)はOriginを送らない。CSRF対策の主眼はブラウザなので許可する。
        self.assertTrue(security.check_origin(None, 8787))

    def test_check_origin_accepts_same_origin(self) -> None:
        self.assertTrue(security.check_origin("http://127.0.0.1:8787", 8787))
        self.assertTrue(security.check_origin("http://localhost:8787", 8787))

    def test_check_origin_rejects_foreign_origin(self) -> None:
        self.assertFalse(security.check_origin("http://evil.example.com", 8787))
        self.assertFalse(security.check_origin("http://127.0.0.1:9999", 8787))


class StaticWhitelistTest(unittest.TestCase):
    def test_whitelisted_names_pass(self) -> None:
        for name in ("index.html", "app.js", "app.css"):
            self.assertEqual(security.resolve_static_name(name), name)

    def test_traversal_rejected(self) -> None:
        for name in ("../server.py", "..%2fserver.py", "/etc/passwd", "app.js.bak", ""):
            self.assertIsNone(security.resolve_static_name(name))


class EnvKeyValidationTest(unittest.TestCase):
    def test_valid_keys(self) -> None:
        for key in ("TEXT_BACKEND", "A", "A1_B2"):
            self.assertTrue(security.is_valid_env_key(key))

    def test_invalid_keys(self) -> None:
        for key in ("text_backend", "1ABC", "A-B", "A B", ""):
            self.assertFalse(security.is_valid_env_key(key))


if __name__ == "__main__":
    unittest.main()
