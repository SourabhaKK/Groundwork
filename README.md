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

_TODO (Phase 6.5) — directly answers the brief's "judgement on scope" criterion. Notes below are seeded as each phase produces them, so this section is written from a record rather than from memory._

**Rebrands and trading names (Phase 1.3).** These are two different things, and the system handles one of them. Both statements below were tested against the live Companies House API, not reasoned about.

**Former registered legal names — supported.** If a company changed its registered name, searching the old one finds it. Companies House indexes former names and flags them in the search response, and we confirm the match against the company's `previous_company_names` before surfacing it. Verified end to end: searching **"Focus FS"** returns **Monzo Bank Limited** (09446231, registered as FOCUS FS LIMITED from 2015-02-18 until 2016-10-21). The result is always presented for confirmation rather than resolved silently — a former name is evidence of identity, not proof of it.

**Trading and brand names that were never registered — not supported, and not fixable here.** If a name was only ever a brand, the register holds no record of it, so no matching logic can reach it. The tested example is the one most people would expect to work: **"Mondo" was Monzo's original brand**, dropped after a trademark dispute — but it was never the company's registered name (that went FOCUS FS LIMITED → MONZO BANK LIMITED). Searching "Mondo" returns **zero results for Monzo Bank Limited at any rank**, confirmed by sweeping the complete result set, not just the first page. The same applies to any brand-only name; Facebook → Meta fails identically.

The distinction matters because the two look the same to a user and have completely different causes. The first was an implementation question and is solved. The second is an absence of data at the source. We report "not found" honestly rather than guessing — an LLM asked to map a trading name to a legal entity was rejected as a fabrication surface at the one step where the register gives us ground truth, and a hand-maintained trading-name table was rejected as unbounded maintenance for arbitrary coverage.

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
| Global | 130 reports total | Worked back from the search provider's 1,000-credit monthly free tier at the real per-report cost — 5 credits (one basic search plus two deeper ones), 6 with a follow-up search — reserving 200 credits for development and manual end-to-end runs |

Exceeding either returns `429` with an explanatory message rather than a bare failure.

**Known limitation, stated plainly:** both counters live in the API process. They reset if the container is replaced, and if the service ever scaled beyond one instance each would keep its own counters. Deployment pins `--max-instances=2` to bound the second case. The durable ceiling is upstream regardless — the search free tier stops on its own, and provider-side spend limits cap the rest. This limiter is a courteous speed bump, not a budget guarantee, and is documented as such rather than presented as airtight.

## 8. Hosted demo

_TODO (Phase 6.4) — URL._

**Cold starts:** the service runs on Cloud Run with scale-to-zero. A keep-warm ping (Phase 6.4a) holds a container warm, but if it lapses the first request may take an extra 30–60s on top of the normal 30–90s agent run. Disclosed here so it reads as a known characteristic rather than an unnoticed flaw.

---

## Planning documents

`workflows.md` (process) → `PRD.md` (problem) → `TECH_STACK.md` (locked stack + rejected alternatives) → `TASKS.md` (phased plan). `AGENTS.md` / `CLAUDE.md` carry operational context for AI coding agents.
