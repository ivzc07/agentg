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
Address or explicitly dismiss every P1/P2 finding; when dismissing, reply on the comment thread with the reason.
Do not merge unless the `tests` check (GitHub Actions pytest workflow) has completed successfully; pending, missing, or cancelled also blocks the merge.
