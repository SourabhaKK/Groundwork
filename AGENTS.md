# AGENTS.md

Repo conventions and hard boundaries — written for coding agents, and equally the
rules the build itself was held to. Kept short on purpose. Architecture and
decision rationale live in `DESIGN_NOTE.md`, `TECH_STACK.md` and `BUILD_LOG.md`;
they are not duplicated here.

## Stack

Python 3.11+, LangGraph, FastAPI, Pydantic v2, Streamlit, pytest. Full rationale in `TECH_STACK.md` — locked, do not substitute without logging the deviation there.

## Commands

```
# Install
uv sync   # or: pip install -e . --break-system-packages

# Run tests
pytest

# Run tests, one module
pytest tests/test_<module>.py -v

# Lint
ruff check .

# Run the API locally
uvicorn src.api.main:app --reload

# Run the frontend locally
streamlit run frontend/app.py
```

## Code Style

- Type hints on all function signatures — no bare `Any` unless genuinely unavoidable.
- Pydantic models for every structured LLM output and every API request/response body. Never parse JSON from an LLM response by hand.
- Async for anything that calls an external API (Companies House, Tavily, LLM providers) — no blocking calls inside async functions.
- One responsibility per module under `src/agent/`, `src/data_sources/`, `src/report/`, `src/api/` — see `TASKS.md` Phase 0.3 for the intended directory shape.

## Testing

- Every new function that calls an external API gets a unit test with the API mocked — no live API calls in the test suite.
- LLM calls are mocked in unit tests; real end-to-end runs (Phase 7.1) are manual, not part of CI.
- A failing test blocks the PR. Don't comment out a test to make CI pass.

## Boundaries — Never Do This

- Never commit `.env`, real API keys, or the demo access token — `.env.example` holds placeholders only, already gitignored.
- Never call an LLM provider directly, bypassing the Phase 0.6 circuit breaker — all LLM calls route through it, including during local testing against real providers.
- Never bypass the token-gate/rate-limit on `/report` "just for testing" — test against it, not around it.
- Never silently swap a tool or library from what's locked in `TECH_STACK.md` — if a change is genuinely needed, log it as a deviation in `workflows.md` first.
- Never fabricate a citation or source URL for an `EvidenceClaim` — if extraction can't find a real source, the claim doesn't get created.

## Before Any Non-Trivial Change

`workflows.md` records the process this project was built under: state the
decision, the reasoning and the rejected alternative before implementing, and
log any deviation from `TECH_STACK.md` rather than substituting silently. The
build log exists because that discipline repeatedly caught things review did
not — several load-bearing assumptions in this codebase were broken by live
testing, not by inspection. Keep to it.

## Commit Conventions

Conventional commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`). One phase's work per commit where practical, matching `TASKS.md`'s phase numbering in the commit body (e.g. `feat: Companies House disambiguation (Phase 1.2)`).
