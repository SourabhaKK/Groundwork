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

## Phase 2 — Data Gathering Layer

**Status:** complete for core scope. 2.1, 2.2, 2.3, 2.3a, 2.4 and 2.6 done; **2.5 remains deferred by design** (TASKS.md scopes it as post-Phase-7). Built on branch `phase-2-data-gathering`. Commits `d1b45e3`, `27ebb96`, `0fa87d5`, `e6acae9`, `67a3287`.

**Built:** the layer between "we know which company this is" and "we have something to extract claims from." Two gathers, one tagged evidence bundle, one failure vocabulary, and a breaker per source.

- `src/data_sources/registry_gather.py`: `gather_companies_house(profile, client)` — filing history, officers and PSC fetched concurrently off an already-resolved `CompanyProfile`, into a `RegistryGather`. Three new endpoints and their models were added to the existing `CompaniesHouseClient` rather than a second client, so one status classification serves everything.
- `src/data_sources/tavily.py` + `web_gather.py`: a Tavily client mirroring the register client's shape, and `gather_web(profile, client)` running one query per report section concurrently. Queries are fixed templates built from the trading name (legal form stripped), not LLM-generated.
- `src/data_sources/sources.py`: the 2.3a taxonomy as a pure `classify_source()` function, one `TaggedDocument` type both gathers convert into, and `build_evidence()` merging them into an `EvidenceBundle` that carries every stated gap alongside the documents.
- `src/data_sources/breakers.py`: one breaker per *provider*, no fallback target, plus a `guarded()` helper. `/health` now reports source-breaker state and credits spent.

**The load-bearing design decision, stated once here because three modules depend on it:** this layer is **partial-tolerant**, unlike Phase 1's all-or-nothing hydration. Resolution makes a uniqueness claim that a partial fetch silently weakens; gathering only accumulates evidence, so one failing source degrades to a *recorded* `GatherFailure` rather than losing the other sources. Two failures are never degraded: an auth error raises (a rejected key would otherwise print "no officers recorded" for every company we ever look at), and a 404 on the PSC endpoint is a finding — nobody is registered as controlling this company — because that is genuinely how the register answers.

**Deviations from TASKS.md:**

1. **The global report budget changed, 200 → 130, and Phase 0's stated rationale for 200 was wrong.** Phase 0 sized the cap against "Tavily's 1,000-credit free tier (~250–400 runs)", which assumed ~3 credits per report. Advanced-depth searches cost 2 credits, not 1. The first version of Phase 2.2 proposed three advanced searches — 6 credits/report, 1,200 against a 1,000 ceiling — and **the arithmetic was wrong in the session before it was caught in review**, not by the plan. Reconciled: 5 credits per report (basic for business model, advanced for competitive landscape and reputation, where deeper retrieval genuinely changes what comes back), 6 worst case with a Phase 3.5 re-search, 200 credits reserved for development and Phase 7.1 manual runs that bypass the token gate entirely, leaving 800 to serve from — hence 130. `tests/test_web_gather.py::test_the_report_cap_still_fits_inside_the_free_tier` pins the arithmetic so it cannot silently stop closing.
2. **The cap must be assumed to be spent inside one month.** The review window is days long and the counter is in-process, so a container replacement resets it — it cannot be relied on to spread load across a month boundary, and the same reset means more than 130 could slip through. Mitigated by counting *credits* rather than searches on the quota tracker and exposing them on `/health`, and — the load-bearing half — by exhaustion surfacing as a stated gap rather than an empty evidence list.
3. **New module `src/data_sources/failures.py`**, not in the TASKS.md list. `GatherFailure`/`FailureKind` are consumed by both gathers, by 2.4's breaker policy and by Phase 5's rendering; leaving them in `registry_gather` would have made the web gather import the register module to describe a Tavily failure.
4. **`CircuitBreaker` moved from `src/llm/` to `src/circuit_breaker.py`.** A data-source module reaching into the LLM package for a generic state machine misrepresents what it is. Same precedent as Phase 0 creating `src/llm/` in the first place. Note `CircuitOpenError` was exported by the router but never actually raised anywhere — 2.4 is its first real use.
5. **`_LEGAL_SUFFIXES` moved from `agent/resolve.py` to `companies_house.py` as public `LEGAL_SUFFIXES`.** `web_gather` needs the same list to turn "MONZO BANK LIMITED" into "Monzo Bank" for press searches; duplicating it was the alternative, and a `data_sources` module importing from `agent` would have inverted the layering.
6. **`respx` is not being added, and the question is now closed.** Phase 0 carried it forward for 1.4; Phase 1 didn't need it; Phase 2 said "revisit only if Tavily mocking proves harder", and it wasn't. `httpx.MockTransport` handled per-query routing and a real-socket timeout test. For the SDK-based LLM providers, HTTP-level mocking is the wrong layer entirely — their own retry and error-wrapping sits between the wire and the exception we classify.

**Real-world surprises and things found by building:**

- **The public register URLs were verified live, not reconstructed from memory.** `find-and-update.company-information.service.gov.uk/company/09446231` plus `/filing-history`, `/officers` and `/persons-with-significant-control` all resolve to real MONZO BANK LIMITED pages. Citations point there rather than at `api.company-information.service.gov.uk`, which needs a key and answers in JSON — a citation an underwriter clicks and cannot read is not a citation. Incidental confirmations: the officers page is titled "…people", not "…officers", so don't assume path and heading match if anything ever scrapes rather than links; and the PSC page independently confirms **Monzo Bank Holding Group Limited** as the active controller.
- **A real auth bug in `CompaniesHouseClient`, latent since Phase 1.** Credentials were baked into the `httpx.AsyncClient` the class constructs, so a caller injecting its own transport sent **no credentials at all** — and the register answers that with a 401 indistinguishable from a wrong key. This is Phase 1's test-vs-live key trap arriving from a different direction. Not reachable in production yet because nothing injected a client, but the gather layer was about to. Auth is now attached per request in both clients; `test_auth_survives_an_injected_transport` is the regression guard.
- **The quota tracker could mask its own most useful diagnosis.** `record_error()` overwrote an `EXHAUSTED` status under a "last observation wins" rule, which reads as sensible until the searches run concurrently: a report fires three at once, and if one returns 429 and another 503, whichever resolved last decided what `/health` said. "Last" is not a meaningful ordering between simultaneous calls, so the tie is now broken by specificity — `exhausted` cannot be overwritten by `error`, though a genuine success still clears it. Found by writing the tracker's first direct tests, not by review.
- **Tavily's exhaustion status code is reasoned, not observed.** 402 and 429 are both treated as exhaustion, and the ambiguity is real: a 429 can mean short-term rate limiting rather than an exhausted plan. Over-reporting a stated gap is the safe direction, and the breaker's 60-second probe recovers automatically if it was temporary. **This has not been checked against a genuinely exhausted account** — see the carried-forward note below.
- **`include_raw_content` is deliberately off.** Phase 0 recorded that Trustpilot, Google Play, FinTech Magazine and others 403 direct fetches. Requesting full page bodies is exactly the thing most likely to fail unevenly across domains, leaving one section thinner than another for reasons unrelated to the company — so extraction works from Tavily's snippets.
- **`official_self` has no authoritative source to check against.** Companies House holds no website field, so the tag is a heuristic matching the domain's registrable label against the company's name. Both error directions are conservative — a false positive demotes a source out of the HIGH-band gate unless corroborated; a false negative leaves the company's own site at the lowest weight. It can withhold authority, never invent it.

**Test status: 232 tests, all passing** (up from 80). `ruff check` and `ruff format --check` clean.

- **Covered this phase:** partial tolerance under every failure mode, including that a failing source does not cancel its siblings in flight; the PSC-404-is-a-finding rule and its control (a 404 on filings *is* recorded); auth raising rather than degrading; filing-history truncation being visible; the five taxonomy tiers including a lookalike-domain test (`notfca.org.uk` must not inherit the regulator's authority) and the self-beats-press case for a media company; that a `TaggedDocument` cannot be constructed without a URL and timestamp; breaker isolation between the two sources; the per-report credit arithmetic and that the report cap still fits inside the free tier.
- **Phase 0's carried test debt is now closed.** `src/llm/providers.py` has direct tests: `is_transient()` is pinned by constructing **real SDK exception objects** rather than fakes, so the suite fails if Anthropic or Google moves `status_code` or renames a class. That couples those tests to SDK versions on purpose — it is the assumption nobody would notice breaking until a live run. `SearchQuotaTracker` and the `lifespan` fail-fast path are also now tested, both carried since Phase 0.
- **The gap this phase left at first draft — nothing validated against live Tavily — was closed before merge, not carried.** Left in place as written because it was true at the time and because it is the reason the trial happened: see "Live Tavily trial" below, which found three broken assumptions that mocking could not have surfaced. **Still true after the trial:** the *exhaustion* status codes (402/429) remain reasoned rather than observed, since the trial ran against a healthy quota — a genuinely exhausted account has never been seen. Test count rose 232 → 257 as a result of the trial's findings.

**Live Tavily trial — run before merge, and it changed the code.**

The gap named above was closed rather than carried. Three real queries for MONZO BANK LIMITED through the actual `gather_web` → `build_evidence` path, 15 results, **10 credits spent across two runs** (the first spent its 5 and then died printing a phonetic character to a cp1252 Windows console — a bug in the scratch script, not the code under test; results are now dumped to JSON so analysis never needs a re-run).

What held: Bearer auth worked first time, all 15 rows parsed with none dropped, no unexpected status codes, the breaker never left CLOSED, and per-run cost was exactly the 5 credits the report cap is derived from. `monzo.com` → `official_self` and a Financial Ombudsman decision → `official_registry` were both tagged correctly with no tuning. Snippet content ran 559–2,404 characters, which vindicates `include_raw_content=False` rather than merely excusing it.

Two shape facts worth keeping: **`published_date` was null on all 15 results**, so recency cannot be asserted from Tavily metadata and must come from the content itself; and Wikipedia came back in *two* sections, so Phase 4.3's dedup has to be URL-aware across sections while preserving per-section attribution — Phase 5.4 counts per section, so silently dropping one copy moves a band.

**The prediction that was wrong.** Phase 2.2 recorded that the reputation query was the one most likely to disappoint. It was the best of the three: an adjudicated Financial Ombudsman decision, a March 2026 Guardian story, and a review blog with concrete figures. Recorded because the caution was reasonable and still wrong, and the design note should not repeat a worry the evidence contradicted.

**Three assumptions broke, all fixed before merge:**

1. **`official_self` conflated "hosted by" with "authored by".** `community.monzo.com/t/an-honest-customers-review/...` was tagged `official_self` — a customer complaint receiving the tier meant for audited annual reports, which under 2.3a can count toward the HIGH-band gate once corroborated. This directly refutes the defence recorded when the heuristic was built ("it can withhold authority, never invent it"); user-generated content on a company domain is the case that breaks it. Fixed: forum/community hosts and paths on a self domain are demoted to `review`, which is honestly what they are.
2. **Entity conflation — the most serious finding.** A reputation query about MONZO BANK LIMITED returned a Better Business Bureau complaints page for **Monzo, Inc.**, a US entity. Nothing downstream would have caught it, because every snippet says only "Monzo", and complaints against a foreign affiliate would have been attributed to the UK bank being underwritten. Phase 1 solved this class of problem for *resolution*; it was never solved for *evidence*. Fixed in two halves, because query filtering alone cannot be trusted: every query template is now anchored to the UK, and `sources.names_a_different_entity()` flags what still gets through onto `TaggedDocument.entity_warning`. The flag is deliberately **not** a filter — whether a foreign affiliate is irrelevant or highly relevant is a judgement for extraction. **TASKS.md 4.2 now carries a required consumer for it**, without which the flag would be decoration.
3. **The allowlist was written from memory of national outlets; what actually ranks is niche fintech press and SEO content farms.** `ffnews.com` (a genuine trade outlet) and `bbb.org` (a complaints platform) were both missing and are now added. Honest proportion: 9 of the 11 `general_web` results genuinely belonged there, so the list was narrow rather than broken.

**One finding deliberately not fixed.** `committees.parliament.uk/writtenevidence/...` — "Monzo — Written evidence" — is an authoritative *host* publishing the company's *own* submission. Adding `parliament.uk` to the registry tier would systematically dress self-authored lobbying material as independent regulator-grade evidence, so it stays `general_web`: understating it, which is the safe direction. Recorded in `sources.py` next to the domain lists, because that is where someone would come to "fix" it.

Every URL in the new tests is one the live API actually returned, so they are regression guards against observed data rather than invented cases.

**Carried forward — what Phase 3 needs to know:**

1. **Live validation is done for Tavily** — findings above, and three of them changed the code before merge. The remaining untested-against-reality surface is the LLM providers, which Phase 4 is the first phase to actually call. Expect the same pattern: the classification and prompt assumptions that survive mocking are not the ones reality tests. 190 search credits remain of the 200-credit dev reserve.
2. **Consume `EvidenceBundle`, and render `failures`.** `build_evidence(profile, registry, web)` returns documents already tagged and timestamped, plus every stated gap. Phase 5 rendering those gaps is what makes the partial-tolerance design honest — a bundle whose failures are dropped is strictly worse than one that failed loudly.
3. **`for_section()` includes every register document.** Register evidence is not section-specific: incorporation date and SIC bear on business model, officer churn on quality signals. Only web results carry a `section`.
4. **Phase 3.5's re-search must go through the same breaker and count against the same budget.** It is one basic-depth search, bounded at one per report — that is the 6th credit the cap was sized against. Re-searching without the breaker would reintroduce doomed calls after exhaustion.
5. **Claim granularity is not solved by document granularity.** The register is deliberately one document per *endpoint*, not per record, so fifty filings cannot inflate apparent evidence volume. Phase 4.1's granularity rule still has to do its own work on top of that.
6. **2.5 is deferred, not forgotten, and the data is already there.** PSC records and full filing history are gathered, tagged and in the bundle today. What is deferred is the *analysis* — SIC-code changes over time, officer turnover rate, filing punctuality. If it is not built after Phase 7, it goes in the design note's deferred section, per TASKS.md.

---

## Phase 3 — Agent Orchestration (LangGraph)

**Built:** a LangGraph state machine over the Phase 1 and 2 functions —
`resolve → (gather_registry ∥ gather_web) → assemble_evidence → extract →
evaluate_sufficiency → [research_gap → extract → evaluate] → synthesize`, with a
`halt` terminal for every non-report outcome. Plus `score_confidence()` (Phase
5.4) and `EvidenceClaim` (Phase 4.1), both pulled forward because 3.5 cannot be
wired or tested without them.

**The scope change, stated plainly for the design note.** 3.5 was specified as an
adaptive re-search loop and was simplified, under a 2-day remaining timeline, to
a **single bounded pass**: one retry per under-evidenced section, not iterative
refinement. The three properties that make it genuine agentic branching rather
than a retry were kept, and each has a test:

1. **It fires only on a measured judgement.** The trigger is
   `score_confidence()` — literally the same function the report publishes its
   bands from, not a parallel heuristic that could drift. A section re-searches
   only when it scores LOW *for a reason more evidence could fix*.
2. **The refined query is demonstrably different and gap-targeted.** Generated
   by Haiku from the section, the original query, the URLs already retrieved and
   the specific gap, then validated mechanically before a credit is spent —
   rejected if it drops the company's distinctive name tokens, exceeds the
   length cap, or overlaps the original above a token-similarity threshold.
3. **The decision is logged either way.** Every section gets a
   `SufficiencyEvaluated` entry every run, carrying the band, the reason, the
   claim count, and whether a re-search fired *and if not, why not*. A log that
   recorded only the times the agent acted could not be audited for judgement.

**Three design choices worth being able to defend:**

- **Parallel gather is two real graph nodes, not one node calling
  `asyncio.gather`.** Identical wall-clock. The difference is that the
  parallelism and the per-source failure branch are visible in the graph, and
  the graph is a deliverable.
- **`evaluate → research_gap → extract → evaluate` is a genuine cycle**, exited
  because a counter on the state says so, not because the edge back is missing.
  `recursion_limit` is a backstop for a counter bug, not the bound.
- **A contradicted section is not re-searched.** `ConfidenceReason` distinguishes
  "thin" from "contradicted" — both are LOW, only one is worth a credit. Phase
  4.3 requires conflicts to surface rather than be resolved, so spending a
  search trying to out-vote one would work against it. Honest tradeoff: more
  evidence would sometimes adjudicate a conflict, and this declines to try.

**The live pass, and the assumption it broke.** One live run against real Monzo
data, aimed at the phase's highest-risk assumption: that the model produces a
genuinely gap-targeted query rather than a paraphrase.

*What held.* The refinement was better than expected. Against a reputation
section whose first pass returned Trustpilot, a review blog and a Monzo
community thread, Haiku produced *"Monzo Bank Limited FCA complaints data UK
Financial Ombudsman Service outcomes"* — token overlap 0.19 with the original,
explicitly reasoning that it targeted "official regulatory and ombudsman records
rather than consumer review platforms". The guardrail never had to fire.

*What broke — and it was not the LLM.* That excellent query, run at **basic**
depth, returned Wikipedia, `monzo.com`, and two app-store listings. The same
query at **advanced** depth returned the FCA's firm-level complaints table, the
FCA Final Notice imposing a £21m fine, a Financial Ombudsman decision PDF, and
ombudsman data on wrongly-rejected fraud refunds — five results, **none** of
which basic found.

The re-search had been specified at basic depth on the reasoning that a gap is a
coverage problem, so breadth beats depth. That reasoning was wrong, and Phase
2.2 had already recorded why: for competitive landscape and reputation, the top
hits are aggregator homepages whose basic snippet is generic boilerplate, and
depth is what returns the actual text. That does not stop being true because it
is the second search rather than the first. **A re-search now inherits its
section's first-pass depth** — a re-search cannot be cheaper than the pass it is
trying to rescue.

Worth noting what this nearly cost: the feature would have shipped looking
correct. The branch fired, the query was good, the log was honest, new URLs came
back. Only reading *which* URLs showed the retrieved evidence was useless for
the gap it was aimed at. A mocked test cannot catch that, and neither can a test
that asserts "the re-search returned new documents".

**Budget consequence, and a deviation from Phase 2's recorded figure.** Phase 2
scoped re-search at one basic search per report — 6 credits worst case. It is
now one per under-evidenced section, at that section's own depth, so the worst
case is the baseline twice over: 5 + 5 = 10. Typical stays 5-7, since all three
sections are rarely thin at once, but a cap sizes on the worst case.
`global_report_budget` therefore moves 130 → **80** (800 serve credits ÷ 10).

**Deferred, with reasons:** extraction (4.2) and synthesis (5.1-5.3) are
injected dependencies with deliberate placeholders — the placeholder extractor
returns *nothing* rather than plausible claims, so every section honestly scores
LOW/NO_CLAIMS while Phase 4 is outstanding. Inventing claims to make the graph
look healthier would put a fabrication inside the one pipeline whose entire
purpose is grounding.

**Carried forward — what Phase 4 needs to know:**

1. **Implement `Extractor`**, not a new call site. `AgentDependencies.extractor`
   is the seam; the graph does not change. On the second pass it is called with
   only the re-searched sections, so it must not assume all three.
2. **`merge_claims` dedupes on a content hash of `source_url + text`.** An
   extractor that rephrases the same fact on a second pass will create a
   duplicate claim and inflate a section's count — which moves a band with no
   new evidence.
3. **`counts_toward_confidence=False` is the mechanism for `entity_warning`.**
   4.2 must either refuse claims from a warned document or mark them non-scoring
   with the mismatch stated in the claim text.
4. **The claim-granularity rule is a convention the type system cannot enforce.**
   Every count-based condition in `score_confidence` inherits it.
5. **190 → ~182 search credits** remain of the development reserve (6 spent on
   the live 3.5 run, 2 on the depth comparison).

---
