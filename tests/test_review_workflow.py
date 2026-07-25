from __future__ import annotations

import unittest
from pathlib import Path


class ReviewWorkflowSecurityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = (
            Path(__file__).parents[1] / ".github/workflows/claude-review.yml"
        ).read_text(encoding="utf-8")

    def test_model_has_no_shell_file_or_github_write_tools(self) -> None:
        self.assertIn('--allowedTools ""', self.workflow)
        self.assertIn(
            '"Bash,Read,Glob,Grep,Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch',
            self.workflow,
        )
        self.assertNotIn("Bash(gh pr comment:*)", self.workflow)

    def test_uses_structured_output_and_fixed_comment_step(self) -> None:
        self.assertIn("steps.claude.outputs.structured_output", self.workflow)
        self.assertIn("needs.review.outputs.review_json", self.workflow)
        self.assertIn("--body-file /tmp/claude-review.md", self.workflow)
        self.assertIn("unsafe review output", self.workflow)

    def test_uses_subscription_oauth_and_immutable_actions(self) -> None:
        self.assertIn("claude_code_oauth_token:", self.workflow)
        self.assertNotIn("anthropic_api_key:", self.workflow)
        self.assertIn(
            "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
            self.workflow,
        )
        self.assertIn("fetch-depth: 0", self.workflow)
        self.assertIn(
            "anthropics/claude-code-action@44423bdec74b97d67543eb16c110546762c110b2",
            self.workflow,
        )


if __name__ == "__main__":
    unittest.main()
