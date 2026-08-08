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

    def test_uses_structured_output_and_fixed_pr_review_step(self) -> None:
        self.assertIn("steps.claude.outputs.structured_output", self.workflow)
        self.assertIn("needs.review.outputs.review_json", self.workflow)
        self.assertIn("Submit one controlled PR review for this head", self.workflow)
        self.assertIn('"event": "COMMENT"', self.workflow)
        self.assertIn('"commit_id": os.environ["PR_HEAD_SHA"]', self.workflow)
        self.assertIn("PR_HEAD_SHA: ${{ github.event.pull_request.head.sha }}", self.workflow)
        self.assertIn('"repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/reviews"', self.workflow)
        self.assertNotIn("gh pr comment", self.workflow)
        self.assertIn(
            "prompt_file: /tmp/doci-claude-review-prompt.md",
            self.workflow,
        )
        self.assertIn("--unified=5", self.workflow)
        self.assertIn('MAX_REVIEW_DIFF_BYTES: "250000"', self.workflow)
        self.assertIn("the Action never truncates a review", self.workflow)
        self.assertNotIn("head -c", self.workflow)
        self.assertIn("cat /tmp/doci-pr.diff", self.workflow)
        # モデルは明示指定せず Action のデフォルトを使う (PR #156 で撤廃)。
        self.assertNotIn("--model ", self.workflow)
        self.assertIn("--max-turns 4", self.workflow)
        self.assertIn("unsafe review output", self.workflow)
        self.assertIn("Require structured review result", self.workflow)
        self.assertIn(
            "success() && env.CLAUDE_CODE_OAUTH_TOKEN_CONFIGURED",
            self.workflow,
        )
        self.assertIn("Claude review returned no structured output", self.workflow)

    def test_uses_subscription_oauth_and_immutable_actions(self) -> None:
        self.assertIn("claude_code_oauth_token:", self.workflow)
        self.assertNotIn("anthropic_api_key:", self.workflow)
        self.assertIn(
            "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
            self.workflow,
        )
        self.assertIn("fetch-depth: 0", self.workflow)
        self.assertIn(
            "anthropics/claude-code-action/base-action@be7b93b1907a4abad570368f3c74b6fe3807510b",
            self.workflow,
        )


if __name__ == "__main__":
    unittest.main()
