from __future__ import annotations

import unittest
from unittest import mock

from doci import gh_cli


class GhCliTest(unittest.TestCase):
    def test_redact_removes_github_token_shapes(self) -> None:
        value = "failed with ghp_abcdefghijklmnopqrstuvwxyz123456"
        self.assertNotIn("ghp_", gh_cli.redact(value))

    def test_run_gh_raises_with_redacted_detail_on_failure(self) -> None:
        proc = mock.Mock(
            returncode=1,
            stdout="",
            stderr="failed with ghp_abcdefghijklmnopqrstuvwxyz123456",
        )
        with mock.patch.object(gh_cli.subprocess, "run", return_value=proc):
            with self.assertRaisesRegex(RuntimeError, "GitHub操作に失敗しました") as raised:
                gh_cli.run_gh(["issue", "view", "1"])
        self.assertNotIn("ghp_", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
