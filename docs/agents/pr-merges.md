# PR merges

How an agent takes a pull request from open to merged in this repo.
Two gates protect `main`: Greptile's review comments and the pytest CI check.

## Before merging

1. Fetch Greptile's inline comments:
   `gh api repos/ivzc07/agentg/pulls/<N>/comments`
2. Read the checks: `gh pr checks <N> --repo ivzc07/agentg`.

## Handling P1/P2 findings

Greptile tags each comment with a severity: `[P1]` (bug or correctness issue, must fix), `[P2]` (should fix), `[P3]` (optional).

For every P1 and P2 finding:

- First diagnose it by reading the actual code and confirming the problem is real.
- Then fix it. Do not accept or "fix" a finding without verifying it.
- If diagnosis shows the finding is wrong or not worth fixing, dismiss it by replying on the comment thread with the reason.

P3 findings are optional; use judgement.

Reply on each comment thread noting what you did (the fix commit, or the dismissal reason), so nothing is silently ignored.

## The tests gate

Do not merge unless the `tests` check (GitHub Actions pytest workflow, `.github/workflows/tests.yml`) has completed successfully.
Pending, missing, or cancelled also blocks the merge.
`main` enforces this with a branch ruleset requiring the `pytest` check, so a red or absent check blocks the merge button as well.

Tests run locally with `uv run pytest`.

## Greptile config

Greptile's behaviour is configured in `.greptile/config.json` (repo root).
It re-reviews on every new commit and instructs Greptile to tag findings with the `[P1]`/`[P2]`/`[P3]` prefixes above.

## Merge

Once findings are handled and checks are green:
`gh pr merge <N> --repo ivzc07/agentg --merge --delete-branch`, then sync local `main`.
