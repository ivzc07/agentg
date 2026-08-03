# AGENTS.md

Entry point for any agent working on this repo. For the full house rules see `CLAUDE.md` and `docs/agents/`.

## Automated review on pull requests

Every PR gets an automated review with comments tagged by severity: `[P1]` (bug / correctness, must fix), `[P2]` (should fix), `[P3]` (optional).
The reviewer is the Pi coding agent (Greptile's trial credits are exhausted; a Greptile credit-limit notice counts as no review).
The review does not trigger itself, and a green check does **not** mean there are no findings - always read the comments.

After opening any PR (and again after pushing substantive fixes): run `scripts/pi-review <N>` from Git Bash.

Before merging any PR:

1. Fetch the comments (with `--paginate` so later pages of findings are not missed): Pi posts PR-level comments (`gh api --paginate repos/ivzc07/agentg/issues/<N>/comments`); older PRs may carry Greptile inline comments (`gh api --paginate repos/ivzc07/agentg/pulls/<N>/comments`).
2. For every P1 and P2: diagnose it against the actual code and confirm it is real, then fix it - do not "fix" a finding without verifying it. If it is wrong or not worth fixing, dismiss it by replying on the comment thread with the reason.
3. Reply on each thread noting what you did (fix commit or dismissal reason) so nothing is silently ignored.

Merge with `gh pr merge <N> --merge --delete-branch` - the repo does **not** delete branches on merge, so omitting the flag leaks a branch every time. Resolving a merge conflict counts as "substantive fixes": re-run `scripts/pi-review <N>` on the result before merging.

When the work is done, clean up: local branches (`git branch -d`), leftover agent worktrees under `~/.herdr/worktrees/`, and `.scratch/` review artifacts. Finish with `git status --porcelain` clean.

Full workflow: `docs/agents/pr-merges.md`.

## The tests gate

Do not merge unless the `pytest` check (`.github/workflows/tests.yml`) has completed successfully; pending, missing, or cancelled also blocks the merge.
`main` enforces this with a branch ruleset requiring `pytest`.
Run tests locally with `uv run pytest`.

## Other conventions

- Issues and PRDs live in GitHub Issues (`gh` CLI): `docs/agents/issue-tracker.md`.
- Triage labels: `docs/agents/triage-labels.md`.
- Domain docs: `CONTEXT.md` and `docs/adr/` at the repo root; see `docs/agents/domain.md`.
