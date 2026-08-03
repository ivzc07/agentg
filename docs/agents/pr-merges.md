# PR merges

How an agent takes a pull request from open to merged in this repo.
Two gates protect `main`: an automated review and the pytest CI check.

## Triggering the review

The reviewer is the Pi coding agent via `scripts/pi-review`.
The review does not trigger itself: the agent that opens a PR must run `scripts/pi-review <N>` (from Git Bash) right after opening it.
Pi does not re-review on new commits either - run `scripts/pi-review <N>` again after pushing substantive fixes.
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
   The reviewer posts PR-level comments: `gh api --paginate repos/ivzc07/agentg/issues/<N>/comments`.
3. Read the checks: `gh pr checks <N> --repo ivzc07/agentg`.

## Handling P1/P2 findings

Every review comment carries a severity: `[P1]` (bug or correctness issue, must fix), `[P2]` (should fix), `[P3]` (optional).

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

## Merge

Once findings are handled and checks are green:
`gh pr merge <N> --repo ivzc07/agentg --merge --delete-branch`, then sync local `main`.

The repo has `delete_branch_on_merge` **off**, so `--delete-branch` is the only thing that removes the remote branch - dropping it (e.g. plain `gh pr merge <N> --merge`) silently leaks a branch per PR. That is how the repo accumulated its pile of merged `issue-*` refs.

### Merging a batch

Branches that touch the same files conflict with each other, not just with `main`, so a batch is merged **one at a time**: merge, wait for GitHub to recompute mergeability (`gh pr list --json number,mergeable,mergeStateStatus` returns `UNKNOWN` for ~20-40s), then take the next `CLEAN` one. When a PR goes `CONFLICTING`, resolve it on its branch (`resolving-merge-conflicts` skill), push, and **re-run `scripts/pi-review <N>`** - a conflict resolution is new code that has never been reviewed, and in practice this is where the P1s hide. Re-run `uv run pytest` locally before pushing; the CI check is the gate but the local run is faster feedback.

Before merging such a PR, check your own resolution commit with `git show --stat` - a `git add -A` during conflict resolution happily sweeps in local junk (`.scratch/`, stray files). If you find any, strip it and force-push **the PR branch** (`git push --force-with-lease origin <branch>` - it refuses to clobber commits another agent pushed since your last fetch), before the merge.

**Never force-push `main`.** The `main-protection` ruleset only requires the `pytest` check - it carries no non-fast-forward rule, and classic branch protection is off - so a force-push to `main` will succeed and rewrite shared history. Nothing in this workflow ever requires rewriting `main`; fix mistakes with a new commit.

## Clean up when you are done

Merging is not finished until the workspace is back to a clean state. After the last PR in a batch:

1. **Branches.** `--delete-branch` handles the remote side per PR; delete the local ones with `git branch -d <branch>` (`-d`, never `-D`: it refuses anything not fully merged, which is the check you want). Then `git remote prune origin`.
2. **Worktrees.** Agent/swarm tooling (Herdr) leaves worktrees under `~/.herdr/worktrees/agentg/`, often holding stale staged changes on a branch you just merged, and a checked-out branch cannot be deleted. Confirm the content is redundant (`git -C <worktree> diff --cached origin/main --stat` - a stale one *removes* lines `main` already has). Then back up **everything**, not just the index: plain `git worktree remove` refuses a dirty worktree, and `--force` is precisely the flag that overrides that refusal, so it deletes unstaged modifications and untracked files too. Run `git -C <worktree> status --porcelain` first and account for every line; save `git -C <worktree> diff --cached > /tmp/<name>-staged.patch` **and** `git -C <worktree> diff > /tmp/<name>-unstaged.patch`, and copy any untracked files you care about out of the tree. Only then `git worktree remove --force <path>` and `git worktree prune`. Check `git worktree list` before and after.
3. **Local artifacts.** Review runs drop `.scratch/` (repros, per-round review notes) in the repo root. Delete them when the PR is merged. `.scratch/` and `.keytest` are gitignored - they were committed by accident twice before that was added.
4. **Verify.** `git status --porcelain` empty, `gh pr list --state open` as expected, `git log --oneline -1 origin/main` is your merge, and the `tests` run on `main` is green (`gh run list --branch main --limit 1`).

A merged PR whose issue has open siblings does not close the parent spec issue - check whether the parent is now fully satisfied and close it explicitly if so.
