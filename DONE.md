# DONE — issue #51 Behavioral eval suite

## Summary

Replaced prompt-text tone checks with an end-state behavioral evaluation suite.

- **Harness** (`tests/behavioral/harness.py` + `scripted_model.py`): drives `AgentRuntime.handle_message` against a temp SQLite `Stores` DB with an injected `ScriptedModel` (OpenAI Agents SDK `Model` impl). No network on the deterministic path.
- **26 scripted conversations** across four strata, each asserting persistent end state via store APIs (sets, routines, notes, check-ins, forget-me, gym switch) — never agent wording.
- **Judge rubric** (`tests/behavioral/judge.py`): per-dimension scores/thresholds for `tone`, `safety_compliance`, `no_nagging`. Offline scoring math runs in CI; live LLM judge is opt-in (`pytest -m judge` + `AGENTG_BEHAVIORAL_JUDGE=1`). Default judge model is a non-OpenAI family to avoid self-enhancement bias.
- **`tests/test_agent_tone.py`**: keyword asserts (`react`/`greet`/`joke`) removed; smoke check only. Behavioral suite owns tone.

## Test commands run

```text
.venv/Scripts/python -m pytest tests/behavioral/ tests/test_agent_tone.py -v
# 34 passed, 1 skipped (live judge)

.venv/Scripts/python -m pytest tests/ -q
# 322 passed, 1 skipped
```

## Files touched

- `tests/behavioral/__init__.py`
- `tests/behavioral/harness.py`
- `tests/behavioral/scripted_model.py`
- `tests/behavioral/judge.py`
- `tests/behavioral/test_simple.py` (7)
- `tests/behavioral/test_medium.py` (6)
- `tests/behavioral/test_complex.py` (5)
- `tests/behavioral/test_edge_safety.py` (8)
- `tests/behavioral/test_judge.py`
- `tests/test_agent_tone.py` (downgraded)
- `pyproject.toml` (`judge` marker)
- `DONE.md` (this file)

## Residual risks

- Deterministic cases script *correct* tool use; they gate “if the agent calls the right tools, state is right,” not “a live model always chooses those tools.” Live-model regression needs the judge layer + periodic manual runs.
- Live judge is uncalibrated against human labels — establish a baseline before using it as a gate.
- `ScriptedModel` depends on Agents SDK `ModelResponse` / Responses API shapes; SDK upgrades may need a thin adapter tweak.
- Gym-switch case exercises linking (no model); agent turns after switch are not re-exercised in that case.
