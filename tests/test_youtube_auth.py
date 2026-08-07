"""YouTube トークン認証まわりのユニットテスト (issue #103)。

保存済みトークンの scopes が refresh 保存時に縮小されないこと、および
scopes 不足時のエラー挙動を検証する。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from doci import youtube

_FULL_SCOPES = [*youtube.ANALYTICS_SCOPES, youtube.MANAGE_SCOPE]


def _write_token_file(path: Path, scopes: list[str], *, expired: bool) -> None:
    from google.oauth2.credentials import Credentials

    expiry = (
        datetime.now(timezone.utc) - timedelta(hours=1)
        if expired
        else datetime.now(timezone.utc) + timedelta(hours=1)
    )
    creds = Credentials(
        token="fake-token",
        refresh_token="fake-refresh-token",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="fake-client",
        client_secret="fake-secret",
        scopes=scopes,
        expiry=expiry,
    )
    path.write_text(creds.to_json(), encoding="utf-8")


class LoadCredentialsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.token_file = Path(self._tmp.name) / "token.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_refresh_preserves_stored_scopes(self) -> None:
        """refresh 後の保存でトークンファイルの scopes が縮小されない(issue #103)。"""
        _write_token_file(self.token_file, _FULL_SCOPES, expired=True)
        from google.oauth2.credentials import Credentials

        with mock.patch.object(Credentials, "refresh", return_value=None):
            creds = youtube._load_credentials(
                False, token_file=self.token_file, scopes=youtube.MANAGE_SCOPES
            )
        self.assertIsNotNone(creds)
        stored = json.loads(self.token_file.read_text(encoding="utf-8"))
        self.assertEqual(set(stored["scopes"]), set(_FULL_SCOPES))

    def test_upload_only_refresh_keeps_full_scopes(self) -> None:
        """upload のみ要求の refresh でも、保存トークンの scopes は維持される。"""
        _write_token_file(self.token_file, _FULL_SCOPES, expired=True)
        from google.oauth2.credentials import Credentials

        with mock.patch.object(Credentials, "refresh", return_value=None):
            creds = youtube._load_credentials(
                False, token_file=self.token_file, scopes=youtube.SCOPES
            )
        self.assertIsNotNone(creds)
        stored = json.loads(self.token_file.read_text(encoding="utf-8"))
        self.assertEqual(set(stored["scopes"]), set(_FULL_SCOPES))

    def test_missing_scopes_raise_runtime_error(self) -> None:
        """保存 scopes が要求を満たさなければ既存メッセージで RuntimeError。"""
        _write_token_file(self.token_file, youtube.SCOPES, expired=False)
        with self.assertRaisesRegex(RuntimeError, "scopeが不足"):
            youtube._load_credentials(
                False, token_file=self.token_file, scopes=youtube.MANAGE_SCOPES
            )

    def test_valid_creds_returned_without_refresh(self) -> None:
        """有効なトークンは refresh せずに返り、ファイルも変更されない。"""
        _write_token_file(self.token_file, _FULL_SCOPES, expired=False)
        before = self.token_file.read_text(encoding="utf-8")
        from google.oauth2.credentials import Credentials

        with mock.patch.object(Credentials, "refresh") as refresh:
            creds = youtube._load_credentials(
                False, token_file=self.token_file, scopes=youtube.MANAGE_SCOPES
            )
        self.assertIsNotNone(creds)
        refresh.assert_not_called()
        self.assertEqual(
            self.token_file.read_text(encoding="utf-8"), before
        )


if __name__ == "__main__":
    unittest.main()
