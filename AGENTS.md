# AGENTS.md

Entry point for any agent working on this repo. For the full house rules see `CLAUDE.md` and `docs/agents/`.

## Handling Greptile on pull requests

Greptile reviews every PR and leaves inline comments tagged by severity: `[P1]` (bug / correctness, must fix), `[P2]` (should fix), `[P3]` (optional).
The green "Greptile Review" check does **not** mean there are no findings, so never merge on the check colour alone - always read the comments.

Before merging any PR:

1. Fetch the comments (with `--paginate` so later pages of findings are not missed): `gh api --paginate repos/ivzc07/agentg/pulls/<N>/comments`
2. For every P1 and P2: diagnose it against the actual code and confirm it is real, then fix it - do not "fix" a finding without verifying it. If it is wrong or not worth fixing, dismiss it by replying on the comment thread with the reason.
3. Reply on each thread noting what you did (fix commit or dismissal reason) so nothing is silently ignored.

Greptile's behaviour is configured in `.greptile/config.json` at the repo root.

Full workflow: `docs/agents/pr-merges.md`.

## The tests gate

Do not merge unless the `pytest` check (`.github/workflows/tests.yml`) has completed successfully; pending, missing, or cancelled also blocks the merge.
`main` enforces this with a branch ruleset requiring `pytest`.
Run tests locally with `uv run pytest`.

## Other conventions

- Issues and PRDs live in GitHub Issues (`gh` CLI): `docs/agents/issue-tracker.md`.
- Triage labels: `docs/agents/triage-labels.md`.
- Domain docs: `CONTEXT.md` and `docs/adr/` at the repo root; see `docs/agents/domain.md`.
