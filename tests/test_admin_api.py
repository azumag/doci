"""doci.admin.api.dispatch() のテスト。ソケットを使わず純粋関数として叩く。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from doci import channel, config
from doci.admin import api, safeio
from tests.admin_test_helpers import write_channel, write_minimal_repo


class DispatchRoutingTest(unittest.TestCase):
    """ルーティング自体のテストは実リポジトリに対して読み取り専用で行う。"""

    def test_unknown_route_is_404(self) -> None:
        status, payload = api.dispatch("GET", "/api/nope", None)
        self.assertEqual(status, 404)

    def test_root_without_api_prefix_is_404(self) -> None:
        status, payload = api.dispatch("GET", "/", None)
        self.assertEqual(status, 404)

    def test_status_ok(self) -> None:
        status, payload = api.dispatch("GET", "/api/status", None)
        self.assertEqual(status, 200)
        self.assertIn("channels", payload)

    def test_env_list_never_exposes_secret_values(self) -> None:
        status, payload = api.dispatch("GET", "/api/env", None)
        self.assertEqual(status, 200)
        for entry in payload["entries"]:
            if entry["is_secret"]:
                self.assertIsNone(entry["value"])

    def test_unknown_channel_toml_is_404(self) -> None:
        status, payload = api.dispatch("GET", "/api/channels/does-not-exist/toml", None)
        self.assertEqual(status, 404)

    def test_backups_requires_colon_target(self) -> None:
        status, payload = api.dispatch("GET", "/api/backups?target=bad", {})
        self.assertEqual(status, 400)

    def test_get_method_not_allowed_on_validate_endpoint(self) -> None:
        status, payload = api.dispatch("GET", "/api/env/validate", None)
        self.assertEqual(status, 404)  # ルート未定義(GETはvalidateに存在しない)

    def test_status_is_json_serializable_when_pipeline_running(self) -> None:
        # PosixPathをそのままpayloadに入れるとjson.dumpsがTypeErrorで落ち、
        # cron稼働中バナーの根拠となる/api/status自体が丸ごと壊れていた
        # （実際にRemoteDisconnectedになることをHTTP層で確認済み）。
        fake_run = safeio.RunningRun(run_name="default", pid=1, lock_path=Path("/tmp/x.lock"))
        with mock.patch("doci.admin.api.safeio.pipeline_running", return_value=[fake_run]):
            status, payload = api.dispatch("GET", "/api/status", None)
        self.assertEqual(status, 200)
        json.dumps(payload)  # 例外を投げなければOK
        self.assertEqual(payload["pipeline_running"][0]["lock_path"], "/tmp/x.lock")

    def test_backups_list_rejects_unknown_surface(self) -> None:
        status, payload = api.dispatch("GET", "/api/backups?target=/etc:passwd", None)
        self.assertEqual(status, 400)

    def test_backups_restore_rejects_unknown_surface(self) -> None:
        status, payload = api.dispatch(
            "POST", "/api/backups/restore", {"target": "/etc:passwd", "timestamp": "x"}
        )
        self.assertEqual(status, 400)



class DispatchIsolatedTest(unittest.TestCase):
    """書き込み系はconfig.ROOT/OUTPUT/PROMPTSを一時ディレクトリへ差し替えて行う。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        write_minimal_repo(self.root)
        write_channel(self.root, "testch")
        (self.root / ".env").write_text("TEXT_BACKEND=codex\n", encoding="utf-8")
        for attr, value in (
            ("ROOT", self.root),
            ("PROMPTS", self.root / "doci" / "prompts"),
            ("OUTPUT", self.root / "output"),
        ):
            p = mock.patch.object(config, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def test_channel_save_round_trip_through_dispatch(self) -> None:
        status, payload = api.dispatch("GET", "/api/channels/testch/toml", None)
        self.assertEqual(status, 200)
        text = payload["text"]
        changed = text.replace('name = "テストチャンネル"', 'name = "改名"', 1)
        status, save_payload = api.dispatch(
            "POST",
            "/api/channels/testch/save",
            {"text": changed, "confirm_warnings": True, "base_fingerprint": payload["fingerprint"]},
        )
        self.assertEqual(status, 200, save_payload)
        self.assertEqual(save_payload["summary"]["name"], "改名")

    def test_channel_save_stale_fingerprint_via_dispatch(self) -> None:
        status, payload = api.dispatch("GET", "/api/channels/testch/toml", None)
        status, save_payload = api.dispatch(
            "POST",
            "/api/channels/testch/save",
            {"text": payload["text"], "confirm_warnings": True, "base_fingerprint": "deadbeef"},
        )
        self.assertEqual(status, 409)

    def test_prompt_slot_not_found_via_dispatch_is_404(self) -> None:
        status, payload = api.dispatch("GET", "/api/prompts/testch:corner:nope", None)
        self.assertEqual(status, 404)

    def test_channel_voices_for_broken_channel_returns_400_not_500(self) -> None:
        # /voices は channel.load() を直接呼ぶ単一チャンネル向けエンドポイントで、
        # channel.ChannelConfigError を dispatch() が拾わないと汎用500まで
        # 素通りしていた。
        broken_dir = self.root / "channels" / "broken"
        broken_dir.mkdir()
        (broken_dir / "channel.toml").write_text("not = valid = toml = at = all\n", encoding="utf-8")
        status, payload = api.dispatch("GET", "/api/channels/broken/voices", None)
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_broken_channel_does_not_break_channels_or_prompts_list(self) -> None:
        # 壊れたchannel.tomlを持つ2つ目のチャンネルを追加する。
        broken_dir = self.root / "channels" / "broken"
        broken_dir.mkdir()
        (broken_dir / "channel.toml").write_text("not = valid = toml = at = all\n", encoding="utf-8")

        status, payload = api.dispatch("GET", "/api/channels", None)
        self.assertEqual(status, 200)
        by_id = {c["id"]: c for c in payload["channels"]}
        self.assertIn("testch", by_id)
        self.assertNotIn("error", by_id["testch"])
        self.assertIn("broken", by_id)
        self.assertIn("error", by_id["broken"])

        status, prompts_payload = api.dispatch("GET", "/api/prompts", None)
        self.assertEqual(status, 200)
        slots = {p["slot"] for p in prompts_payload["prompts"]}
        self.assertTrue(any(s.startswith("testch:") for s in slots))
        self.assertFalse(any(s.startswith("broken:") for s in slots))

    def test_env_save_via_dispatch(self) -> None:
        status, payload = api.dispatch(
            "POST", "/api/env/save", {"changes": {"TEXT_BACKEND": "opencode_go"}}
        )
        self.assertEqual(status, 200, payload)
        self.assertIn("TEXT_BACKEND=opencode_go", (self.root / ".env").read_text(encoding="utf-8"))

    def test_backup_restore_round_trip_via_dispatch(self) -> None:
        status, payload = api.dispatch("GET", "/api/channels/testch/toml", None)
        original = payload["text"]
        changed = original.replace('name = "テストチャンネル"', 'name = "改名"', 1)
        api.dispatch(
            "POST",
            "/api/channels/testch/save",
            {"text": changed, "confirm_warnings": True, "base_fingerprint": payload["fingerprint"]},
        )
        status, backups_payload = api.dispatch("GET", "/api/backups?target=channel:testch", None)
        self.assertEqual(status, 200)
        self.assertEqual(len(backups_payload["backups"]), 1)
        timestamp = backups_payload["backups"][0]["timestamp"]

        status, restore_payload = api.dispatch(
            "POST", "/api/backups/restore", {"target": "channel:testch", "timestamp": timestamp}
        )
        self.assertEqual(status, 200, restore_payload)
        status, payload_after = api.dispatch("GET", "/api/channels/testch/toml", None)
        self.assertEqual(payload_after["text"], original)

    def test_env_backup_restore_round_trip_via_dispatch(self) -> None:
        original = (self.root / ".env").read_text(encoding="utf-8")
        api.dispatch("POST", "/api/env/save", {"changes": {"TEXT_BACKEND": "opencode_go"}})
        self.assertNotEqual((self.root / ".env").read_text(encoding="utf-8"), original)

        status, backups_payload = api.dispatch("GET", "/api/backups?target=env:env", None)
        self.assertEqual(status, 200)
        self.assertEqual(len(backups_payload["backups"]), 1)
        timestamp = backups_payload["backups"][0]["timestamp"]

        status, restore_payload = api.dispatch(
            "POST", "/api/backups/restore", {"target": "env:env", "timestamp": timestamp}
        )
        self.assertEqual(status, 200, restore_payload)
        self.assertEqual((self.root / ".env").read_text(encoding="utf-8"), original)

    def test_code_prompt_backup_restore_round_trip_via_dispatch(self) -> None:
        # code-promptsの --enable-code-prompts ゲートは server.py 側の責務なので
        # dispatch()自体は常に到達可能(server.pyのゲートは別途テスト済み)。
        const_id = "ai_text:_SEMANTIC_DUPLICATE_PROMPT"
        (self.root / "doci" / "ai_text.py").write_text(
            '_SEMANTIC_DUPLICATE_PROMPT = """\\\n'
            "candidate: {candidate}\nnumbered: {numbered}\n"
            '"""\n',
            encoding="utf-8",
        )
        status, payload = api.dispatch("GET", f"/api/code-prompts/{const_id}", None)
        self.assertEqual(status, 200, payload)
        original = payload["text"]

        status, save_payload = api.dispatch(
            "POST",
            f"/api/code-prompts/{const_id}/save",
            {
                "text": original.replace("candidate:", "候補:"),
                "confirm_warnings": True,
                "base_fingerprint": payload["fingerprint"],
                "run_guarded_tests": False,
            },
        )
        self.assertEqual(status, 200, save_payload)

        status, backups_payload = api.dispatch(
            "GET", f"/api/backups?target=code_prompt:{const_id}", None
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(backups_payload["backups"]), 1)
        timestamp = backups_payload["backups"][0]["timestamp"]

        status, restore_payload = api.dispatch(
            "POST", "/api/backups/restore", {"target": f"code_prompt:{const_id}", "timestamp": timestamp}
        )
        self.assertEqual(status, 200, restore_payload)

        status, payload_after = api.dispatch("GET", f"/api/code-prompts/{const_id}", None)
        self.assertEqual(payload_after["text"], original)


if __name__ == "__main__":
    unittest.main()
