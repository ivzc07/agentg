# agentg

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues (ivzc07/agentg) via the `gh` CLI; external PRs are not a triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

### PR merges

Before merging any PR, fetch Greptile's inline comments: `gh api repos/ivzc07/agentg/pulls/<N>/comments`.
For every P1/P2 finding: first diagnose it by reading the actual code and confirming the problem is real, then fix it; do not accept or "fix" a finding without verifying it.
If diagnosis shows the finding is wrong or not worth fixing, dismiss it by replying on the comment thread with the reason.
Do not merge unless the `tests` check (GitHub Actions pytest workflow) has completed successfully; pending, missing, or cancelled also blocks the merge.
