# doci agent instructions

## Implementation safety

- Preserve unrelated local changes and artifacts. Stage and commit only files that belong to the requested issue.
- Start from the actual Git checkout and reconcile local state with the live GitHub issue before editing.
- Validate external-platform behavior with local mocks or fixtures first. Do not create production GitHub issues or labels, upload videos, or change YouTube privacy as an implementation test without explicit user approval.
- Never save or print tokens, OAuth credentials, API keys, or other secrets.
- Runtime generation must use the OpenCode Go defaults; Claude CLI/API is not a runtime dependency.
  The repository-side Claude Action is review-only and must not be introduced into the production path.

## Admin UI synchronization

`doci/admin/` is a local web UI (added in PR #190) for editing `.env`,
`channels/<id>/channel.toml`, Markdown prompts, and 11 hardcoded prompt string
constants. When changing anything it depends on, update it in the same change,
not as follow-up work:

- Adding/removing/renaming a prompt string constant in `doci/ai_text.py`,
  `doci/factcheck.py`, `doci/research.py`, `doci/plan.py`, or
  `doci/tactic_backfill.py`, or changing its `.format()` call-site kwargs:
  update `doci/admin/code_prompt_registry.py` (a hand-maintained static list by
  design — it does not auto-discover new constants).
- Changing `channel.toml`'s schema in `doci/channel.py`: verify
  `doci/admin/channel_store.py`'s `_summarize()` still surfaces the new fields.
- Adding a new `.env` key with a bounded choice set: register it in
  `doci/admin/env_schema.py`'s `_CHOICES_ATTR_BY_KEY` so it renders as a
  dropdown instead of free text (type harvesting is automatic via AST; choice
  sets are not).
- Changing prompt assembly or template placeholders in `doci/corners.py`:
  update `doci/admin/markdown_store.py`'s `required_tokens`.

Run `tests/test_admin_*.py` after any such change.

## Required validation

- Run focused tests for the changed behavior.
- Run the full suite with the active project environment:
  `python -m unittest discover -s tests -v`.
- Run `python -m compileall -q doci tests` and
  `git diff --check`.

## Required review sequence

1. After implementation and local validation, request an independent Sol review and address actionable findings.
2. Push a ready-for-review pull request so `.github/workflows/claude-review.yml` runs.
3. Use the repository-side `Claude PR Review` Action (no explicit `--model`, uses the Action's default model).
4. Do not substitute a local Claude Code CLI review for the repository-side Action.
5. Inspect the Action result and all required checks, address actionable findings, push fixes, and wait for the Action to review the updated head again before reporting completion.
