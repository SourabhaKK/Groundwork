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

## Phase 1 — Company Resolution & Disambiguation

**Status:** complete. 1.1–1.4 all done, and unlike Phase 0 this one was validated against the **live** Companies House API, not only against mocks. Built on branch `phase-1-company-resolution`.

**Built:** `resolve_company(query, client)` — the front door of the system — turning typed human input into either one specific legal entity or a statable reason why not.

- `src/data_sources/companies_house.py`: an async client over two endpoints (search-by-name, profile-by-number), parsing the register's JSON into our own Pydantic models at the boundary so no Companies House shape escapes the module. Three-way error split: `CompanyNotFound` (a reportable fact), `CompaniesHouseAuthError` (our misconfiguration, must fail loudly), `CompaniesHouseUnavailable` (5xx/429/timeout/connection). No retry — a 429 surfaces its `Retry-After` for Phase 2.4 to act on. No module singleton: an `httpx.AsyncClient` can't be closed from a synchronous test reset hook, so callers own the lifetime via `async with`.
- `src/agent/resolve.py`: search → shortlist → hydrate → decide. Candidates are ranked on name match, status, and SIC (shell codes penalised); the top 5 are hydrated concurrently because the search endpoint returns no SIC codes and no address, so ranking on either requires the full profile.
- **Auto-resolution is deliberately hard to reach.** It requires a unique exact normalised-name match that is active, a going concern, and not a dormant shell. Anything else returns `NEEDS_CONFIRMATION` with the company still attached — the automation is withheld, not the information.
- **Relevance floor**: candidates with zero name similarity are dropped before hydration, with former-name candidates exempt. Without it, junk input produced a plausible-looking candidate list (see surprises).
- 1.4 closed properly: `tests/test_companies_house.py` tests the client directly — auth scheme, every status→exception mapping, `Retry-After` propagation, and timeout enforcement against a **real loopback socket** rather than asserting a config value was passed.

**Deviations from TASKS.md:**

1. **1.2's output shape changed during the build, and TASKS.md has been updated to say so.** The plan implied resolution returns "one company" or "a list to choose from". Building it produced a third case that is neither: exactly one company identified, but not safe to accept silently. That became `NEEDS_CONFIRMATION` carrying a **list** of `ConfirmationReason` values (`dormant_sic`, `dissolved`, `not_active`, `previous_name`, `relaxed_match`). A list, not one value, because a dissolved company found under a former name is both things at once and reporting whichever was checked first hands back the less alarming half at random. Two of those reasons (`not_active`, `relaxed_match`) were additions beyond the three originally scoped, both flagged and approved at the time. **A fresh session should not expect TASKS.md's original 1.2 wording to describe the code** — this is why.
2. **Company-number normalisation lives in 1.1, not 1.3.** People type `9446231`, not `09446231`; without normalising, lookup-by-number 404s on the most natural input a user can give and reads as "no such company". 1.1 doesn't work without it.
3. **`respx` was never added.** Phase 0 carried it forward as the plan for 1.4. `httpx.MockTransport` covered every case, including the real-socket timeout test, so a dependency was not introduced to do what the standard library and an existing dependency already did. Revisit for Phase 2 only if Tavily mocking proves harder.
4. **`SEARCH_LIMIT` raised 20 → 100** after live measurement (below). Same single request; the register's per-page maximum.

**Real-world surprises — all from live testing, none from a new audit round:**

- **Companies House issues separate test and live API keys, and the wrong one fails as `401`, not as a distinguishable error.** A sandbox key against the live host is indistinguishable from a revoked key. Cost real debugging time; worth knowing before Phase 6 deployment configures a key it can't interactively test.
- **"Mondo" was never Monzo's registered name, and this is a different failure class from a rebrand.** The register's `previous_company_names` for `09446231` is **FOCUS FS LIMITED** (2015-02-18 → 2016-10-21). Mondo was a *trading/brand* name, dropped after a trademark dispute, and never entered the register at all. Searching "Mondo" returns zero results for Monzo at **any** rank — confirmed by sweeping the complete result set, not just page one. Former registered names are findable; brand names that were never registered are structurally unreachable, because there is no record to match. These look identical to a user and have completely different causes. Both are documented in README §5.
- **A test fixture in this repo asserted the invented former name "MONDO BANK LIMITED" before this was checked.** Corrected to the verified value. Recording it because the project's hard boundary is never fabricating a source-backed fact, and a fixture stating a false register fact is exactly what gets read later as ground truth.
- **The search endpoint returns former-name match data pre-hydration**, in a `snippet` field plus a `matches` object — e.g. `q="Mondo"` returns `KNOX MORTGAGES LIMITED` with `snippet: "MONDO MORTGAGES LIMITED"`. This was assumed impossible when tier 3 was designed. It is now used for *discovery* only: `snippet` decides who gets hydrated, and the match is still confirmed against `previous_company_names` on the full profile before anything is surfaced. `snippet` is an undocumented relevance artifact and must never become the reported fact.
- **35% of the search window can be former-name noise.** For `q="Monzo"`, **7 of the first 20** results were former-name matches from unrelated companies, and MONZO BANK LIMITED arrived **4th**. Our own pre-score re-ranks it to 1st, so the shortlist was never the exposure — `SEARCH_LIMIT` was. At 20, enough noise pushes the correct entity out of the fetched window entirely, silently, leaving a full candidate list that simply lacks the right answer. Hence 100.
- **The register fuzzy-matches almost any input.** `"Nonexistent Widgets Xyzzy"` returned 34 results including AJAX WIDGETS LIMITED and XYZZY LIMITED — real companies, zero connection to the query — and we surfaced five as plausible candidates after paying five profile fetches for them. Every one scored `match="none"`. This is why the relevance floor exists; it was found by live testing, not by review.
- **`ruff format` rewrites a docstring opening `""""Acme` to `""" "Acme`.** Cosmetic, but it silently broke an exact-match edit; not worth working around.

**Test status: 80 tests, all passing** (up from 31). `ruff check` and `ruff format --check` clean.

- **Covered:** the auto-resolve gate including the dormant-shell case and its discriminating control; ranking precedence pinned both directly and end-to-end; malformed input rejected before any network call; dissolved vs. administration distinguished; former-name discovery, its confirmation requirement, and its exemption from the relevance floor with a control proving the exemption does the work; reasons accumulating rather than dropping one; relaxed retry; junk input returning `NOT_FOUND`.
- **`CompaniesHouseClient` now has direct unit tests** — auth scheme decoded and asserted, every status→exception mapping tested at the client rather than inferred through `resolve_company`, `Retry-After` propagation, and read-timeout enforcement over a real loopback socket. **Phase 0's carried test debt on this item is closed.**
- **Still outstanding from Phase 0:** `src/llm/providers.py` has no direct unit tests. `AnthropicProvider` / `GeminiProvider` `is_transient()` classification is still exercised only via `FakeProvider`. Unchanged this phase; still the highest-value remaining test debt.
- No live API calls in the suite. The live validation described above was manual, run from a scratch script outside the repo, in the same spirit as Phase 7.1.

**Carried forward — what Phase 2 needs to know:**

1. **Consume the typed outcome, don't re-derive it.** `resolve_company()` returns a `Resolution` with `kind` ∈ `RESOLVED` | `NEEDS_CONFIRMATION` | `AMBIGUOUS` | `NOT_FOUND` | `INVALID`, plus `profile`, `candidates`, `reasons`, and a human-readable `detail`. Only `RESOLVED` may proceed to gathering without a human in the loop. `NEEDS_CONFIRMATION` carries a real company and must be surfaced, not treated as a failure.
2. **The register's errors are already classified — preserve the distinction.** `CompanyNotFound` is reportable; `CompaniesHouseUnavailable` must never render as thin evidence; `CompaniesHouseAuthError` must fail loudly. Phase 2.4's per-source breaker should trip on the third and never on the first.
3. **TASKS.md 2.4 was clarified during 1.1**: each source gets its own circuit-breaker *instance* following Phase 0.6's state machine, with no fallback target. The pattern is shared; the LLM router object is not reused for non-LLM sources.
4. **Hydration is all-or-nothing.** If any shortlisted profile fetch fails, the whole resolution fails rather than ranking on survivors — the uniqueness guarantee is only a guarantee if every candidate considered was actually seen. Apply the same discipline to Phase 2's parallel gathers, or the same class of bug reappears.
5. **API key hygiene.** Three keys were exposed in session transcripts during this phase's live testing. Redact credentials in any command or output relayed outward from here on, and set the Restricted IPs field on the live key once Cloud Run exists (Phase 6).

---
