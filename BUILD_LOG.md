# Build Log

Append one entry per completed phase, in order. Never edit a past entry to reflect later learning — if something from an earlier phase turns out to be wrong or incomplete, add a note in the CURRENT phase's entry pointing back to it, so the log stays an honest record of what was known at the time, not a retroactively cleaned-up story.

This file records what *actually happened* during implementation. `PRD.md`, `TECH_STACK.md`, `TASKS.md` and `workflows.md` record what was planned and why. When the two disagree, this file is the record of reality.

---

## Phase 0 — Project Setup & Core Infrastructure

**Status:** complete. 0.1–0.8 all done; 0.7 mock signed off 30 July 2026. Commits `c1d04e3`, `46db562`, `4f7d79a`, `4a952c4`, `5a33512`, `6811c8a`.

**Built:** A runnable, gated FastAPI service with nothing behind the gate yet — `/report` returns 501, but it is already bearer-token protected and rate limited, and `/health` is open for the Phase 6.4a keep-warm ping. Every LLM call in the codebase routes through one `LLMRouter` + circuit breaker (`src/llm/`), config fails fast at startup on any missing key, CI runs lint and tests with no secrets in the job, and the Phase 0.7 Monzo mock exists as the signed-off shape target.

**Deviations from TASKS.md/TECH_STACK.md:**

1. **New directory `src/llm/`**, not in the Phase 0.3 list. The router and breaker are used by both `src/agent/` (extraction) and `src/report/` (synthesis); putting them in either would make one import the other's internals. Logged in `workflows.md`. No tool substitution — a directory addition only.
2. **Rate limiting hand-rolled (~60 lines) rather than `slowapi`.** `slowapi` has no global-budget concept, so half of it would have been hand-written anyway alongside a dependency whose semantics we don't control. `TECH_STACK.md` specifies the approach, not a library, so this is not a locked-decision deviation. Logged in `workflows.md`.
3. **Rate-limit numbers changed before any code was written.** Originally proposed 50 reports/day; that allows ~1,500/month against a Tavily free tier supporting ~250–400. Now **200 total** across the review window, per-IP unchanged at 5/hour.
4. **Phase 0.5 gained a search-quota signal on `/health`** (`src/data_sources/search_quota.py`) — a scope addition, not a substitution. Reasoning under "surprises" below.
5. **Branch strategy not followed for this phase.** The stated plan was branch-per-phase then squash-merge; a PowerShell quoting failure on `git commit -F -` meant Phase 0 was committed directly to `main`. Resulting history is identical (one commit for the phase), so it was not redone. Phase 1 onward uses the branch.

**Real-world surprises:**

- **Silent search-tier exhaustion is the project's worst failure mode, and it mimics the product's own feature.** When Tavily's credits run out nothing crashes — every later report renders with no web evidence, LOW confidence throughout, and almost no citations. That is indistinguishable from the system working correctly on a thinly-documented company. This was not anticipated in the plan and is why `/health` now reports observed search status.
- **Tavily exposes no dependable credits-remaining endpoint.** The quota tracker therefore records what the last real search *did* rather than polling a balance. Do not go looking for that endpoint again.
- **Several sources 403 `WebFetch`:** Trustpilot, Google Play, Money To The Masses, FinTech Magazine, paymentexpert. This is a live constraint on Phase 2's gathering layer, not a one-off — Tavily's content extraction may or may not fare better, and that is worth testing early rather than discovering at Phase 7.
- **A company's own figures can be a full year ahead of the Companies House register.** Monzo published FY2026 results in May 2026; the register's most recent *filed* accounts were FY2025, with FY2026 not due until 31 December 2026. Any claim sourced to an annual report may have no registry counterpart yet. This is what produced the `official_registry` / `official_self` split.
- **Ruff needed two adjustments:** `B008` flags FastAPI's `Depends()`-in-default idiom, so `src/api/*.py` carries a per-file ignore; `UP042` pushed `str, Enum` → `StrEnum` (fine on 3.11+).
- **Local Python is 3.12, CI targets 3.11.** Nothing has depended on the difference yet.
- **`starlette.testclient` emits a deprecation warning** about httpx vs httpx2. Cosmetic; not addressed.

**Test status:** 31 tests, all passing. `ruff check` and `ruff format --check` clean.

- **Covered:** circuit-breaker state machine including cooldown and half-open probe (deterministic via injected clock, no sleeps); router fallback, model tiering, and the transient/non-transient error split; rate limiter per-IP sliding window, global budget, and `Retry-After`; API auth rejection paths, `/health` reachability, and that unauthenticated requests don't consume budget.
- **Not covered — the real gap:** `src/llm/providers.py` has **no unit tests**. `AnthropicProvider` and `GeminiProvider`, including their `is_transient()` classification, are exercised only via `FakeProvider` in the router tests. The breaker's correctness depends entirely on that classification being right, so this is the highest-value test debt in the repo. Also untested: `SearchQuotaTracker` directly, and the `lifespan` fail-fast path.
- No live API calls anywhere; CI deliberately runs without secrets so a test that reaches a real endpoint fails there.

**Carried forward — what the next phase needs to know:**

1. **Never call a provider SDK directly.** Use `from src.llm import get_router, LLMTier` and `await router.complete(prompt, tier=LLMTier.EXTRACTION|SYNTHESIS)`. The tier picks the model; callers never name one.
2. **Tests must reset process globals.** `tests/conftest.py` has an autouse fixture clearing `get_settings.cache_clear()`, `reset_limiter()`, `reset_router()` and the quota tracker, and injecting placeholder credentials. New process-level singletons need adding there or tests will leak state across cases.
3. **Companies House specifics already confirmed** while building the 0.7 mock: Monzo Bank Limited is `09446231`, incorporated 18 Feb 2015, SIC `64191` + `64999`. Auth is HTTP Basic with the API key as username and an empty password.
4. **`respx` is not yet a dependency.** Phase 1.4 plans to add it for httpx transport mocking.
5. **Five spec fixes landed during this phase** and Phases 4/5 depend on them: 5.4 precedence order, the `official_registry`/`official_self` split, `counts_toward_confidence`, the claim-granularity rule (worked example in `docs/phase-4.1-claim-granularity-example.md`), and badges reporting claim count rather than source count.

---
