# agentg

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues (ivzc07/agentg) via the `gh` CLI; external PRs are not a triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

### PR merges

After opening any PR, trigger the automated review with `scripts/pi-review <N>` (Greptile is out of credits; Pi is the reviewer, and it must be re-run after substantive fix pushes).
Before merging, handle the review's P1/P2 findings (diagnose against the code, then fix or dismiss with a reason) and require the pytest check to pass. See `docs/agents/pr-merges.md`.
