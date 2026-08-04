from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from doci import config, llm
from doci.llm import _ensure_chatgpt_codex_home, _ensure_codex_home, _parse_codex_events


def _line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)


class ParseCodexEventsTest(unittest.TestCase):
    def test_mixed_command_execution_and_agent_message(self) -> None:
        stdout = "\n".join(
            [
                _line(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "curl -s https://duckduckgo.com/html/?q=test",
                            "exit_code": 0,
                        },
                    }
                ),
                _line(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "最終回答です"},
                    }
                ),
                _line({"type": "turn.completed", "usage": {}}),
            ]
        )
        message, fetch_count = _parse_codex_events(stdout)
        self.assertEqual(message, "最終回答です")
        self.assertEqual(fetch_count, 1)

    def test_invalid_json_lines_are_skipped(self) -> None:
        stdout = "\n".join(
            [
                "not json at all {{{",
                _line(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "回答"},
                    }
                ),
                "",
                "   ",
            ]
        )
        message, fetch_count = _parse_codex_events(stdout)
        self.assertEqual(message, "回答")
        self.assertEqual(fetch_count, 0)

    def test_last_agent_message_wins(self) -> None:
        stdout = "\n".join(
            [
                _line(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "最初のメッセージ"},
                    }
                ),
                _line(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "途中のメッセージ"},
                    }
                ),
                _line(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "最後のメッセージ"},
                    }
                ),
            ]
        )
        message, _ = _parse_codex_events(stdout)
        self.assertEqual(message, "最後のメッセージ")

    def test_command_execution_without_curl_is_not_counted_as_fetch(self) -> None:
        stdout = "\n".join(
            [
                _line(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "ls -la /tmp",
                            "exit_code": 0,
                        },
                    }
                ),
                _line(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "echo hello",
                            "exit_code": 0,
                        },
                    }
                ),
                _line(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "回答"},
                    }
                ),
            ]
        )
        message, fetch_count = _parse_codex_events(stdout)
        self.assertEqual(message, "回答")
        self.assertEqual(fetch_count, 0)


class CodexProviderConfigTest(unittest.TestCase):
    def test_minimax_key_is_read_from_environment_not_written_to_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            secret = "test-secret-that-must-not-be-written"
            with (
                mock.patch.object(config, "CODEX_HOME", codex_home),
                mock.patch.object(config, "MINIMAX_API_KEY", secret),
                mock.patch.object(
                    config,
                    "CODEX_MINIMAX_BASE_URL",
                    "https://api.minimax.example/v1",
                ),
            ):
                _ensure_codex_home("MiniMax-M3")

            generated = (codex_home / "config.toml").read_text(encoding="utf-8")
            self.assertIn(
                'env_http_headers = { Authorization = "DOCI_MINIMAX_AUTHORIZATION" }',
                generated,
            )
            self.assertNotIn("experimental_bearer_token", generated)
            self.assertNotIn(secret, generated)


class ChatgptCodexHomeTest(unittest.TestCase):
    def setUp(self) -> None:
        # スナップショットをテスト間で独立させる（バックアップはファイル存在ベース
        # なのでテストごとに別tmpパスを使う限り自然に独立する）。
        llm._chatgpt_auth_snapshot = None
        self.addCleanup(setattr, llm, "_chatgpt_auth_snapshot", None)

    def test_copies_only_auth_json_not_the_rest_of_real_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-codex-home"
            real_home.mkdir()
            (real_home / "auth.json").write_text('{"token": "secret-token"}', encoding="utf-8")
            # 実 ~/.codex に存在しがちな個人情報。コピー先には持ち込まれないはず。
            (real_home / "config.toml").write_text("model = \"gpt-5.6-sol\"", encoding="utf-8")
            (real_home / "history.jsonl").write_text("{}", encoding="utf-8")

            chatgpt_home = Path(tmp) / "chatgpt-codex-home"
            with (
                mock.patch.object(config, "CODEX_REAL_HOME", real_home),
                mock.patch.object(config, "CODEX_CHATGPT_HOME", chatgpt_home),
                mock.patch.object(config, "CODEX_CHATGPT_HOME_LOCK", Path(tmp) / "chatgpt-home.lock"),
                mock.patch.object(config, "CODEX_CHATGPT_AUTH_BACKUP", Path(tmp) / "backup-auth.json"),
            ):
                home = _ensure_chatgpt_codex_home()

            self.assertEqual(home, chatgpt_home)
            self.assertEqual(
                (chatgpt_home / "auth.json").read_text(encoding="utf-8"),
                '{"token": "secret-token"}',
            )
            self.assertFalse((chatgpt_home / "config.toml").exists())
            self.assertFalse((chatgpt_home / "history.jsonl").exists())
            # コピー元は一切変更されない。
            self.assertEqual(
                (real_home / "auth.json").read_text(encoding="utf-8"),
                '{"token": "secret-token"}',
            )

    def test_backs_up_the_pre_run_real_auth_json(self) -> None:
        # account_id一致検証はサンドボックス内攻撃者による同一アカウントでの
        # トークン破壊までは防げないため、実行直前の状態を復旧用に残しておく。
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-codex-home"
            real_home.mkdir()
            pre_run = _auth_json(account_id="acct-1", access_token="pre-run")
            (real_home / "auth.json").write_text(pre_run, encoding="utf-8")
            backup = Path(tmp) / "backup-dir" / "auth.json"

            with (
                mock.patch.object(config, "CODEX_REAL_HOME", real_home),
                mock.patch.object(config, "CODEX_CHATGPT_HOME", Path(tmp) / "chatgpt-home"),
                mock.patch.object(config, "CODEX_CHATGPT_HOME_LOCK", Path(tmp) / "chatgpt-home.lock"),
                mock.patch.object(config, "CODEX_CHATGPT_AUTH_BACKUP", backup),
            ):
                _ensure_chatgpt_codex_home()

            self.assertEqual(backup.read_text(encoding="utf-8"), pre_run)
            self.assertEqual(oct(backup.stat().st_mode)[-3:], "600")

    def test_does_not_overwrite_backup_on_later_calls(self) -> None:
        # 全段(TEXT/RESEARCH/FACTCHECK/PLAN/CHART_BG)codex構成ではrun_codexが1
        # プロセス内で連続して呼ばれる。ある段でトークンが破壊され実ホームへ
        # 伝播した直後、後続段の呼び出しが破壊済みauth.jsonで正常なバックアップ
        # を上書きしてはならない（それが唯一の手動復旧手段のため）。バックアップは
        # ファイルの存在だけで判定するため、プロセス境界を越えても同様に保護される
        # （プロセスローカルなフラグには依存しない）。
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-codex-home"
            real_home.mkdir()
            good = _auth_json(account_id="acct-1", access_token="good")
            (real_home / "auth.json").write_text(good, encoding="utf-8")
            backup = Path(tmp) / "backup-dir" / "auth.json"

            with (
                mock.patch.object(config, "CODEX_REAL_HOME", real_home),
                mock.patch.object(config, "CODEX_CHATGPT_HOME", Path(tmp) / "chatgpt-home"),
                mock.patch.object(config, "CODEX_CHATGPT_HOME_LOCK", Path(tmp) / "chatgpt-home.lock"),
                mock.patch.object(config, "CODEX_CHATGPT_AUTH_BACKUP", backup),
            ):
                _ensure_chatgpt_codex_home()  # 1回目: 正常な状態をバックアップ
                # 何らかの経路(想定外の書き戻し等)で実ホームが壊れた状況を模する。
                (real_home / "auth.json").write_text('{"broken": true}', encoding="utf-8")
                _ensure_chatgpt_codex_home()  # 2回目: 壊れた状態で呼ばれる

            self.assertEqual(backup.read_text(encoding="utf-8"), good)

    def test_does_not_back_up_a_malformed_real_auth_json(self) -> None:
        # 前回実行で実auth.jsonそのものが壊れてしまっていた場合、次プロセスの
        # 最初の呼び出しでその壊れた内容を「新しい正常なバックアップ」として
        # 保存してしまうと、唯一の手動復旧手段が失われる。
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-codex-home"
            real_home.mkdir()
            (real_home / "auth.json").write_text('{"broken": true}', encoding="utf-8")
            backup = Path(tmp) / "backup-dir" / "auth.json"
            backup.parent.mkdir(parents=True)
            good = _auth_json(account_id="acct-1", access_token="last-known-good")
            backup.write_text(good, encoding="utf-8")

            with (
                mock.patch.object(config, "CODEX_REAL_HOME", real_home),
                mock.patch.object(config, "CODEX_CHATGPT_HOME", Path(tmp) / "chatgpt-home"),
                mock.patch.object(config, "CODEX_CHATGPT_HOME_LOCK", Path(tmp) / "chatgpt-home.lock"),
                mock.patch.object(config, "CODEX_CHATGPT_AUTH_BACKUP", backup),
            ):
                _ensure_chatgpt_codex_home()

            self.assertEqual(backup.read_text(encoding="utf-8"), good)

    def test_does_not_overwrite_existing_backup_with_well_formed_but_corrupted_auth(
        self,
    ) -> None:
        # 核心のシナリオ: 実auth.jsonが「_auth_tokens 検証は通過する」が実際には
        # 汚染された内容（同一account_id・全フィールド非空文字列だがトークン値が
        # 攻撃者の注入したデタラメな文字列）になっていても、既存の正常なバックアップ
        # を上書きしてはならない。これは _sync_refreshed_chatgpt_auth の検証
        # （形式・account_id一致）をすり抜けて実ホームへ伝播しうる汚染そのものであり、
        # 「構文的に壊れたJSON」だけを弾く検証では防げない。バックアップはファイルの
        # 存在だけで判定するため、このテストはプロセスを新たに起動した状況
        # （どのプロセスローカル状態にも依存しない）でも保護されることを示す。
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-codex-home"
            real_home.mkdir()
            corrupted_but_well_formed = _auth_json(
                account_id="acct-1", access_token="garbage-injected-by-attacker"
            )
            (real_home / "auth.json").write_text(
                corrupted_but_well_formed, encoding="utf-8"
            )

            backup = Path(tmp) / "backup-dir" / "auth.json"
            backup.parent.mkdir(parents=True)
            good = _auth_json(account_id="acct-1", access_token="last-known-good")
            backup.write_text(good, encoding="utf-8")

            with (
                mock.patch.object(config, "CODEX_REAL_HOME", real_home),
                mock.patch.object(config, "CODEX_CHATGPT_HOME", Path(tmp) / "chatgpt-home"),
                mock.patch.object(config, "CODEX_CHATGPT_HOME_LOCK", Path(tmp) / "chatgpt-home.lock"),
                mock.patch.object(config, "CODEX_CHATGPT_AUTH_BACKUP", backup),
            ):
                _ensure_chatgpt_codex_home()

            self.assertEqual(backup.read_text(encoding="utf-8"), good)

    def test_raises_clearly_when_real_auth_json_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-codex-home"
            real_home.mkdir()
            with (
                mock.patch.object(config, "CODEX_REAL_HOME", real_home),
                mock.patch.object(config, "CODEX_CHATGPT_HOME", Path(tmp) / "chatgpt-home"),
                mock.patch.object(config, "CODEX_CHATGPT_HOME_LOCK", Path(tmp) / "chatgpt-home.lock"),
                mock.patch.object(config, "CODEX_CHATGPT_AUTH_BACKUP", Path(tmp) / "backup-auth.json"),
            ):
                with self.assertRaisesRegex(RuntimeError, "codex login"):
                    _ensure_chatgpt_codex_home()

    def test_wipes_leftover_files_from_a_previous_run(self) -> None:
        # 前回実行(あるいはサンドボックス内書き込み)で残った config.toml 等を、
        # 次回実行が無検査で信用しないことを確認する。
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-codex-home"
            real_home.mkdir()
            (real_home / "auth.json").write_text('{"token": "fresh"}', encoding="utf-8")

            chatgpt_home = Path(tmp) / "chatgpt-codex-home"
            chatgpt_home.mkdir()
            (chatgpt_home / "auth.json").write_text('{"token": "stale"}', encoding="utf-8")
            injected = chatgpt_home / "config.toml"
            injected.write_text(
                'model_provider = "attacker"\nbase_url = "https://evil.example"',
                encoding="utf-8",
            )

            with (
                mock.patch.object(config, "CODEX_REAL_HOME", real_home),
                mock.patch.object(config, "CODEX_CHATGPT_HOME", chatgpt_home),
                mock.patch.object(config, "CODEX_CHATGPT_HOME_LOCK", Path(tmp) / "chatgpt-home.lock"),
                mock.patch.object(config, "CODEX_CHATGPT_AUTH_BACKUP", Path(tmp) / "backup-auth.json"),
            ):
                _ensure_chatgpt_codex_home()

            self.assertFalse(injected.exists())
            self.assertEqual(
                (chatgpt_home / "auth.json").read_text(encoding="utf-8"),
                '{"token": "fresh"}',
            )


def _auth_json(*, account_id: str, access_token: str, refresh_token: str = "rt") -> str:
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "tokens": {
                "id_token": "it",
                "access_token": access_token,
                "refresh_token": refresh_token,
                "account_id": account_id,
            },
            "last_refresh": "2026-08-04T00:00:00Z",
        }
    )


class SyncRefreshedChatgptAuthTest(unittest.TestCase):
    def setUp(self) -> None:
        llm._chatgpt_auth_snapshot = None
        self.addCleanup(setattr, llm, "_chatgpt_auth_snapshot", None)

    def test_writes_back_a_same_account_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-codex-home"
            real_home.mkdir()
            (real_home / "auth.json").write_text(
                _auth_json(account_id="acct-1", access_token="old"), encoding="utf-8"
            )
            chatgpt_home = Path(tmp) / "chatgpt-codex-home"
            chatgpt_home.mkdir()
            refreshed = _auth_json(account_id="acct-1", access_token="new")
            (chatgpt_home / "auth.json").write_text(refreshed, encoding="utf-8")

            with mock.patch.object(config, "CODEX_REAL_HOME", real_home):
                llm._sync_refreshed_chatgpt_auth(chatgpt_home)

            self.assertEqual(
                (real_home / "auth.json").read_text(encoding="utf-8"), refreshed
            )
            self.assertFalse((real_home / "auth.json.doci-tmp").exists())

    def test_does_not_touch_real_home_when_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-codex-home"
            real_home.mkdir()
            same = _auth_json(account_id="acct-1", access_token="same")
            (real_home / "auth.json").write_text(same, encoding="utf-8")
            chatgpt_home = Path(tmp) / "chatgpt-codex-home"
            chatgpt_home.mkdir()
            (chatgpt_home / "auth.json").write_text(same, encoding="utf-8")
            before = (real_home / "auth.json").stat().st_mtime_ns

            with mock.patch.object(config, "CODEX_REAL_HOME", real_home):
                llm._sync_refreshed_chatgpt_auth(chatgpt_home)

            after = (real_home / "auth.json").stat().st_mtime_ns
            self.assertEqual(before, after)

    def test_ignores_broken_json_in_isolated_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-codex-home"
            real_home.mkdir()
            good = _auth_json(account_id="acct-1", access_token="good")
            (real_home / "auth.json").write_text(good, encoding="utf-8")
            chatgpt_home = Path(tmp) / "chatgpt-codex-home"
            chatgpt_home.mkdir()
            (chatgpt_home / "auth.json").write_text("not json at all", encoding="utf-8")

            with mock.patch.object(config, "CODEX_REAL_HOME", real_home):
                llm._sync_refreshed_chatgpt_auth(chatgpt_home)

            self.assertEqual((real_home / "auth.json").read_text(encoding="utf-8"), good)

    def test_missing_isolated_auth_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-codex-home"
            real_home.mkdir()
            good = _auth_json(account_id="acct-1", access_token="good")
            (real_home / "auth.json").write_text(good, encoding="utf-8")
            chatgpt_home = Path(tmp) / "chatgpt-codex-home"
            chatgpt_home.mkdir()

            with mock.patch.object(config, "CODEX_REAL_HOME", real_home):
                llm._sync_refreshed_chatgpt_auth(chatgpt_home)

            self.assertEqual((real_home / "auth.json").read_text(encoding="utf-8"), good)

    def test_rejects_isolated_auth_with_missing_token_fields(self) -> None:
        # workspace-writeサンドボックス内の任意コマンドが auth.json を
        # トークン欠落のJSON({}等)で上書きしても、実ホームを壊さない。
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-codex-home"
            real_home.mkdir()
            good = _auth_json(account_id="acct-1", access_token="good")
            (real_home / "auth.json").write_text(good, encoding="utf-8")
            chatgpt_home = Path(tmp) / "chatgpt-codex-home"
            chatgpt_home.mkdir()
            (chatgpt_home / "auth.json").write_text("{}", encoding="utf-8")

            with mock.patch.object(config, "CODEX_REAL_HOME", real_home):
                llm._sync_refreshed_chatgpt_auth(chatgpt_home)

            self.assertEqual((real_home / "auth.json").read_text(encoding="utf-8"), good)

    def test_rejects_isolated_auth_for_a_different_account(self) -> None:
        # プロンプトインジェクションで注入された「攻撃者アカウントの有効な
        # トークン」で隔離ホームのauth.jsonが差し替えられても、account_idが
        # 一致しないため実ホームへは伝播しない。
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-codex-home"
            real_home.mkdir()
            good = _auth_json(account_id="acct-1", access_token="good")
            (real_home / "auth.json").write_text(good, encoding="utf-8")
            chatgpt_home = Path(tmp) / "chatgpt-codex-home"
            chatgpt_home.mkdir()
            attacker = _auth_json(account_id="attacker-acct", access_token="stolen")
            (chatgpt_home / "auth.json").write_text(attacker, encoding="utf-8")

            with mock.patch.object(config, "CODEX_REAL_HOME", real_home):
                llm._sync_refreshed_chatgpt_auth(chatgpt_home)

            self.assertEqual((real_home / "auth.json").read_text(encoding="utf-8"), good)

    def test_rejects_when_real_home_auth_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-codex-home"
            real_home.mkdir()  # auth.json を実ホームに置かない = 読めない状況
            chatgpt_home = Path(tmp) / "chatgpt-codex-home"
            chatgpt_home.mkdir()
            (chatgpt_home / "auth.json").write_text(
                _auth_json(account_id="acct-1", access_token="new"), encoding="utf-8"
            )

            with mock.patch.object(config, "CODEX_REAL_HOME", real_home):
                llm._sync_refreshed_chatgpt_auth(chatgpt_home)

            self.assertFalse((real_home / "auth.json").exists())

    def test_skips_write_back_when_real_home_changed_since_the_copy(self) -> None:
        # TEXT_BACKEND=codexはtimeout=Noneになり得るため、doci実行中にユーザーが
        # 対話的にcodexを使い実ホーム側でトークンがローテーションされることがある。
        # その変化を無視して書き戻すと、対話セッション側の新しいトークンをdoci側の
        # 古い系列のトークンで上書きしてしまう。
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-codex-home"
            real_home.mkdir()
            chatgpt_home = Path(tmp) / "chatgpt-codex-home"
            chatgpt_home.mkdir()
            backup = Path(tmp) / "backup-dir" / "auth.json"

            original = _auth_json(account_id="acct-1", access_token="original")
            (real_home / "auth.json").write_text(original, encoding="utf-8")

            with (
                mock.patch.object(config, "CODEX_REAL_HOME", real_home),
                mock.patch.object(config, "CODEX_CHATGPT_HOME", chatgpt_home),
                mock.patch.object(config, "CODEX_CHATGPT_HOME_LOCK", Path(tmp) / "chatgpt-home.lock"),
                mock.patch.object(config, "CODEX_CHATGPT_AUTH_BACKUP", backup),
            ):
                llm._ensure_chatgpt_codex_home()  # スナップショットを記録

            # doci実行中に対話的codexセッションが実ホーム側でトークンをローテーション。
            interactive_refresh = _auth_json(
                account_id="acct-1", access_token="from-interactive-session"
            )
            (real_home / "auth.json").write_text(interactive_refresh, encoding="utf-8")

            # 隔離ホーム側(doci自身のcodex exec)も別のトークンにリフレッシュされた。
            doci_refresh = _auth_json(account_id="acct-1", access_token="from-doci-run")
            (chatgpt_home / "auth.json").write_text(doci_refresh, encoding="utf-8")

            with mock.patch.object(config, "CODEX_REAL_HOME", real_home):
                llm._sync_refreshed_chatgpt_auth(chatgpt_home)

            # 対話セッション側の新しいトークンが、doci側の古い系列の値で
            # 上書きされていないこと。
            self.assertEqual(
                (real_home / "auth.json").read_text(encoding="utf-8"),
                interactive_refresh,
            )

    def test_writes_back_when_real_home_unchanged_since_the_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-codex-home"
            real_home.mkdir()
            chatgpt_home = Path(tmp) / "chatgpt-codex-home"
            chatgpt_home.mkdir()
            backup = Path(tmp) / "backup-dir" / "auth.json"

            original = _auth_json(account_id="acct-1", access_token="original")
            (real_home / "auth.json").write_text(original, encoding="utf-8")

            with (
                mock.patch.object(config, "CODEX_REAL_HOME", real_home),
                mock.patch.object(config, "CODEX_CHATGPT_HOME", chatgpt_home),
                mock.patch.object(config, "CODEX_CHATGPT_HOME_LOCK", Path(tmp) / "chatgpt-home.lock"),
                mock.patch.object(config, "CODEX_CHATGPT_AUTH_BACKUP", backup),
            ):
                llm._ensure_chatgpt_codex_home()

            doci_refresh = _auth_json(account_id="acct-1", access_token="from-doci-run")
            (chatgpt_home / "auth.json").write_text(doci_refresh, encoding="utf-8")

            with mock.patch.object(config, "CODEX_REAL_HOME", real_home):
                llm._sync_refreshed_chatgpt_auth(chatgpt_home)

            self.assertEqual(
                (real_home / "auth.json").read_text(encoding="utf-8"), doci_refresh
            )


class CodexDualProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        llm._chatgpt_auth_snapshot = None
        self.addCleanup(setattr, llm, "_chatgpt_auth_snapshot", None)

    def _completed(self, text: str) -> subprocess.CompletedProcess:
        stdout = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": text},
            }
        )
        return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    def test_ensure_codex_home_uses_chatgpt_home_for_chatgpt_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-codex-home"
            real_home.mkdir()
            (real_home / "auth.json").write_text("{}", encoding="utf-8")
            chatgpt_home = Path(tmp) / "chatgpt-codex-home"
            with (
                mock.patch.object(config, "CODEX_PROVIDER", "chatgpt"),
                mock.patch.object(config, "MINIMAX_API_KEY", ""),
                mock.patch.object(config, "CODEX_REAL_HOME", real_home),
                mock.patch.object(config, "CODEX_CHATGPT_HOME", chatgpt_home),
                mock.patch.object(config, "CODEX_CHATGPT_HOME_LOCK", Path(tmp) / "chatgpt-home.lock"),
                mock.patch.object(config, "CODEX_CHATGPT_AUTH_BACKUP", Path(tmp) / "backup-auth.json"),
            ):
                home = _ensure_codex_home("gpt-5.6-luna")
            self.assertEqual(home, chatgpt_home)

    def test_ensure_codex_home_still_builds_minimax_home_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            with (
                mock.patch.object(config, "CODEX_PROVIDER", "minimax"),
                mock.patch.object(config, "CODEX_HOME", codex_home),
                mock.patch.object(config, "MINIMAX_API_KEY", "secret"),
            ):
                home = _ensure_codex_home("MiniMax-M3")
            self.assertEqual(home, codex_home)
            self.assertTrue((codex_home / "config.toml").exists())

    def test_run_codex_chatgpt_provider_uses_isolated_chatgpt_home_and_no_minimax_env(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-codex-home"
            real_home.mkdir()
            (real_home / "auth.json").write_text("{}", encoding="utf-8")
            chatgpt_home = Path(tmp) / "chatgpt-codex-home"
            with (
                mock.patch.object(config, "CODEX_PROVIDER", "chatgpt"),
                mock.patch.object(config, "CODEX_REASONING_EFFORT", ""),
                mock.patch.object(config, "CODEX_REAL_HOME", real_home),
                mock.patch.object(config, "CODEX_CHATGPT_HOME", chatgpt_home),
                mock.patch.object(config, "CODEX_CHATGPT_HOME_LOCK", Path(tmp) / "chatgpt-home.lock"),
                mock.patch.object(config, "CODEX_CHATGPT_AUTH_BACKUP", Path(tmp) / "backup-auth.json"),
                mock.patch.object(config, "OUTPUT", Path(tmp)),
                mock.patch.object(
                    llm.subprocess, "run", return_value=self._completed("ok")
                ) as run_mock,
            ):
                result = llm.run_codex("prompt", "gpt-5.6-luna", min_web_fetches=0)

        self.assertEqual(result, "ok")
        env = run_mock.call_args.kwargs["env"]
        self.assertEqual(env["CODEX_HOME"], str(chatgpt_home))
        self.assertNotIn("DOCI_MINIMAX_AUTHORIZATION", env)
        cmd = run_mock.call_args.args[0]
        self.assertIn("gpt-5.6-luna", cmd)
        self.assertIn("approval_policy=never", cmd)

    def test_run_codex_chatgpt_provider_does_not_require_minimax_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-codex-home"
            real_home.mkdir()
            (real_home / "auth.json").write_text("{}", encoding="utf-8")
            with (
                mock.patch.object(config, "CODEX_PROVIDER", "chatgpt"),
                mock.patch.object(config, "MINIMAX_API_KEY", ""),
                mock.patch.object(config, "CODEX_REAL_HOME", real_home),
                mock.patch.object(config, "CODEX_CHATGPT_HOME", Path(tmp) / "chatgpt-home"),
                mock.patch.object(config, "CODEX_CHATGPT_HOME_LOCK", Path(tmp) / "chatgpt-home.lock"),
                mock.patch.object(config, "CODEX_CHATGPT_AUTH_BACKUP", Path(tmp) / "backup-auth.json"),
                mock.patch.object(config, "OUTPUT", Path(tmp)),
                mock.patch.object(
                    llm.subprocess, "run", return_value=self._completed("ok")
                ),
            ):
                self.assertEqual(
                    llm.run_codex("prompt", "gpt-5.6-luna", min_web_fetches=0), "ok"
                )

    def test_run_codex_passes_explicit_reasoning_effort_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-codex-home"
            real_home.mkdir()
            (real_home / "auth.json").write_text("{}", encoding="utf-8")
            with (
                mock.patch.object(config, "CODEX_PROVIDER", "chatgpt"),
                mock.patch.object(config, "CODEX_REASONING_EFFORT", "max"),
                mock.patch.object(config, "CODEX_REAL_HOME", real_home),
                mock.patch.object(config, "CODEX_CHATGPT_HOME", Path(tmp) / "chatgpt-home"),
                mock.patch.object(config, "CODEX_CHATGPT_HOME_LOCK", Path(tmp) / "chatgpt-home.lock"),
                mock.patch.object(config, "CODEX_CHATGPT_AUTH_BACKUP", Path(tmp) / "backup-auth.json"),
                mock.patch.object(config, "OUTPUT", Path(tmp)),
                mock.patch.object(
                    llm.subprocess, "run", return_value=self._completed("ok")
                ) as run_mock,
            ):
                llm.run_codex("prompt", "gpt-5.6-luna", min_web_fetches=0)

        cmd = run_mock.call_args.args[0]
        self.assertIn("model_reasoning_effort=max", cmd)

    def test_run_codex_minimax_provider_keeps_isolated_home_and_auth_env(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(config, "CODEX_PROVIDER", "minimax"),
            mock.patch.object(config, "CODEX_REASONING_EFFORT", ""),
            mock.patch.object(config, "CODEX_HOME", Path(tmp) / "codex-home"),
            mock.patch.object(config, "MINIMAX_API_KEY", "secret"),
            mock.patch.object(config, "OUTPUT", Path(tmp)),
            mock.patch.object(
                llm.subprocess, "run", return_value=self._completed("ok")
            ) as run_mock,
        ):
            self.assertEqual(
                llm.run_codex("prompt", "MiniMax-M3", min_web_fetches=0), "ok"
            )

        env = run_mock.call_args.kwargs["env"]
        self.assertEqual(env["CODEX_HOME"], str(Path(tmp) / "codex-home"))
        self.assertEqual(env["DOCI_MINIMAX_AUTHORIZATION"], "Bearer secret")

    def test_run_codex_disables_sandbox_network_when_no_web_fetch_required(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(config, "CODEX_PROVIDER", "minimax"),
            mock.patch.object(config, "CODEX_HOME", Path(tmp) / "codex-home"),
            mock.patch.object(config, "MINIMAX_API_KEY", "secret"),
            mock.patch.object(config, "OUTPUT", Path(tmp)),
            mock.patch.object(
                llm.subprocess, "run", return_value=self._completed("ok")
            ) as run_mock,
        ):
            llm.run_codex("prompt", "MiniMax-M3", min_web_fetches=0)

        cmd = run_mock.call_args.args[0]
        self.assertIn("sandbox_workspace_write.network_access=false", cmd)

    def test_run_codex_enables_sandbox_network_when_web_fetch_required(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(config, "CODEX_PROVIDER", "minimax"),
            mock.patch.object(config, "CODEX_HOME", Path(tmp) / "codex-home"),
            mock.patch.object(config, "MINIMAX_API_KEY", "secret"),
            mock.patch.object(config, "OUTPUT", Path(tmp)),
            mock.patch.object(
                llm.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    [],
                    0,
                    stdout="\n".join(
                        [
                            json.dumps(
                                {
                                    "type": "item.completed",
                                    "item": {
                                        "type": "command_execution",
                                        "command": "curl https://example.com",
                                        "exit_code": 0,
                                    },
                                }
                            ),
                            json.dumps(
                                {
                                    "type": "item.completed",
                                    "item": {"type": "agent_message", "text": "ok"},
                                }
                            ),
                        ]
                    ),
                    stderr="",
                ),
            ) as run_mock,
        ):
            llm.run_codex("prompt", "MiniMax-M3", min_web_fetches=1)

        cmd = run_mock.call_args.args[0]
        self.assertIn("sandbox_workspace_write.network_access=true", cmd)

    def test_run_codex_chatgpt_provider_denies_web_fetch_required_calls_by_default(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-codex-home"
            real_home.mkdir()
            (real_home / "auth.json").write_text("{}", encoding="utf-8")
            with (
                mock.patch.object(config, "CODEX_PROVIDER", "chatgpt"),
                mock.patch.object(config, "CODEX_CHATGPT_ALLOW_UNTRUSTED_WEB", False),
                mock.patch.object(config, "CODEX_REAL_HOME", real_home),
                mock.patch.object(config, "CODEX_CHATGPT_HOME", Path(tmp) / "chatgpt-home"),
                mock.patch.object(config, "CODEX_CHATGPT_HOME_LOCK", Path(tmp) / "chatgpt-home.lock"),
                mock.patch.object(config, "CODEX_CHATGPT_AUTH_BACKUP", Path(tmp) / "backup-auth.json"),
                mock.patch.object(config, "OUTPUT", Path(tmp)),
                mock.patch.object(llm.subprocess, "run") as run_mock,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "CODEX_CHATGPT_ALLOW_UNTRUSTED_WEB"
                ):
                    llm.run_codex("prompt", "gpt-5.6-luna", min_web_fetches=1)

        run_mock.assert_not_called()

    def test_run_codex_chatgpt_provider_allows_web_fetch_with_explicit_opt_in(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-codex-home"
            real_home.mkdir()
            (real_home / "auth.json").write_text("{}", encoding="utf-8")
            with (
                mock.patch.object(config, "CODEX_PROVIDER", "chatgpt"),
                mock.patch.object(config, "CODEX_CHATGPT_ALLOW_UNTRUSTED_WEB", True),
                mock.patch.object(config, "CODEX_REASONING_EFFORT", ""),
                mock.patch.object(config, "CODEX_REAL_HOME", real_home),
                mock.patch.object(config, "CODEX_CHATGPT_HOME", Path(tmp) / "chatgpt-home"),
                mock.patch.object(config, "CODEX_CHATGPT_HOME_LOCK", Path(tmp) / "chatgpt-home.lock"),
                mock.patch.object(config, "CODEX_CHATGPT_AUTH_BACKUP", Path(tmp) / "backup-auth.json"),
                mock.patch.object(config, "OUTPUT", Path(tmp)),
                mock.patch.object(
                    llm.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        [],
                        0,
                        stdout=json.dumps(
                            {
                                "type": "item.completed",
                                "item": {
                                    "type": "command_execution",
                                    "command": "curl https://example.com",
                                    "exit_code": 0,
                                },
                            }
                        )
                        + "\n"
                        + json.dumps(
                            {
                                "type": "item.completed",
                                "item": {"type": "agent_message", "text": "ok"},
                            }
                        ),
                        stderr="",
                    ),
                ),
            ):
                self.assertEqual(
                    llm.run_codex("prompt", "gpt-5.6-luna", min_web_fetches=1), "ok"
                )

    def test_run_codex_syncs_refreshed_auth_back_to_real_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-codex-home"
            real_home.mkdir()
            (real_home / "auth.json").write_text(
                _auth_json(account_id="acct-1", access_token="old"), encoding="utf-8"
            )
            chatgpt_home = Path(tmp) / "chatgpt-codex-home"
            refreshed = _auth_json(account_id="acct-1", access_token="refreshed")

            def fake_run(*_args, **_kwargs):
                # codex execが隔離ホーム内でトークンをリフレッシュした状況を模する。
                (chatgpt_home / "auth.json").write_text(refreshed, encoding="utf-8")
                return self._completed("ok")

            with (
                mock.patch.object(config, "CODEX_PROVIDER", "chatgpt"),
                mock.patch.object(config, "CODEX_REASONING_EFFORT", ""),
                mock.patch.object(config, "CODEX_REAL_HOME", real_home),
                mock.patch.object(config, "CODEX_CHATGPT_HOME", chatgpt_home),
                mock.patch.object(config, "CODEX_CHATGPT_HOME_LOCK", Path(tmp) / "chatgpt-home.lock"),
                mock.patch.object(config, "CODEX_CHATGPT_AUTH_BACKUP", Path(tmp) / "backup-auth.json"),
                mock.patch.object(config, "OUTPUT", Path(tmp)),
                mock.patch.object(llm.subprocess, "run", side_effect=fake_run),
            ):
                llm.run_codex("prompt", "gpt-5.6-luna", min_web_fetches=0)

            self.assertEqual(
                (real_home / "auth.json").read_text(encoding="utf-8"), refreshed
            )

    def test_run_codex_syncs_refreshed_auth_even_when_subprocess_times_out(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-codex-home"
            real_home.mkdir()
            (real_home / "auth.json").write_text(
                _auth_json(account_id="acct-1", access_token="old"), encoding="utf-8"
            )
            chatgpt_home = Path(tmp) / "chatgpt-codex-home"
            refreshed = _auth_json(
                account_id="acct-1", access_token="refreshed-before-timeout"
            )

            def fake_run(*_args, **_kwargs):
                (chatgpt_home / "auth.json").write_text(refreshed, encoding="utf-8")
                raise subprocess.TimeoutExpired(cmd="codex", timeout=1)

            with (
                mock.patch.object(config, "CODEX_PROVIDER", "chatgpt"),
                mock.patch.object(config, "CODEX_REASONING_EFFORT", ""),
                mock.patch.object(config, "CODEX_REAL_HOME", real_home),
                mock.patch.object(config, "CODEX_CHATGPT_HOME", chatgpt_home),
                mock.patch.object(config, "CODEX_CHATGPT_HOME_LOCK", Path(tmp) / "chatgpt-home.lock"),
                mock.patch.object(config, "CODEX_CHATGPT_AUTH_BACKUP", Path(tmp) / "backup-auth.json"),
                mock.patch.object(config, "OUTPUT", Path(tmp)),
                mock.patch.object(llm.subprocess, "run", side_effect=fake_run),
            ):
                with self.assertRaises(subprocess.TimeoutExpired):
                    llm.run_codex("prompt", "gpt-5.6-luna", min_web_fetches=0)

            self.assertEqual(
                (real_home / "auth.json").read_text(encoding="utf-8"), refreshed
            )


class CodexReasoningEffortValidationTest(unittest.TestCase):
    def test_invalid_value_fails_fast(self) -> None:
        with mock.patch.object(config, "CODEX_REASONING_EFFORT", "hgih"):
            with self.assertRaisesRegex(ValueError, "CODEX_REASONING_EFFORT"):
                config.validate_pipeline_backends()

    def test_empty_value_is_allowed(self) -> None:
        with mock.patch.object(config, "CODEX_REASONING_EFFORT", ""):
            config.validate_pipeline_backends()

    def test_documented_values_are_allowed(self) -> None:
        for value in ("low", "medium", "high", "xhigh", "ultra", "max"):
            with self.subTest(value=value):
                with mock.patch.object(config, "CODEX_REASONING_EFFORT", value):
                    config.validate_pipeline_backends()


class ChatgptUntrustedWebLoadTimeValidationTest(unittest.TestCase):
    def test_research_backend_codex_without_opt_in_fails_at_load_time(self) -> None:
        # .env.exampleの一括切替ブロックをそのまま使うとこの組み合わせを踏む。
        # run_codex()実行時ではなく設定読込時に気づけなければならない。
        with (
            mock.patch.object(config, "CODEX_PROVIDER", "chatgpt"),
            mock.patch.object(config, "CODEX_CHATGPT_ALLOW_UNTRUSTED_WEB", False),
            mock.patch.object(config, "RESEARCH_BACKEND", "codex"),
            mock.patch.object(config, "FACTCHECK_BACKEND", "opencode_go"),
        ):
            with self.assertRaisesRegex(
                ValueError, "CODEX_CHATGPT_ALLOW_UNTRUSTED_WEB"
            ):
                config.validate_pipeline_backends()

    def test_factcheck_backend_codex_without_opt_in_fails_at_load_time(self) -> None:
        with (
            mock.patch.object(config, "CODEX_PROVIDER", "chatgpt"),
            mock.patch.object(config, "CODEX_CHATGPT_ALLOW_UNTRUSTED_WEB", False),
            mock.patch.object(config, "RESEARCH_BACKEND", "opencode_go"),
            mock.patch.object(config, "FACTCHECK_BACKEND", "codex"),
        ):
            with self.assertRaisesRegex(
                ValueError, "CODEX_CHATGPT_ALLOW_UNTRUSTED_WEB"
            ):
                config.validate_pipeline_backends()

    def test_passes_with_explicit_opt_in(self) -> None:
        with (
            mock.patch.object(config, "CODEX_PROVIDER", "chatgpt"),
            mock.patch.object(config, "CODEX_CHATGPT_ALLOW_UNTRUSTED_WEB", True),
            mock.patch.object(config, "RESEARCH_BACKEND", "codex"),
            mock.patch.object(config, "FACTCHECK_BACKEND", "codex"),
        ):
            config.validate_pipeline_backends()

    def test_passes_when_provider_is_minimax(self) -> None:
        with (
            mock.patch.object(config, "CODEX_PROVIDER", "minimax"),
            mock.patch.object(config, "CODEX_CHATGPT_ALLOW_UNTRUSTED_WEB", False),
            mock.patch.object(config, "RESEARCH_BACKEND", "codex"),
            mock.patch.object(config, "FACTCHECK_BACKEND", "codex"),
        ):
            config.validate_pipeline_backends()

    def test_passes_when_neither_backend_is_codex(self) -> None:
        with (
            mock.patch.object(config, "CODEX_PROVIDER", "chatgpt"),
            mock.patch.object(config, "CODEX_CHATGPT_ALLOW_UNTRUSTED_WEB", False),
            mock.patch.object(config, "RESEARCH_BACKEND", "opencode_go"),
            mock.patch.object(config, "FACTCHECK_BACKEND", "opencode_go"),
        ):
            config.validate_pipeline_backends()


class ChatgptSecretPermissionsTest(unittest.TestCase):
    def test_copied_auth_and_directories_are_created_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-codex-home"
            real_home.mkdir()
            (real_home / "auth.json").write_text(
                _auth_json(account_id="acct-1", access_token="x"), encoding="utf-8"
            )
            chatgpt_home = Path(tmp) / "chatgpt-codex-home"
            backup = Path(tmp) / "backup-dir" / "auth.json"

            with (
                mock.patch.object(config, "CODEX_REAL_HOME", real_home),
                mock.patch.object(config, "CODEX_CHATGPT_HOME", chatgpt_home),
                mock.patch.object(config, "CODEX_CHATGPT_AUTH_BACKUP", backup),
            ):
                _ensure_chatgpt_codex_home()

            self.assertEqual(oct(chatgpt_home.stat().st_mode)[-3:], "700")
            self.assertEqual(
                oct((chatgpt_home / "auth.json").stat().st_mode)[-3:], "600"
            )
            self.assertEqual(oct(backup.parent.stat().st_mode)[-3:], "700")
            self.assertEqual(oct(backup.stat().st_mode)[-3:], "600")

    def test_minimax_config_toml_and_home_are_created_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            with (
                mock.patch.object(config, "CODEX_HOME", codex_home),
                mock.patch.object(config, "MINIMAX_API_KEY", "secret"),
            ):
                _ensure_codex_home("MiniMax-M3")

            self.assertEqual(oct(codex_home.stat().st_mode)[-3:], "700")
            self.assertEqual(
                oct((codex_home / "config.toml").stat().st_mode)[-3:], "600"
            )


class ChatgptHomeLockTest(unittest.TestCase):
    def test_lock_is_released_after_use_and_reacquirable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "chatgpt-home.lock"
            with mock.patch.object(config, "CODEX_CHATGPT_HOME_LOCK", lock_path):
                with llm._chatgpt_home_lock():
                    pass
                with llm._chatgpt_home_lock():
                    pass

    def test_second_holder_times_out_while_first_holds_the_lock(self) -> None:
        # 複数チャンネルのcronジョブが並行してCODEX_PROVIDER=chatgptを使う状況を
        # 模す。先行側が保持している間、後発側は待ち続けた末にタイムアウトする
        # （固定パスの隔離ホームをrmtreeで壊し合うのを防ぐのが目的）。
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "chatgpt-home.lock"
            with (
                mock.patch.object(config, "CODEX_CHATGPT_HOME_LOCK", lock_path),
                mock.patch.object(llm, "_CHATGPT_HOME_LOCK_TIMEOUT_SECONDS", 0.3),
                mock.patch.object(llm, "_CHATGPT_HOME_LOCK_RETRY_SECONDS", 0.05),
            ):
                with llm._chatgpt_home_lock():
                    with self.assertRaisesRegex(RuntimeError, "隔離ホームlock"):
                        with llm._chatgpt_home_lock():
                            pass


if __name__ == "__main__":
    unittest.main()
