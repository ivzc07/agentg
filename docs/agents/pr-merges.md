# PR merges

How an agent takes a pull request from open to merged in this repo.
Two gates protect `main`: an automated review (Pi, formerly Greptile) and the pytest CI check.

## Triggering the review

Greptile's trial credits are exhausted, so the working reviewer is the Pi coding agent.
The review does not trigger itself: the agent that opens a PR must run `scripts/pi-review <N>` (from Git Bash) right after opening it.
Unlike Greptile, Pi does not re-review on new commits either - run `scripts/pi-review <N>` again after pushing substantive fixes.
The script runs the multi-agent review workflow and posts `[P1]`/`[P2]`/`[P3]` comments on the PR, signed `- pi code-review`.
Inside Herdr it runs in a visible Pi pane named `pi-review` (created on first use, serialized across callers); outside Herdr it runs headless.

### Windows environment traps

Three traps have broken this gate before. The first two are fixed in-repo; the third is not ours to fix.

- **CRLF line endings.** Git for Windows sets `core.autocrlf=true` in its *system* gitconfig, so every clone and every `git worktree add` used to check the script out with CRLF, breaking its shebang under non-MSYS shells. `.gitattributes` now pins `*.sh` and, **per file**, `scripts/pi-review` to `eol=lf`, so a fresh worktree is runnable with no manual CRLF-stripping. The pin is deliberately not a directory glob (`scripts/*` would force `text` onto any binary dropped in there, and would still miss subdirectories since `*` does not cross `/`), so **a new extensionless script is not covered until you add it to `.gitattributes` explicitly**. `tests/test_pi_review_script.py` guards the existing entries - do not delete them.
- **`bash` resolving to WSL.** From a PowerShell pane, `C:\Windows\System32\bash.exe` (WSL) can precede Git Bash on the PATH. WSL cannot follow a linked worktree's `.git` file, which holds a Windows path, so git fails with a misleading `fatal: not a git repository: /mnt/c/...`. The script now detects WSL and tells you to re-run under Git Bash. Always invoke it through the `bash.exe` inside your Git for Windows install - typically `"C:\Program Files\Git\bin\bash.exe" scripts/pi-review <N>`, or under `%LOCALAPPDATA%\Programs\Git` for a per-user install (`where.exe bash` lists the candidates).
- **`EPERM` renaming `state.json` (upstream bug, still unfixed).** The review workflow persists run state via tmp-write + `renameSync`. On Windows a rename onto a file another process holds open fails with `EPERM`; the run panel re-reads run files about every 300ms while state is saved on a 400ms throttle, so two reviews running at once collide and one dies mid-run. This is not this repo's code, and a reinstall of the offending package reintroduces it. **Two** packages carry the same unguarded rename, so check both: `pi-extensible-workflows` (`src/persistence.ts` - this is the one observed killing a run in practice, on the async path) and `@quintinshaw/pi-dynamic-workflows` (`dist/fs-persistence.js`, in 3.4.1 and 3.5.0 alike, so upgrading does not help). Mitigations: **run one review at a time**, and if a review dies with `EPERM`, wrap that `renameSync` in a bounded retry on `EPERM`/`EACCES`/`EBUSY` (a few ms is enough, the contending handle is short-lived) or re-run the review. If the workflow still cannot complete, run the review directly and post the findings with `gh pr comment`, signed `- pi code-review`, so the gate is genuinely satisfied.

## Before merging

1. Confirm the review actually ran: the PR must carry at least one comment signed `- pi code-review` (there is no status check for it; a missing review blocks the merge exactly like a red pytest).
2. Fetch the review comments.
   Pi posts PR-level comments: `gh api --paginate repos/ivzc07/agentg/issues/<N>/comments`.
   Older PRs may also carry Greptile inline comments: `gh api --paginate repos/ivzc07/agentg/pulls/<N>/comments`.
3. Read the checks: `gh pr checks <N> --repo ivzc07/agentg`.

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
