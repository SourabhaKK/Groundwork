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

## Phase 4 — Structured Extraction

**Status:** complete. 4.1 was already built in Phase 3; 4.2 and 4.3 done this
phase. Built on branch `phase-4-extraction`, cut from `phase-3-orchestration`
because `main` does not contain Phase 3 — see the correction below. Commits
`338c4ea`, `a77a3dd`, `4323a40`, `f831f65`. **Not merged: awaiting sign-off.**

**Correction to Phase 3's entry, per this log's own rule about never editing a
past entry.** Phase 3's entry is the only one with no `Status:` line and no
`Test status:` section, and TASKS.md ticks Phase 3 complete while its work sits
unmerged on `phase-3-orchestration` — `main` is still at `182224d`, the Phase 2
merge. The missing test-status line is not just bookkeeping: `ruff format
--check .` was failing on five files, all of them arriving in the four commits
on that branch, so **CI's formatting step was red on Phase 3 and nobody knew**.
`main` is verified format-clean, which is how the failure was localised. Fixed
in `338c4ea` as a separate commit. For the record, Phase 3 as it stands: 328
tests passing, `ruff check` clean, `ruff format` now clean.

**Built:** `src/agent/extract.py` — the implementation behind
`AgentDependencies.extractor`, the seam Phase 3 left open. The graph is
unchanged. Plus `src/llm/json_parsing.py`, a shared fenced-JSON finder, because
3.5's query refinement and 4.2's extraction now do the identical step and a
second copy would drift.

**The four decisions worth being able to defend:**

- **One model call per document, not per section.** 4.2 requires per-source-type
  framing, and source type is a property of a document — a section-level call
  puts an officer list and a Trustpilot snippet in one prompt and averages the
  two framings. The real payoff is the next point.
- **The model has no field through which a URL can arrive.** `source_url`,
  `source_type`, `section` and `origin` are attached here from the
  `TaggedDocument`; the response carries only the fact, a verbatim quote, and
  how directly the source supports it. AGENTS.md's never-fabricate-a-citation
  boundary is therefore unrepresentable rather than discouraged. A section-level
  prompt would have had to hand the model a menu of URLs to choose from, which
  is the exact mechanism by which a claim gets bound to the wrong source.
- **Every claim carries a quote, checked against the source text.** Normalised
  for typography, then a substring test. An unverifiable quote costs the claim
  its *score*, never its place in the report.
- **`entity_warning` is enforced in code, not by prompt.** A warned document's
  claims are unconditionally marked non-scoring, with the mismatch written into
  the claim text by us. The prompt is a request; only code makes "must not
  silently absorb" structurally true.

**Deviations and additions beyond the approved proposal:**

1. **`EvidenceClaim.id` now hashes `source_url + section + text`** (approved
   before implementation as D7, recorded here as a change to a Phase 3
   artifact). It hashed url+text, so the same URL gathered for two sections
   collapsed to one claim in `merge_claims` — exactly the shape Phase 2's live
   trial recorded when Wikipedia came back in two sections. Phase 5.4 counts per
   section, so the second section silently lost a claim it had genuinely
   gathered. A band moving on a hash collision. Second-pass idempotence is
   unaffected and is pinned by its own test.
2. **`MAX_CLAIMS_PER_DOCUMENT = 12`, added during implementation, not proposed.**
   Above it, a document's claims are all kept but none score. The don't-split
   half of the granularity rule has no mechanical enforcer and every count-based
   condition in `score_confidence` trusts it. Safe direction only — it can
   withhold confidence, never manufacture it — and it fired on real data (see
   below), so it is not theoretical.
3. **Conflict detection compares claims per section**, with the model
   referencing claims by list position and the positions mapped back to real ids
   here. An out-of-range position is discarded rather than guessed at.

---

### The live pass, and what reading the claims found

Per Phase 3's lesson, the point was to read what came out, not to count it. One
full run against real data: Companies House 09446231 (4 documents), a real
`gather_web` (15 results, **5 credits**), and the FCA enforcement notice fetched
live (HTTP 200, 3,339 characters of text after tag-stripping) — the document the
Phase 4.1 worked example says chunks into exactly 5 claims. Then two cheap
re-runs against the same saved documents (**0 further credits**) to test fixes,
plus one basic search (**1 credit**) for the entity-warning case. **6 credits
total; ~182 → ~176 of the development reserve.**

First run: **104 claims from 20 documents, 85 scored, 5 conflicts.** Bands
business_model HIGH, competitive_landscape HIGH, reputation MEDIUM. All three
plausible at a glance — which is exactly why the claims had to be read.

**1. Granularity on the FCA notice: 7 claims against the worked example's 5,
and the two extra were different failures.** One was a genuine over-split (the
requirement breach and the 34,000 accounts opened in breach of it came back as
two claims; the worked example folds them into one). The other was not a
granularity error at all: *"This was the 10th fine the FCA has imposed on a bank
for financial crime control failings in the last 4 years."* That is a fact about
the **FCA**, not about Monzo, and nothing in the prompts said claims had to be
about the company under report — it was assumed. Fixed; every prompt now says
so, and the claim is gone on re-run. The notice now yields 6, within the ±1 the
rule tolerates if the breach split is counted as the one disagreement.

**2. The failure that would have shipped: register list documents extracted one
claim per row.** The filing-history document produced nine claims of the form
*"The company filed full accounts on 1 September 2025."* The officers document
produced nine of *"X was appointed director on DATE."* All eighteen scored, all
routed to reputation, which is the correct section — governance signals are
Section 3 — so reputation carried 37 scored claims of which half were filing-
clerk records. It did not move a band *in this run*, because the FCA notice and
the officer facts would have carried reputation to HIGH anyway. **The exposure
is a thin company**: nine filing rows alone clear HIGH's `>= 3` gate with an
`official_registry` source present, so a business with no press coverage and no
regulatory history could report HIGH confidence on the strength of having filed
its accounts. That is precisely the failure Phase 7.1a's fourth criterion
exists to catch, arriving from a direction nobody was watching.

The registry framing now says a row is a record while the fact is the pattern
across rows, with the filing-history case as the literal example. **It half
worked, and the half that did not is the honest finding.** Officers went 9 → 7
and changed shape entirely: an aggregate ("9 current officers comprising 6
directors and 1 secretary") plus six resignations named individually *because
they were independently significant* — two directors who served under three
months, which is exactly the judgement the rule asks for. Filing history was
**unchanged**: still nine one-row-per-claim. The same instruction, in the same
prompt, on two documents from the same endpoint family.

**Second attempt, and the diagnosis it produced.** A filing-history-specific
worked example was then added — the actual rows, the three correct pattern
claims, and the nine wrong ones spelled out as the anti-example. **It made the
document worse: 9 → 12 claims, still one per row.** Officers collapsed further,
to 2. So the instruction was not being missed for want of a concrete example.

What isolated it was running the same prompt against the same document with the
noise removed. The filing-history document renders the **100 most recent filings
of 213 on the register**, and about 90 of those rows are opaque officer-movement
codes — `2026-07-30 — officers —
termination-director-company-with-name-termination-date`, repeated. Filtered to
the 13 rows that are accounts or confirmation statements, the same prompt and
the same model returned **exactly the three claims the worked example asks for**,
in its shape:

> "The company has filed annual accounts in each of the last six years, most
> recently full accounts filed on 1 September 2025."
> "The company has filed a confirmation statement in each of the last four
> years, most recently on 11 November 2025."
> "The company changed from filing group accounts to filing full accounts…"

**So this is not prompt non-compliance. It is document shape.** A hundred rows
of mostly machine codes drives enumeration, and no amount of instruction
survives that; the same instruction works on the officers document, which is 9
readable rows. Worth stating for the design note because it generalises: how
evidence is *rendered* into a document silently sets what extraction does with
it, and that rendering decision was made in Phase 2 for a different reason — one
document per endpoint, to stop fifty filings inflating apparent evidence volume.
It stopped them inflating the *document* count and did nothing about the *claim*
count, which is what Phase 5.4 actually scores.

The fix therefore belongs in `registry_documents`, not in the prompt and not in
a scoring cap: collapse the officer-movement rows into a count rather than
listing ninety of them, since those events are already fully represented in the
officers document. **Not applied — it changes what an evidence document
contains, which is Phase 2's layer and warrants its own decision.** Recorded in
README §5 as a disclosed limitation in the meantime. **This is an open
correctness risk carried out of Phase 4, not a solved problem.**

**3. The over-fragmentation guard fired on real data, exactly where the worked
example predicted.** Monzo's annual report produced 14 claims: every metric, and
then every growth percentage as a separate claim — "revenue £1.7bn" and "revenue
grew 39%" as two facts. The worked example names this as the clearest form of
over-fragmentation. All 14 were excluded from scoring and kept in prose. Without
the guard, one self-published page would have contributed 14 claims to
business_model's count.

**4. Quote verification excluded 19 of 104 claims (18%), and the false-rejection
rate is concentrated by document shape.** Nearly all were the annual report and
Trustpilot — pages whose content arrives as a mangled table, where the figure is
genuinely present but not as a contiguous span the model can quote. So the check
does what it was built for (a fabricated quote fails) at a real cost (a true fact
in a table can fail too). The direction is safe: the claim still renders, it
just does not score. Recorded as a known limitation rather than tuned away,
because loosening the check to accept non-contiguous fragments would make it
stop catching the thing it exists to catch.

**5. Conflict detection surfaced a genuinely good conflict and one
misclassification — the second is a disclosed known limitation, not an
incidental observation.** The good one: Trustpilot rates `www.monzo.com` at 4.6 and
`web.monzo.com` at 1.8 — two real pages for the same company, flagged MAJOR.
That is the kind of disagreement the brief wants surfaced rather than resolved,
and neither side was dropped. The misclassification: "40% of customers use Monzo
as their main account" versus "49% of monthly active users use it as a primary
bank" was called MAJOR. Those are different denominators, not a contradiction —
MINOR at worst. It did not drag business_model to LOW **only because** the 49%
claim came from the annual report and had already been excluded by the
over-fragmentation guard, and `score_confidence` only counts a conflict when
every claim it references scores. A correct outcome reached by luck, and worth
saying so plainly rather than presenting the band as evidence the rule works.

**Logged as a known limitation and carried, not fixed.** A false MAJOR is
one-directional — it takes a section to LOW when its evidence does not actually
disagree, so it can only understate confidence, never inflate it. That is the
safe direction, which is why it is disclosed rather than patched under time
pressure. The fixes considered and rejected for now: refusing conflicts between
figures with different units (misses most real conflicts, which are not
numeric), and requiring two independent classification passes to agree (doubles
the cost of every report to catch a bounded, safe-direction error). The shape a
real fix would take is a second pass that must state the *basis* of the
disagreement, giving a denominator mismatch somewhere to be declared. Written up
in README §5 so it is visible to a reader who never opens this log.

**6. The entity-warning requirement, verified live — after the first attempt
quietly did the wrong thing.** One basic search returned four real Better
Business Bureau pages for **Monzo, Inc.**, the US entity, with the warning
computed by `names_a_different_entity` on real content. First run: **zero claims
from all four documents.** The model refused outright. TASKS.md 4.2 permits
refusal, so this passed the letter of the requirement — but D5 chose flagging
over refusal precisely because dropping the evidence hides something a reader
may want, and one of those pages carries a real fact ("30 total complaints in
the last 3 years, 9 closed in the last 12 months"). The prompt had reverted the
decision by accident. Reworded to ask for the facts while forbidding attribution
to the UK company, then re-run: **5 claims, none scoring, every one prefixed
`[Refers to "Monzo, Inc.", not MONZO BANK LIMITED]`, band LOW/NO_CLAIMS.** The
prefix is written by code, so it holds whatever the model returns — pinned by a
test that feeds in a maximally uncooperative response attributing the complaints
flatly to the UK bank.

**Incidental:** the same fact arrived from two BBB URLs (page 1 and page 3 of
the same complaints listing) as two claims. Different URLs, so different ids,
so `merge_claims` keeps both. Cross-document duplication of one fact is not
handled and would inflate a count if it happened to scoring claims. It did not
here, because everything from those documents is excluded.

**Test status: 375 tests, all passing** (up from 328). `ruff check` and `ruff
format --check` clean.

- **Covered this phase:** the entity-warning requirement with an unwarned twin
  as the control and an end-to-end check that the band actually moves; that
  provenance comes from the document and an invented `source_url` in the
  response cannot reach a claim; quote verification including the typography
  case, the fabricated case and the too-short case; the over-fragmentation guard
  and its control at the boundary; register claims routed by section and dropped
  when unrouted; a full pass versus a narrowed second pass; per-document failure
  isolation versus a total outage propagating; conflicts mapped to real ids,
  with out-of-range and self-referencing conflicts discarded; and the id fix in
  both directions — two sections keep two claims, a re-extraction still
  collapses to one.
- **Not covered:** nothing asserts the *quality* of a claim's wording, and
  nothing can. That is Phase 7.1a's rubric, and the live pass above is the only
  thing standing in for it today.

**Deferred / carried forward — what Phase 5 needs to know:**

1. **Filing-history over-fragmentation is unresolved** and is the one open
   correctness risk from this phase. Two fixes were tried and rejected on
   evidence rather than taste: a per-document ceiling on scoring claims (turned
   down — it changes what a claim count means, which is 5.4's settled spec), and
   two rounds of prompt instruction including a document-specific worked example
   (tried; made it worse, 9 → 12). The diagnosis above locates the cause in how
   `registry_documents` renders filing history, so **the fix is Phase 2's to
   make and needs its own decision.** Disclosed in README §5 until then.
2. **Excluded claims are a large fraction of the output** — 19 of 104 in the
   live run, before the guard's 14. Phase 5's prose must render them, with their
   `exclusion_reason`, or the report will look thinner than the evidence
   gathered and the reason for the thinness will be invisible.
3. **`ClaimExtractor(router, detect_conflicts=False)`** exists for callers that
   want claims without the comparison pass; the graph uses the default.
4. **A conflict can be surfaced without affecting a band**, by design — an
   excluded claim's disagreement still renders. Phase 5.5's badge must not imply
   otherwise.
5. **~176 search credits** remain of the development reserve.

### Addendum, appended 2 August 2026 during Phase 5 — a bug that was live while the numbers above were measured

**Nothing above has been edited.** This note is appended under this log's own
rule that a past entry stays an honest record of what was known at the time; it
exists because the numbers above were measured against a defect nobody had found
yet, and a reader who takes them as exact would be misled.

**The bug.** `json.loads` rejects a raw control character inside a JSON string.
Claude writes multi-paragraph prose into a JSON string field with the paragraph
breaks as *actual* newlines rather than escaped `\n`. Every extraction response
in this phase was validated with `model_validate_json`, which is strict, so any
response that happened to contain a literal newline inside a string was
discarded whole — and a discarded extraction response means **that document
contributed no claims at all**. The only symptom was a single
`unusable JSON` warning line in the log, which reads identically to a model
genuinely returning nonsense.

**Found in Phase 5**, not here, and only because the same failure appeared on the
synthesis side where it was impossible to miss — it took out the entire report.
Chasing it there surfaced the same log line against a Companies House
filing-history document in the same run. Fixed in `src/llm/json_parsing.py` via
`json_payload`, which loads with the standard library's `strict=False`; the fix
is described in full in the Phase 5 entry below.

**What this means for the figures above.** The 104-claim first run, the 85
scored, the 5 conflicts, the 18% quote-verification exclusion rate, the 9 → 7
officers and 9 → 12 filing-history counts, and the per-document numbers in the
granularity discussion were all measured while this bug was live. Any document
whose response contained a literal newline inside a string contributed zero
claims to those totals, silently.

**They have not been re-measured.** Re-running Phase 4's live pass under the
fixed parser would cost search credits and model tokens that the remaining
timeline is better spent elsewhere, and the *conclusions* those numbers support
do not turn on their exact values — the granularity findings, the entity-warning
behaviour, the over-fragmentation guard firing, and the conflict misclassification
were all read from the claims themselves rather than inferred from counts.

**So: treat every figure in the Phase 4 entry as approximate rather than exact,
and specifically as a floor.** The bug could only ever remove claims, never add
them, so the true counts under a fixed parser are the same or higher. Do not
quote these numbers as measurements in the design note without this caveat, and
do not diff them against any later run as though the two were measured the same
way — they were not.

---

## Phase 5 — Report Synthesis & Uncertainty Handling

**Status:** complete. 5.1, 5.2, 5.3, 5.5, 5.6 and 5.7 done this phase; 5.4 was
already built in Phase 3 and is **consumed unchanged** — `src/report/confidence.py`
has zero edits. Built on branch `phase-5-synthesis`, cut from `main` at `27a9802`.
Commits `9850d2b`, `b1ef671`. **Not merged: awaiting sign-off.**

**Test status: 415 tests, all passing** (up from 375). `ruff check` and `ruff
format --check` clean. No live provider call anywhere in the suite; the live
pass below was run from a scratch script outside the repo.

**Built:** `src/report/` — `synthesize.py` (the `Synthesizer` the graph has been
calling since Phase 3; the seam and the graph are unchanged), `prompts.py`,
`citations.py`, `render.py`, and `signature.py`.

**The decision the whole phase rests on: the model writes prose, code renders
every piece of structure.** Badge, per-section source table, citation numbering,
excluded-claims note, evidence gaps, conflict blocks and the bottom working
table are all assembled from data. Three things follow, each a requirement
somewhere else in the plan:

1. **5.7 becomes true by construction.** If both providers only ever supply
   prose, they cannot produce differently-shaped reports. A cross-provider shape
   guarantee resting on two model families following formatting instructions is
   one that has to be re-earned every time a prompt changes.
2. **7.1a's second criterion cannot drift.** The badge and the working table
   both print from the same `ConfidenceAssessment` the graph scored on. Neither
   counts claims a second time — `_working_row` reads `assessment.total_claims`
   and `assessment.scored_claims` rather than measuring the list it was handed,
   and two tests pin this by passing an assessment whose numbers deliberately
   disagree with the claim list. The mock's own badge once conflated claim count
   with distinct-source count, and that was a human rendering by hand.
3. **A citation cannot be invented.** The model is never shown a URL — each
   claim arrives with a marker already bound to it, so there is no mapping it
   could extend. Same mechanism as Phase 4.2 one layer up, and pinned by a test
   asserting no source URL appears in any synthesis prompt.

**Four further decisions worth being able to defend:**

- **One call per section, not one per report.** The band is a per-section fact
  and the prose has to be consistent with its own band; one call holding all
  three is how Section 3's HIGH language reaches Section 1's MEDIUM prose. It
  also bounds truncation exposure. Section 3 still gets a read-only digest of
  the other sections' bands and headline facts — enough to notice the kind of
  cross-section connection the mock makes by hand — but may not cite from it,
  because a claim gathered for another section is numbered differently there.
- **Excluded claims are prose input, and their exclusion is rendered from data.**
  Phase 4 carried this forward explicitly: 19 of 104 claims failed quote
  verification in its live run. Dropped, the report looks thinner than the
  evidence gathered *and the reason for the thinness is invisible*.
- **A failing section degrades to a stated absence, not a missing section.** It
  still renders its real band, sources and gaps, and says plainly that the
  written summary is what is missing. `AllProvidersFailedError` is the exception
  and propagates, so the graph halts rather than printing a thin report.
- **5.7 exercises Gemini through the breaker**, never by instantiating the
  provider — a primary that raises a transient error, exactly as an Anthropic
  outage would. Calling `GeminiProvider` directly would breach AGENTS.md and
  test the wrong path: the question is whether the *fallback path* holds the
  report's shape, and that path includes the breaker.

**Deviations and additions beyond the approved proposal:**

1. **`src/report/signature.py`, not proposed as its own module.** D9 listed four
   modules and put the comparator in `render.py`. It reads rendered markdown
   rather than the objects that produced it — deliberately, so the same function
   runs against the hand-written Phase 0.7 mock, which has no objects behind it.
   That makes Phase 7.1's structural diff a run rather than a build.
2. **The quality-signals badge carries a signal-strength word** (TASKS.md 5.3),
   derived from the band rather than judged separately. Two numbers saying the
   same thing is how they start disagreeing.
3. **`json_payload` added to `src/llm/json_parsing.py` and adopted by
   extraction and query refinement as well** — a Phase 4 / Phase 3 module changed
   from inside Phase 5. Reasoning under finding 2 below; it was a live
   correctness bug, not a refactor.

---

### The live pass, and the three failures it found

Three full runs against real data. Run 1 established the baseline, runs 2 and 3
verified fixes. **5 Tavily credits each, 15 total; ~176 → ~161 of the
development reserve.** Two diagnostic passes rebuilt prompts from the saved run
state and cost 0 credits. Every model call went through the router.

**1. The report was written entirely by the fallback, and the primary wrote none
of it.** Run 1: all three Sonnet synthesis calls returned no parseable JSON and
every section rendered the honest "synthesis did not complete" placeholder,
while Gemini — reached through the tripped breaker — produced the whole report.
The mechanism is the exact asymmetry Phase 3 recorded, arriving from the
opposite direction. `MAX_SECTION_TOKENS` was set to 2,000 *to protect the
fallback*, on Phase 3's measurement that a 2.5-generation model spends
`max_output_tokens` on internal reasoning before emitting visible text. What
that reasoning missed is that **Sonnet 5 runs adaptive thinking by default when
the `thinking` parameter is omitted** — unlike the generation before it, where
omitting the parameter meant no thinking at all — and `max_tokens` caps thinking
*and* response text together. So the cap chosen to protect the fallback starved
the primary. Isolated by re-running one section's prompt at two caps: 1,327
characters of unterminated JSON at 2,000, 4,502 characters of complete JSON at
6,000. Fixed by raising the cap; `stop_reason == "max_tokens"` is now logged,
because a truncated response is an HTTP 200 and was otherwise invisible.

**Worth stating for the design note: without the cross-provider check, this
ships looking fine.** A report was produced, it had the right shape, and every
band and source table in it was correct — because all of that is rendered from
data. Only the prose was missing, and only comparing the two providers showed
that the primary had contributed none of it.

**2. The failure that was not a Phase 5 failure. Sonnet writes literal newlines
inside JSON strings; Gemini escapes them.** Run 2, with the cap fixed: business
model parsed, the other two sections failed schema validation. The diagnosis was
not a schema mismatch at all — `json.loads` rejects a raw control character
inside a string, and Sonnet writes multi-paragraph prose with the paragraph
breaks as actual newlines. Both providers return the same response as far as the
model is concerned; the standard library accepts one and rejects the other.

**The same log line appeared on a Companies House filing-history document in the
same run, which makes this a correction to Phase 4 rather than a Phase 5
finding.** In extraction a rejected response means the document contributes **no
claims at all** — a claim count moving, and therefore a confidence band moving,
because a model used a newline. Nothing surfaced it but one warning line, and
**Phase 4's recorded live-pass claim counts were measured with this bug active**,
so they undercount what the same run would produce today. Fixed in the shared
`json_parsing` module with `strict=False`, the standard library's own switch for
exactly this, and adopted by extraction and 3.5's query refinement too. The
alternative — a regex escaping newlines inside string literals before parsing —
was rejected as hand-repairing a model response, the class of thing AGENTS.md's
no-hand-parsing rule exists to prevent. `tests/test_json_parsing.py` pins both
providers' shapes, and asserts the fixture is genuinely strict-invalid so the
test cannot quietly stop testing anything.

**3. Cross-provider equivalence, measured rather than eyeballed: zero
differences.** Run 3, both providers on the same evidence:
`structural_signature()` reduces a rendered report to section headings and
order, band per section, presence of each block, source-table row counts and
authority tiers, citation markers used, working-table rows, and trailing
content. **Claude and Gemini produced identical signatures** — not similar,
equal — while their prose differs completely. That is 5.7 satisfied on real
evidence rather than by argument, and it is a property of the design rather than
a lucky run: the shape does not come from the model.

### The structural diff against the Phase 0.7 mock — reported as found

**11 differences, and none of them are shape.** Every structural field matches:
section count, order and headings; badge present with a band; conflict block,
evidence-gaps block, sources table and exclusion note present in each section;
the working table with three rows; no summary table at the top; no trailing
content after the working table. The 11 differences are all **evidence volume**:

- **Bands.** Section 1 scored HIGH against the mock's MEDIUM. The mock's MEDIUM
  came from a MINOR conflict over adjusted-vs-statutory profit; the real run
  never retrieved the trade-press article that produced that disagreement.
- **Source counts and tiers.** 8/5/8 sources against the mock's 4/4/3, and
  markedly more `general_web`. The mock was hand-built from four
  carefully-chosen sources per section; a real Tavily pass returns what ranks.
- **Citation markers.** Follows directly from the source counts.
- **Section 3 carries a conflict block the mock does not** — a real MINOR
  disagreement between two Trustpilot figures (4.6 vs 4.5) from different pages.

Read honestly, that is the mock working as specified: **a floor on shape, not a
ceiling on quality.** The shape conformance Phase 7.1 asks for holds. Whether
the substance is better is 7.1a's question, and three observations from reading
the output bear on it — recorded here rather than smoothed over:

- **A MAJOR conflict is present in business model and the band is still HIGH.**
  It did not fire because `score_confidence` counts a conflict only when every
  claim it references scores, and the 49% claim failed quote verification. This
  is the *same* 40%-vs-49% denominator misread Phase 4 disclosed, reaching the
  same correct-by-luck outcome for the same reason. It is rendered — the block
  says MAJOR and says it did not move the band, and the prose reports both
  figures without reconciling them — which is the honest handling of a known
  rough edge, not a fix.
- **Filing-history over-enumeration is still visible**, as expected: 95 claims
  across three sections against the mock's 17. The Phase 4 limitation is
  unchanged and still needs its Phase 2 rendering decision.
- **One extraction-quality slip worth logging for 7.1a:** the prose says
  "full accounts for the year to 1 September 2025", conflating the filing date
  with the period end. That is a claim stating something its source does not
  support — 7.1a's third criterion — and it originates in extraction, not
  synthesis.

**The badge's tier summary describes scoring sources only**, which is why a
badge can say "2 `official_registry`" while the table below lists 3. Deliberate:
the badge describes what the band rests on. Recorded because it reads as an
inconsistency until you know that.

**Carried forward — what Phase 6 needs to know:**

1. **Render `ReportState`, don't re-derive it.** `state.report` is the finished
   markdown; `state.assessments`, `state.claims`, `state.conflicts` and
   `state.decisions` are the deliverable alongside it. The frontend should
   surface the decision log — it is the evidence for the "agent design"
   criterion — and must never recompute a band.
2. **`structural_signature()` is Phase 7.1's diff tool**, already validated
   against the mock and against both providers. 7.1 is a run, not a build.
3. **Synthesis costs three Sonnet calls per report** at up to 6,000 output
   tokens each, on top of extraction's one Haiku call per document. Worth
   knowing before 6.4's cold-start and timeout work: the graph's wall clock is
   now dominated by extraction fan-out plus three synthesis calls.
4. **~161 search credits** remain of the development reserve.
5. **A section can fail without the report failing.** `SectionRender.warnings`
   records stripped citations and failed calls; nothing currently surfaces them
   to the API response, and Phase 6 may want to.

### Pre-merge review — three items, all closed before the merge

**1. Does the raised cap reopen truncation risk on the fallback? Measured, not
assumed — and it found an interaction, running the opposite way.**

The cap was raised to stop Sonnet's default-on adaptive thinking starving
visible output. The obvious worry is the other side: `thinking_budget=0` was set
on Gemini in Phase 3 precisely because a 2.5-generation model spends
`max_output_tokens` on internal reasoning, so a bigger cap might interact with
that. It does not, and the evidence is direct rather than reasoned. The same
reputation prompt was run through the **real router with a failing primary**, so
the fallback was reached via the breaker exactly as an outage would reach it:

| cap | visible chars | prose chars | JSON loads | `gaps` field |
|---|---|---|---|---|
| 2,000 | 2,776 | 2,741 | yes | **absent** |
| 6,000 | 3,553 | 3,110 | yes | present |

**Confirmed independent on the thinking question.** At a 2,000 cap Gemini
produced 2,741 characters of *visible* prose — roughly 700 tokens. That is
impossible if internal reasoning were drawing against the same budget: Phase 3
measured 15 visible tokens at the identical cap with thinking left on. Thinking
is genuinely off, the whole budget goes to visible text, and raising the ceiling
only adds headroom. Gemini's natural output is ~900 tokens, nowhere near 6,000,
so the larger cap introduces no truncation risk on the fallback side.

**But the check found something the raise was not aimed at, and it is worse than
the thing it was aimed at.** At the old 2,000 cap Gemini **dropped the entire
`gaps` array** while still closing well-formed JSON. `SectionProse.gaps` carries
a list default, so that response validated cleanly, and the section rendered
with no evidence-gaps block at all — indistinguishable from a section that
genuinely had nothing to state. The uncertainty block is the one the whole
partial-tolerance design exists to produce, and under budget pressure it is the
first thing a model drops, because it comes last in the response.

So the old cap was degrading *both* providers, in two different and equally
invisible ways: truncating the primary into unparseable JSON, and quietly
shrinking the fallback's honesty. Raising it fixes both. A response that omits
`gaps` entirely is now recorded as a warning, distinguished from an explicitly
empty list, with a test and a control pinning the difference — because the fix
here is not the cap number, which will be wrong again on some future model, but
making the degradation visible when it happens.

**2. Phase 4's entry has an appended addendum** recording that its numbers were
measured while the strict-JSON bug was live, that they were not re-measured, and
that they should be read as a floor rather than as exact. Appended, not edited,
per this log's own rule.

**3. Date conflation in filing-history extraction — a new finding, disclosed.**

Distinct from the two limitations already on record, and worth separating from
the enumeration problem it shares a document with. In the Phase 5 live run one
claim read *"full accounts for the year to 1 September 2025"*. 1 September 2025
is when those accounts were **filed**; the period they cover ends in February.
The claim states something its source does not support — Phase 7.1a's third
criterion — and it does so in the field where it matters most to an underwriter,
since the period an account covers is what makes a figure current or stale.

**Mechanism, located precisely.** A filing row renders as
`2025-09-01 — accounts — Full accounts made up to 2025-02-28`. The leading date
is **the only unlabelled date in any register document**; its meaning is
positional convention. The period end is present, but as prose inside the
description. Every other register date carries its label inline —
`appointed …` / `resigned …` on officers, `notified …` / `ceased …` on PSC,
`incorporated: …` on the profile.

**Is it systematic? Yes, but narrowly, and the boundary is the useful part of
the answer.** It does *not* generalise across Companies House metadata: no other
register document has an unlabelled date for a model to re-label. It does
generalise *within* filing history, where every accounts and confirmation-
statement row carries the same two-date shape — so this is a property of that
document, not a one-off misread of one row. Checked by reading
`registry_documents` rather than by inference.

That makes the filing-history document responsible for two of the three
disclosed limitations, with the same root cause in both: how the document is
rendered, not how the prompt is written. The extraction prompt already spells
out the correct handling in its own worked example — *"most recently full
accounts for the year to 28 February 2025, filed on 1 September 2025"* — and the
model merged them anyway on real data. Same pattern as the enumeration finding:
a stated instruction that does not survive the document's shape.

**Not fixed under this timeline**, and disclosed in README §5 to the same
standard as the other two. Labelling the filed date at render time is a small
change and would likely close it, but it changes what an evidence document
contains, which is Phase 2's decision to make — the same decision the
enumeration limitation is already waiting on. Both should be taken together.

**One related absence, recorded so it is not mistaken for the same defect:** the
register's *next accounts due* date is not rendered into any document, so the
system cannot claim anything about filing deadlines even though the register
holds one. The Phase 0.7 mock cites exactly that fact. That is missing evidence,
not wrong evidence, and it is a gathering gap rather than an extraction one.

---

## Phase 6 — API, Frontend, Deployment

**Status:** complete. 6.1, 6.2, 6.3, 6.4, 6.4a and 6.5 all done. Built on branch
`phase-6-deployment`, cut from `main` at `aff6107`. Commits `1de9a11`,
`8069a56`. **Not merged: awaiting sign-off.**

**Test status: 422 tests, all passing** (up from 415). `ruff check` and `ruff
format --check` clean. No live API call anywhere in the suite — see finding 1,
which is the reason that sentence needed re-earning this phase rather than being
inherited.

**Built:** the last mile. `src/api/report.py` puts the Phase 3 graph behind the
Phase 0.5 gate; `frontend/app.py` is a Streamlit client for it;
`.github/workflows/keep-warm.yml` closes 6.4a; the README is written; and
revision `groundwork-00004-2jv` is live at
`https://groundwork-213351312105.europe-west2.run.app`.

**What was already proven and deliberately not rebuilt.** The Dockerfile,
Secret Manager wiring, the least-privilege runtime service account, the
`.gcloudignore` build context, `--max-instances=2`, 1Gi memory and startup CPU
boost were all stood up during Phase 3's parallel deployment task and were
correct. 6.3 was therefore verified rather than built: Cloud Build logged
"Building using Dockerfile", not the Buildpacks fallback that a wrong `--source`
path silently produces. The only infrastructure change this phase is the request
timeout, 300s → 600s, sized against Phase 5's carry-forward that the wall clock
is extraction fan-out plus three synthesis calls at up to 6,000 output tokens.

**A stale-revision check worth keeping.** The deployed container was still
running pre-Phase-3 code — `/health` reported `global_budget: 130` against the
repo's 80 since Phase 3's credit recalculation. Neither TASKS.md nor this log
recorded that, and it was found by reading `/health` rather than trusting the
deployment note. The 130 → 80 flip then served as free proof that the new
revision was actually serving, rather than a cached one answering.

**The four decisions worth being able to defend:**

- **Only `PROVIDER_FAILURE` is an HTTP error.** `NEEDS_INPUT`, `NOT_FOUND` and
  `NO_EVIDENCE` are 200s carrying a typed outcome. A company that cannot be
  found is a *finding* about that company, and disambiguation is the product's
  best feature — rendering either as a 4xx would make a client display them as
  faults. `PROVIDER_FAILURE` gets a 503 for the reason `src/config.py` exists:
  "our providers are down" must never reach a reader wearing the same clothes
  as "this company is thinly documented". A provider failure still consumes
  rate-limit budget, pinned by its own test — it cost real calls, and refunding
  it would make the one path most likely to be retried also free.
- **No progress streaming**, though 6.1 offers it. The decision log delivers
  what a progress bar would and delivers it auditably, after the fact, including
  the steps where the agent decided to do nothing. Deferred explicitly and
  recorded in README §5 rather than quietly skipped.
- **The frontend renders and never computes.** Bands, badges, citations and
  source tables arrive as finished markdown from Phase 5's render-from-data
  layer. Disambiguation closes the loop — selecting a candidate re-submits by
  company number, which Phase 1.1 normalises — rather than listing candidates
  and making the user retype the name, which would be a screenshot of the
  feature rather than the feature.
- **The frontend is not separately deployed.** It runs locally against the
  hosted API; the hosted demo is the Cloud Run URL. A second hosting account, a
  second secrets path and a second cold start, on submission eve, for a UI the
  brief says not to polish, was not worth it. `.gcloudignore` already excluded
  `frontend/`, so no build-context change was needed.

**Deviations and additions beyond the approved proposal:**

1. **`get_report_runner`, a dependency seam not in the proposal.** Reasoning
   under finding 1 — it was a correctness problem, not a refactor.
2. **`SectionRender.warnings` is still not surfaced.** Phase 5 carried this
   forward and the approved proposal said it would be done if it proved small.
   It does not: `render_report` returns a string, so the warnings are discarded
   at the `Synthesizer` protocol boundary, and surfacing them means changing a
   Phase 3 seam, a Phase 5 module, the graph node and their tests. Flagged and
   left rather than absorbed, per the same proposal's commitment to say so
   instead of letting it eat Phase 7 time. **Still carried.**

---

### Finding 1 — the endpoint could not be tested without a network, and the suite proved it

The first version of `create_report` built its dependencies and called
`run_report` inline. It has no seam, so `tests/test_api.py` — which had been
asserting the 501 stub since Phase 0 — ran the real graph, made a live
Companies House request, and got back a genuine `401`.

That is a direct breach of AGENTS.md's rule that no unit test reaches an
external API, and CI runs deliberately without secrets so it would have failed
there too. It is also the *third* time this project has learned that a
boundary is only real if something structural enforces it: Phase 4 made a
fabricated citation unrepresentable rather than discouraged, Phase 5 made the
report's shape a property of the code rather than of prompt compliance, and
here the fix is the same shape — a `get_report_runner` dependency the tests
override, plus a test asserting the default runner is still
`run_with_real_dependencies`, so the override cannot quietly become the
production path.

Worth noting what made it visible: the Phase 0 stub tests. They existed only to
assert a 501 and had no further purpose, and they were the thing that caught
this.

---

### The live pass — one cold request through the deployed service

Per the approved plan, exactly one live pass, and the cold-start measurement
was the reason for its ordering. **The service had been warm through every dev
session, so cold-start behaviour had never actually been measured** — 6.4 asks
for it explicitly and 6.4a's mitigation is sized against it.

Method: deploy, verify, then leave the service untouched for ~20 minutes so the
container passed its scale-to-zero window, then issue a cold `POST /report` for
"Monzo Bank Ltd" as the first traffic to arrive. Confirmed genuinely cold from
Cloud Run's own logs — a fresh application startup at 16:42:08 UTC, twenty
minutes after the previous container's at 16:21:59. Real token, real gate, real
graph, real report. **7 Tavily credits; ~161 → ~154 of the development
reserve.**

| | |
|---|---|
| Cold first request, end to end | **85.3s**, HTTP 200, 69.5 KB |
| ├ container cold start | ~13s (request arrival → app ready, from log timestamps) |
| └ agent run | ~72s (app ready → `run finished`) |
| Warm `/health` after | 0.09s |

**The cold container is the small half.** ~13s of the 85s is the platform; ~72s
is the agent doing its work, and that cost is identical warm. Worth recording
because the Round 1 council finding that produced 6.4a assumed the reverse — it
warned of "a cold container plus a 30-90s agent run" as a compounding problem,
and the compounding turns out to be roughly 15%. The keep-warm ping is still
worth having, but the README now states the honest figure rather than implying
the ping is what makes the demo responsive.

**What held.** Resolution, both gathers, extraction across 19 documents, the
3.5 re-search branch, synthesis and rendering all ran end to end in the
container for the first time. Outcome `complete`, 109 claims, 101 scoring, 14
decisions, a 20,075-character report. The gate returned 401 unauthenticated.
Quote verification excluded 8 of 109 (7.3%), well below Phase 4's 18% — not
comparable as a measurement, since Phase 4's figure was taken while the
strict-JSON bug was live and against different documents, but it is at least
not worse.

**Filing-history over-enumeration did not dominate this run.** The register
profile contributed 6 claims and no register URL appeared in the top sources.
That is not evidence the disclosed limitation is fixed — it is unfixed, and
untouched this phase — only that it did not manifest severely here.

---

### The finding this run produced: the disclosed conflict misclassification moved a band, and re-search made it worse

Phase 4 disclosed that conflict severity is judged by a model and that it
misreads figures which differ by *basis* rather than by contradiction. Phase 4
and Phase 5 both recorded the same caveat: the outcome had been correct **by
luck**, because the offending claim had already been excluded from scoring for
an unrelated reason and the rule only counts a conflict when every claim it
references scores. **That luck ran out here, on the deployed service, on the
run the README links to.**

Competitive landscape scored **LOW on three MAJOR conflicts**, and reading them,
all three are the same misreading:

- a $5.2bn valuation against a $5bn Series I round;
- that Series I against a later employee secondary sale at just under $6bn;
- a $501m raise against a $600m one.

Different instruments at different dates. All can be true at once. None is a
contradiction of a core fact, which is what the 5.4 definition requires for
MAJOR. Every claim involved had scored, so the conflict counted, and the band
went to LOW.

**The second-order effect, which nobody predicted and which cuts against the
system's own design.** The decision log records the whole sequence, which is
what made it readable at all:

1. First pass: competitive landscape scores **LOW (weak_sources, 16 of 16
   claims)** — the section had drawn SEO content farms, exactly the failure
   Phase 2 recorded when it chose per-section search depths.
2. The 3.5 branch fires, correctly, because that is LOW *for a reason more
   evidence could fix*. Refined query: *"Quantified market share data and
   competitive positioning metrics from financial services analysts"*. 5
   results, 2 credits, at the section's own depth.
3. Second pass: 27 new claims, 16 → 42 scored. **The weak-sources problem is
   genuinely solved.**
4. And the section still scores LOW — now for `major_conflict`, because the new
   evidence brought the valuation figures that the classifier misreads.

**More evidence lowered the confidence.** The agent's adaptive re-search and
the conflict classifier are currently working against each other: the re-search
does its job, and the classifier converts the reward into a penalty. The log
also records the run declining to re-search again, because the loop is bounded
at one pass — so the bound behaved correctly, and is the only reason this did
not repeat.

**Not fixed, and the reasoning for not fixing it is unchanged rather than
convenient.** The direction is still safe: a false MAJOR can only understate
confidence, never inflate it, and both sides of every disagreement are printed
for the reader. The fixes were already considered and rejected on evidence in
Phase 4 — refusing conflicts between figures with different units misses most
real conflicts, and requiring two independent classification passes doubles the
cost of every report. What this run adds is *evidence about how much it
matters*: a section that had been rescued by the agent's best feature was
dragged back down by it. README §5 has been updated from "in that run it
changed nothing" to record that it has now changed something, because the
earlier wording would read as understatement to anyone who ran the demo.

**This is the strongest argument in the repo for the second-pass fix that Phase
4 sketched** — a classification pass required to state the *basis* of a
disagreement, giving a denominator or instrument mismatch somewhere to be
declared. Recommended for the design note's "what would change with more time",
where it should now outrank the filing-history rendering fix.

---

**Carried forward — what Phase 7 needs to know:**

1. **The deployed revision is `groundwork-00004-2jv` and it is current with this
   branch.** If anything merges after this, redeploy — and check
   `/health`'s `global_budget` to confirm the new code is serving, which is how
   the stale revision was caught this phase.
2. **7.1's four runs still need credits.** ~154 of the development reserve
   remain; the four scenarios cost roughly 5-10 each. The live pass above is a
   fifth data point for the well-documented case, so 7.1 may not need to
   re-run Monzo — but it is one run, not the structural diff, which is still
   outstanding.
3. **`structural_signature()` remains 7.1's diff tool**, unchanged and still
   validated. The cold-run report markdown is saved outside the repo if 7.1
   wants to diff this exact run.
4. **Three limitations are disclosed in README §5, and one of them just got
   worse.** The conflict misclassification is now observed moving a band on the
   deployed service; the filing-history enumeration and date-conflation pair
   are unchanged and still awaiting the Phase 2 rendering decision they were
   always waiting on.
5. **`SectionRender.warnings` is still unsurfaced** (deviation 2 above).
6. **The keep-warm cron has never actually run on a schedule.** It was
   committed this phase and `workflow_dispatch` is available, but GitHub only
   begins honouring `schedule` once the workflow is on the default branch —
   so it does nothing until this branch merges. Worth a manual dispatch after
   the merge to confirm it goes green rather than assuming it.

---

## Phase 7 — Testing, Design Note, Audit

**Status:** complete. 7.1, 7.1a, 7.2, 7.3 and 7.4 all done. Built on branch
`phase-7-final`, cut from `main` at `bbd229e`.

**Test status: 422 tests, all passing** (unchanged — this phase added no
production code). `ruff check` and `ruff format --check` clean.

**Built:** no source changes. `DESIGN_NOTE.md`,
`docs/phase-7.1-run-results.md`, `docs/phase-7.3-self-audit.md`, and README
corrections. Phase 7 was a validation-and-articulation phase, and the fact that
it produced no production diff is itself worth recording: four correctness
issues were found and all four were disclosed rather than patched, for the
reasons given in each case below.

**Note on Phase 6's entry:** it says "Not merged: awaiting sign-off". It merged
as `bbd229e`. Recorded here rather than edited there, per this log's own rule.

---

### The four scenarios — 12 Tavily credits, ~154 to ~142

Scenario selection was itself evidence-driven, which is worth keeping. A
**zero-credit reconnaissance pass** resolved a dozen candidate queries against
Companies House alone before any search credit was spent, so the thin-evidence
and ambiguous-name companies were chosen on what the register actually returned
rather than guessed at.

| Scenario | Query | Outcome | Credits | Time |
|---|---|---|---|---|
| Well-documented | `Monzo Bank Ltd` | `complete` | 7 | 62.3s |
| Thin evidence | `Ashgrove Bespoke Cabinetry` (16259703) | `complete` | 5 | 43.4s |
| Ambiguous | `Sterling Services` | `needs_input`, 8 candidates | 0 | 0.2s |
| Invalid | `!!!` | `invalid_input` | 0 | 0.0s |
| Malformed | `12345678901234567890` | `not_found` | 0 | 0.0s |

Full analysis in `docs/phase-7.1-run-results.md`. **The structural diff against
the Phase 0.7 mock passes: 11 differences, none of them shape.** Section count,
order, headings, badges, gaps blocks, sources tables, exclusion notes, the
three-row working table and the absence of trailing content all match. The 11
are band, one conflict block, and evidence volume — the mock working as
specified, a floor on shape rather than a ceiling on quality.

**7.1a: criteria 1, 2 and 4 pass; criterion 3 fails on the thin-evidence run**,
via finding 1 below.

---

### Finding 1 — a fourth limitation: web evidence attributed to a same-named different company

The thin-evidence company (ASHGROVE BESPOKE CABINETRY LTD, registered office
Tonbridge, Kent) drew web evidence from at least three different UK businesses —
a Glasgow cabinetmaker, a Devon joinery firm, and a site stating East Sussex —
and extraction attributed all of them to the subject.

**Zero entity warnings fired, and the reason is structural rather than a tuning
miss.** `names_a_different_entity()` was built in Phase 2 after a Better
Business Bureau page for *Monzo, Inc.* reached a UK report. It keys on the
subject's distinctive name tokens, and every one of these sources shares
"Ashgrove". Phase 2's other half — UK-anchoring every query — does not help when
all three confusable entities genuinely are UK companies.

**The property that makes it matter: the failure is correlated with thin
evidence.** A well-documented company dominates its own search results; a
micro-business with a generic name does not, so the ranking fills with
same-named others. The mode bites hardest exactly where an underwriter most
needs care.

**What contained it, recorded because it is the honest half.** The report did
not state the conflated facts confidently — it surfaced the contradiction,
printed both locations, and said the evidence did not allow either to be
confirmed. The uncertainty machinery bounded a grounding failure it was not
designed to catch. That is reassuring about the architecture and is not a reason
to leave the defect.

**Disclosed, not fixed**, as an explicit decision. The fix — requiring identity
corroboration before a web document may contribute *scoring* claims — is
data-gathering-layer work, the same layer and the same pending decision the two
filing-history limitations have been waiting on since Phase 4. Consistent with
how every prior correctness-adjacent finding was handled under this timeline.
README §5 now carries four limitations, not three.

---

### Finding 2 — the predicted filing-row exposure materialised, and was masked by finding 1

Phase 4 predicted it exactly: *"nine filing rows alone clear HIGH's `>= 3` gate
with an `official_registry` source present, so a business with no press coverage
and no regulatory history could report HIGH confidence on the strength of having
filed its accounts."*

Ashgrove's business model section drew **11 scored `official_registry` claims**,
almost all register enumeration. That clears the gate. **Under the rule, that
section scores HIGH.** It reported LOW only because finding 1's contradictions
tripped `major_conflict`, which is checked first.

**Two defects cancelled.** Had the web search returned nothing — entirely
plausible for a company this small — a 2025-incorporated joinery micro-business
with no trading history would have reported HIGH confidence on its business
model. This is the clearest example in the project of something that ships
looking correct.

Also observed, and previously predicted as unhandled: *"The company was
incorporated on 18 February 2025"* appeared **twice** as two scored claims, from
the profile and the filing-history documents. Different URLs, different ids, so
`merge_claims` keeps both. Phase 4 flagged cross-document duplication as
unhandled and said it would inflate a count if it ever reached scoring claims.
It has now.

---

### Finding 3 — the conflict classifier emits self-refuting conflict records

The sharpest result of this phase, and it **makes the recommended fix cheaper
than Phase 4 estimated**.

Monzo's business model is capped at MEDIUM by a MINOR conflict whose own detail
text reads: *"These claims describe two separate foreign transaction charges (£5
for card delivery and 3% for card usage abroad) **rather than conflicting with
each other**, so they represent different fees rather than a genuine
disagreement."*

The classifier emitted a conflict record explaining, in the same field, why the
two claims are not in conflict — and the band moved anyway. The thin run holds a
second instance. `score_confidence()` reads only `severity`; `detail` is
rendered to the reader and never inspected. **That self-refuting MINOR is the
sole reason Monzo's business model scored MEDIUM rather than HIGH.**

Phase 4 proposed the fix as a second classification pass required to state the
basis of the disagreement, and rejected it as doubling classification cost on
every report. This run shows **the model already volunteers the basis,
unprompted, in a field that already exists**. The fix is therefore closer to
making an existing signal load-bearing than to adding a pass — require the basis
as a structured field, and let a declared basis mismatch downgrade or discard
the conflict. Recorded as the design note's first "what would change with more
time" item.

**Further evidence against prompt-tuning as the fix:** the same $5.2bn-vs-$5bn
valuation pair that Phase 6 classified MAJOR was classified **MINOR** this run.
Same pattern, same prompt, different severity — the classification is not stable
across runs.

---

### Finding 4 — the Phase 6 second-order effect reproduced independently

Monzo's competitive landscape repeated Phase 6's sequence on different evidence:
first pass LOW, the 3.5 branch fires correctly and re-searches at the section's
own depth, 45 scored claims across 7 sources, and the section still finishes LOW
— now on `major_conflict` (user counts of 10m vs 6.5m vs 9m; "largest
digital-only bank" vs "7th largest UK bank", which are different dates and
different denominators).

**More evidence lowered the confidence, again.** Observed twice now, on two
independent runs, one of them the deployed service. No longer an anecdote, and
the design note leads its final section with it.

---

### Finding 5 — a README accuracy defect, found by running the scenario

README §7 claimed *"`!!!` or a 20-digit number is rejected before any network
call."* The second half is false. `_validate_query` rejects empty, over-length,
non-alphanumeric and single-character input; a 20-digit number passes all four,
fails the company-number pattern, and is searched as a name, returning
`NOT_FOUND`. The outcome is honest, but it is not `INVALID` and it is not free
of a network call. Corrected in 7.4. Worth recording as the class of error only
a run finds — the behaviour was always correct, only the documentation was
wrong.

---

### One extraction failure, logged

The Monzo officers document returned `no JSON object in response` and
contributed no claims to that run. Per-document isolation meant one bad response
cost one document rather than the report, which is correct — but it is invisible
in the output, the same criticism Phase 5 made of the strict-JSON bug.

---

### 7.4 — repo and history audit

- **Secret history clean, checked against past commits rather than current
  files.** `.env` was never tracked at any point in the 46-commit history; no
  secret-shaped file was ever added; a content scan of every commit's diff for
  Anthropic, Google, Tavily, GitHub and PEM private-key patterns returns
  nothing. The only credential-shaped line ever committed is `.env.example`'s
  `DEMO_ACCESS_TOKEN=generate-a-long-random-string` placeholder. CI carries no
  secrets, and the keep-warm workflow deliberately hits the unauthenticated
  `/health` so that it needs none.
- **Commit history reads cleanly** — conventional commits, one phase per merge,
  phase numbers in the messages.
- **README:** the §7 defect corrected, the fourth limitation added to §5, a
  design-note section added, and the closing document table rewritten for an
  external reader.
- **`AGENTS.md` / `CLAUDE.md` reworded.** Both were written in a live build-time
  instruction voice ("wait for explicit confirmation before writing code",
  "before starting Phase 0") that reads oddly to an evaluator opening the repo
  after the fact. Rewritten as repo conventions plus a plain statement that
  Claude Code was the implementation tool and what process governed it. The
  disclosure is kept and made more direct rather than removed, and the hard
  boundaries are unchanged.

---

**Carried forward — what a future session would need to know:**

1. **Four limitations are disclosed in README §5**, and three of them wait on
   the same decision: what the data-gathering layer renders into an evidence
   document, and what it must verify before a document may contribute scoring
   claims. Filing-history rendering, date labelling and web-entity corroboration
   should be taken together.
2. **The conflict-basis fix (finding 3) is the highest-value change in the
   repo**, and it is cheaper than previously estimated, because the signal is
   already present in `Conflict.detail` and merely unread.
3. **`SectionRender.warnings` is still unsurfaced.** Carried since Phase 5.
4. **~142 search credits** remain of the development reserve.
5. **The deployed revision predates this branch.** Nothing in Phase 7 changed
   `src/`, so the running service is functionally current; redeploy only if a
   later change touches source, and confirm via `/health` that the new code is
   serving.

---

### Addendum, appended 3 August 2026 — post-submission-audit deploy, revision `groundwork-00005-scr`

**Nothing above has been edited.** This supersedes carry-forward item 5: a later
change did touch `src/`, so the redeploy that item made conditional has now
happened.

**Revision `groundwork-00005-scr`, source commit `157df5f`, created
2026-08-02 23:48:33 UTC**, serving 100% of traffic. Cloud Build logged "Building
using Dockerfile", not the Buildpacks fallback. The previous revision was
`groundwork-00004-2jv`, the one the Phase 6 entry records. Revision config was
captured before and after and is unchanged: the `groundwork-run` service
account, 600s timeout, 1Gi/1 CPU, `--max-instances=2`, startup CPU boost, and
all five Secret Manager bindings.

**Deploy-target discipline, worth restating because the ambient config is a
trap.** The machine's default `gcloud` project is the developer's unrelated
startup project. Every call passed `--project=groundwork-demo-765584`
explicitly, and the source was given as an absolute path rather than `--source=.`
— the repo root sits one level below the working directory, and the wrong level
silently falls back to Buildpacks. Both are the traps the Phase 3 deployment
skeleton already paid for.

**What this revision ships that `00004-2jv` did not:**

1. **Citation titles are escaped, collapsed and length-capped**
   (`src/report/citations.py::clean_title`). Retrieved titles were interpolated
   into the source table raw, so a title containing `|` split its own row: with
   four header columns, GFM dropped the surplus and the Phase 2.3a authority
   tier rendered in the retrieval-date column. That tier is what the confidence
   rule scores on, so it was a grounding-display error rather than a cosmetic
   one. It affected 6 of 23 source rows in the Phase 6 cold-run capture.
2. **Two disclosures**, no behaviour change: README §5 and `DESIGN_NOTE.md` §4
   now state that a section's write-up can fail while its band, sources and gaps
   still render — designed and covered by three tests, but previously undisclosed
   to a reader. Both are tied to the Phase 6 run discussed above, where Section 3
   carries the placeholder.
3. Documentation-consistency fixes (`f60018e`) and the Streamlit disambiguation
   fix (`8b436c9`). **The frontend fix is in the repo but not in this image** —
   `.gcloudignore` excludes `frontend/`, which remains correct: the hosted demo
   is the API, and the Streamlit client runs locally against it.

**Verification, on the hosted URL rather than locally.**

- `/health`: 200 in 0.31s, `global_budget: 80` — the same discriminator that
  caught the stale revision in Phase 6, confirming the new code is serving.
- One real `Monzo Bank Limited` report through the deployed endpoint with the
  real token: **HTTP 200 in 49.3s**, 61 KB, 87 claims, `outcome: complete`,
  **5 Tavily credits**.
- **20 of 20 source rows parsed correctly** — every row's Type column holds a
  valid authority tier and its Retrieved column a valid date, checked
  positionally rather than by eye. **5 rows contain escaped pipes**, so the fix
  fired on freshly retrieved data, including a LinkedIn result that did not
  exist in the Phase 6 capture. No mojibake; citation markers [1]–[8]
  contiguous.
- Post-run `/health` confirms the work was real rather than cached: both source
  breakers present and `closed`, `credits_this_container: 5`, `global_used: 1`.

**The bands moved against the Phase 6 capture, and this is not a regression.**
That run scored MEDIUM / LOW / HIGH; this one scored MEDIUM / HIGH / LOW —
competitive landscape rose and reputation fell. Same company, different
retrieved evidence, and a different call from the conflict classifier, whose
instability is already on record: Phase 7 finding 3 recorded the *same*
$5.2bn-vs-$5bn valuation pair being classified MAJOR in one run and MINOR in
another. Nothing in this deploy touches scoring — `confidence.py` is unchanged
since Phase 3 — and the rendering fix cannot move a band, since it operates on
the title string after scoring is complete. Recorded here so a later reader
comparing the two runs does not read run-to-run variance as a fault introduced
by the redeploy.

**~132 → ~127 search credits** remain of the development reserve.

---

### Addendum, appended 3 August 2026 — demo token rotated, revision `groundwork-00006-gbb`

**Nothing above has been edited.** Second deploy of the day, and the last one
before submission.

**The scan came back clean, and the rotation happened anyway.** Before changing
anything, the live `DEMO_ACCESS_TOKEN` value was searched for across the entire
history rather than just the working tree — `git log --all -S<value>` returned
0 commits, and `git grep -F <value>` across all 53 commits returned 0 matches.
The token had never been committed, and README already used a `<token>`
placeholder. So there was nothing to redact. It was rotated regardless, on the
reasoning that a credential handled repeatedly across development sessions
before being published to an evaluator is worth replacing on principle, not only
in response to a known leak. Phase 1 recorded three keys reaching session
transcripts during live testing; that history is the argument.

**Ordering was deliberate.** README was committed first (`bd4094a`, mechanism
only — the token is provided separately, held in Secret Manager, injected at
runtime, in neither the repo nor the image), and only then was the value
rotated. Reversing those two would have briefly published a fresh token
alongside a README that still pointed at it.

**Revision `groundwork-00006-gbb`, source commit `bd4094a`**, serving 100% of
traffic, built from the Dockerfile. `demo-access-token` version 3 was added
while versions 1 and 2 were left enabled, so nothing depended on the new value
until it was proven to work.

**Verified in this order, and the third check is the one that matters:**

- `GET /health` — 200 in 0.31s, `global_budget: 80`, breaker closed.
- `POST /report` with the **new** token — **200**, 49.4s, 58.7 KB, a real
  MONZO BANK LIMITED report: 79 claims, 5 Tavily credits, outcome `complete`.
- `POST /report` with the **old** token — **401** in 0.07s,
  `"Missing or invalid access token"`. Rejected before the rate limiter, so a
  stale credential cannot burn demo budget.

Only after all three passed were versions 2 **and 1** disabled — version 1 was
an even older enabled version left over from the first Secret Manager wiring on
1 August, and it had no business still being live. Disabled rather than
destroyed, so a rollback remains possible during the review window; both should
be destroyed once the window closes. `/health` was re-checked afterwards and
still answers 200.

**~127 → ~122 search credits** remain of the development reserve.

**Carried forward, and these are the only two open items:** the keep-warm cron
has still never been observed running on a schedule, and the repository is still
private, so the hosted-demo link is of no use to a reader until it is made
public or the evaluator is added as a collaborator.
