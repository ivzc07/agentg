# PR merges

How an agent takes a pull request from open to merged in this repo.
Two gates protect `main`: an automated review (Pi, formerly Greptile) and the pytest CI check.

## Triggering the review

Greptile's trial credits are exhausted, so the working reviewer is the Pi coding agent.
The review does not trigger itself: the agent that opens a PR must run `scripts/pi-review <N>` (from Git Bash) right after opening it.
Unlike Greptile, Pi does not re-review on new commits either - run `scripts/pi-review <N>` again after pushing substantive fixes.
The script runs a headless multi-agent review workflow and posts `[P1]`/`[P2]`/`[P3]` comments on the PR, signed `- pi code-review`.

## Before merging

1. Fetch the review comments.
   Pi posts PR-level comments: `gh api --paginate repos/ivzc07/agentg/issues/<N>/comments`.
   Older PRs may also carry Greptile inline comments: `gh api --paginate repos/ivzc07/agentg/pulls/<N>/comments`.
2. Read the checks: `gh pr checks <N> --repo ivzc07/agentg`.

## Handling P1/P2 findings

Every review comment carries a severity: `[P1]` (bug or correctness issue, must fix), `[P2]` (should fix), `[P3]` (optional).
The handling below applies the same whether Pi or Greptile posted the finding.

For every P1 and P2 finding:

- First diagnose it by reading the actual code and confirming the problem is real.
- Then fix it. Do not accept or "fix" a finding without verifying it.
- If diagnosis shows the finding is wrong or not worth fixing, dismiss it by replying on the comment thread with the reason.

P3 findings are optional; use judgement.

Reply on each comment thread noting what you did (the fix commit, or the dismissal reason), so nothing is silently ignored.

## The tests gate

Do not merge unless the `pytest` check (the job in `.github/workflows/tests.yml`; the workflow's display name is "tests" but the status check is named after the job, `pytest`) has completed successfully.
Pending, missing, or cancelled also blocks the merge.
`main` enforces this with a branch ruleset requiring the `pytest` check, so a red or absent check blocks the merge button as well.

Tests run locally with `uv run pytest`.

## Greptile config

Greptile's behaviour is configured in `.greptile/config.json` (repo root).
It re-reviews on every new commit and instructs Greptile to tag findings with the `[P1]`/`[P2]`/`[P3]` prefixes above.
This only applies while the account has review credits; without them Greptile posts a credit-limit notice instead of a review, which counts as no review - the Pi review above is the gate.

## Merge

Once findings are handled and checks are green:
`gh pr merge <N> --repo ivzc07/agentg --merge --delete-branch`, then sync local `main`.
