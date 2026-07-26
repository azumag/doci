# doci agent instructions

## Implementation safety

- Preserve unrelated local changes and artifacts. Stage and commit only files that belong to the requested issue.
- Start from the actual Git checkout and reconcile local state with the live GitHub issue before editing.
- Validate external-platform behavior with local mocks or fixtures first. Do not create production GitHub issues or labels, upload videos, or change YouTube privacy as an implementation test without explicit user approval.
- Never save or print tokens, OAuth credentials, API keys, or other secrets.

## Required validation

- Run focused tests for the changed behavior.
- Run the full suite with the project environment:
  `/Users/azumag/work/doci/.venv/bin/python -m unittest discover -s tests -v`.
- Run `/Users/azumag/work/doci/.venv/bin/python -m compileall -q doci tests` and
  `git diff --check`.

## Required review sequence

1. After implementation and local validation, request an independent Sol review and address actionable findings.
2. Push a ready-for-review pull request so `.github/workflows/claude-review.yml` runs.
3. Use the repository-side `Claude PR Review` Action with the explicit model `claude-opus-5`.
4. Do not substitute a local Claude Code CLI review for the repository-side Action.
5. Inspect the Action result and all required checks, address actionable findings, push fixes, and wait for the Action to review the updated head again before reporting completion.
