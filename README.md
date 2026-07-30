# groundwork

Agentic UK company underwriting intelligence: resolves a company, gathers evidence via Companies House + web, generates a citation-backed report with explicit per-section confidence.

> **Status: Phase 0 complete.** Scaffolding, the token gate, and the LLM circuit breaker are in place. Report generation lands in Phase 6.1 — `/report` currently returns 501. This README is filled in properly at Phase 6.5, after the Phase 7 audit, so that it describes what was actually built rather than what was planned.

---

## 1. What this is

_TODO (Phase 6.5) — one paragraph, plain language, addressed to the underwriter who would use it._

## 2. What was built

_TODO (Phase 6.5) — feature summary mapped to the brief's required output: business model, competitive landscape, quality signals, uncertainty handling._

## 3. Architecture overview

_TODO (Phase 6.5) — brief, with an ASCII diagram, matching `TECH_STACK.md`._

## 4. Key decisions

_TODO (Phase 6.5) — condensed decisions with their rejected alternatives, so a reader sees the judgement without reading every commit._

## 5. What was deliberately deferred, and why

_TODO (Phase 6.5) — directly answers the brief's "judgement on scope" criterion._

## 6. Setup

```bash
git clone https://github.com/SourabhaKK/groundwork.git
cd groundwork

cp .env.example .env      # then fill in the five required keys
uv sync --dev

uv run pytest             # tests — no live API calls, everything mocked
uv run ruff check .       # lint

uv run uvicorn src.api.main:app --reload    # API on http://127.0.0.1:8000
uv run streamlit run frontend/app.py        # frontend (Phase 6.2)
```

The app validates configuration at startup and **fails immediately** if any of the five keys in `.env.example` is missing. That is deliberate: a missing search key would otherwise surface as a report with no evidence, which is indistinguishable from a company that genuinely has a thin public footprint — the one signal this system must never fake.

## 7. How to use it

_TODO (Phase 6.5) — example input, what the output looks like, and specifically how to trigger the disambiguation and low-confidence behaviours, since those are what an evaluator will most want to test._

### Access token

`/report` is gated. Send the token from the submission email:

```bash
curl -X POST https://<demo-url>/report \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"query": "Monzo Bank Ltd"}'
```

`/health` needs no token and reports operational state (breaker, budget, search-quota status).

### Rate limits, and why they exist

The hosted demo runs on a personal API key and a metered search tier, so `/report` is capped:

| Limit | Value | Reason |
|---|---|---|
| Per client | 5 reports/hour | Comfortably more than testing all four Phase 7.1 scenarios back to back |
| Global | 200 reports total | Sized against the search provider's 1,000-credit free tier (~250–400 runs), not a per-day guess |

Exceeding either returns `429` with an explanatory message rather than a bare failure.

**Known limitation, stated plainly:** both counters live in the API process. They reset if the container is replaced, and if the service ever scaled beyond one instance each would keep its own counters. Deployment pins `--max-instances=2` to bound the second case. The durable ceiling is upstream regardless — the search free tier stops on its own, and provider-side spend limits cap the rest. This limiter is a courteous speed bump, not a budget guarantee, and is documented as such rather than presented as airtight.

## 8. Hosted demo

_TODO (Phase 6.4) — URL._

**Cold starts:** the service runs on Cloud Run with scale-to-zero. A keep-warm ping (Phase 6.4a) holds a container warm, but if it lapses the first request may take an extra 30–60s on top of the normal 30–90s agent run. Disclosed here so it reads as a known characteristic rather than an unnoticed flaw.

---

## Planning documents

`workflows.md` (process) → `PRD.md` (problem) → `TECH_STACK.md` (locked stack + rejected alternatives) → `TASKS.md` (phased plan). `AGENTS.md` / `CLAUDE.md` carry operational context for AI coding agents.
