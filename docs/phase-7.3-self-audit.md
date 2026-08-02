# Phase 7.3 — Self-audit against the brief's six evaluation dimensions

An explicit checklist. Each claim cites where it is satisfied in the codebase —
a file, a function or a named test — rather than asserting it. Where a dimension
is only partly satisfied, that is stated with the gap, because a self-audit that
scores itself full marks on every line is not evidence of anything.

**Baseline as audited:** 422 tests passing, `ruff check` and
`ruff format --check` clean, ~8,500 lines of Python across `src/` and
`frontend/`, 46 commits.

---

## 1. Agent design

*Is this a genuine agent — making data-dependent decisions with inspectable
control flow — or a pipeline with an LLM in it?*

**Satisfied, with one honest reduction in scope.**

| Claim | Where |
|---|---|
| Explicit state machine, not a linear chain | `src/agent/graph.py` — `build_graph()` wires nodes and conditional edges over `ReportState` |
| A genuine cycle, exited by a state counter rather than a missing edge | `evaluate → research_gap → extract → evaluate`; bound is `ReportState.may_research()`, with `RECURSION_LIMIT` as a backstop for a counter bug |
| The branch fires on a *measured* judgement, using the same function the report publishes | `src/agent/nodes.py` calls `score_confidence()` — the identical function behind every printed band, not a parallel heuristic |
| The re-search decision is data-dependent, not scheduled | `ConfidenceAssessment.re_search_worthwhile` — LOW **and** the reason is in `_RECOVERABLE` |
| A contradicted section is deliberately *not* re-searched | `_RECOVERABLE` excludes `MAJOR_CONFLICT`; `tests/test_graph.py::test_a_contradicted_section_is_not_re_searched` |
| The refined query is mechanically validated before a credit is spent | `src/agent/research.py`; `tests/test_research.py::test_a_paraphrase_of_the_original_is_rejected`, `::test_a_query_that_drops_the_company_name_is_rejected`, `::test_an_overlong_query_is_rejected` |
| Restraint is logged, not just action | `tests/test_graph.py::test_the_negative_decision_is_logged_for_every_section` |
| Failure branches are visible in the graph | `halt` terminal for every non-report outcome; `tests/test_graph.py::test_every_non_resolved_kind_maps_to_its_own_outcome` |
| Concurrency is structural, not incidental | `gather_registry` and `gather_web` are two real nodes, so the per-source failure branch is inspectable |
| Degradation never masquerades as a finding | `tests/test_graph.py::test_an_llm_outage_during_extraction_is_not_reported_as_thin_evidence` |

**The gap, stated plainly.** Phase 3.5 was specified as an adaptive re-search
*loop* and was reduced to a **single bounded pass** — one retry per
under-evidenced section, no iterative refinement — under timeline pressure. The
three properties that make it agentic rather than a retry were kept and each is
tested. It is genuinely one pass, and the design note says so.

**Evidence it is more than architecture-on-paper:** the live decision logs show
the branch firing correctly on real data, producing a query the model justified
as targeting *"official regulatory and ombudsman records rather than consumer
review platforms"* (token overlap 0.19 with the original), and the log records
the run then *declining* to re-search again because the bound had been reached.

---

## 2. Data grounding

*Can every claim be traced to a real source, structurally rather than by
prompting?*

**Satisfied, and this is the dimension the architecture is most deliberately
shaped around.**

| Claim | Where |
|---|---|
| A citation cannot be fabricated, because the model is never shown a URL | `src/agent/extract.py` — one call per document; `source_url`, `source_type`, `section`, `origin` attached by code from the `TaggedDocument` |
| The same guarantee one layer up, at synthesis | `tests/test_synthesize.py::test_the_prompt_never_contains_a_source_url` |
| A marker the model invents anyway is stripped rather than rendered | `tests/test_synthesize.py::test_a_fabricated_marker_is_stripped_from_the_rendered_report` |
| Traceability is a property of the type, not a convention | `EvidenceClaim` requires `source_url`/`source_type`/`section` at construction — an unciteable claim is unrepresentable |
| Every claim carries a verbatim quote, checked against the source text | `tests/test_extract.py::test_a_verbatim_quote_is_grounded`, `::test_a_fabricated_quote_is_not_grounded`, `::test_typographic_differences_do_not_fail_a_real_quote` |
| Source authority is a mechanism, not an adjective | `src/data_sources/sources.py::classify_source()` — five tiers, with `official_registry` / `official_self` split so a regulator and a company's own account are not equal |
| A lookalike domain cannot inherit authority | `tests/test_sources.py` — `notfca.org.uk` case |
| Foreign-affiliate evidence cannot be silently absorbed | `entity_warning` enforced in code: warned documents' claims are unconditionally non-scoring with the mismatch written into the claim text, pinned by a test feeding a maximally uncooperative model response |
| Citations point at pages a human can read | Register citations use `find-and-update.company-information.service.gov.uk`, not the JSON API |

**The gap, and it is real.** Phase 7.1 found that the entity-identity guarantee
holds against a *differently named* entity and fails against a *similarly named*
one: three different "Ashgrove" businesses were attributed to one subject
company, with no warning fired, because the mechanism keys on distinctive-token
mismatch. Grounding to *a real source* held perfectly — every citation resolves.
Grounding to *the right company* did not. Disclosed as a fourth limitation;
design note §5.3.

---

## 3. Uncertainty handling

*Is uncertainty a mechanism visible in the output, or a disclaimer?*

**Satisfied, and it is the product's headline feature rather than a caveat
appended at the end.**

| Claim | Where |
|---|---|
| The band is an explicit function with defined precedence | `src/report/confidence.py::score_confidence()` — 18 tests in `tests/test_confidence.py` |
| Precedence is load-bearing and was a caught bug | MAJOR checked before any count; MINOR caps at MEDIUM *before* the count-and-authority gate, which was dead code in an earlier draft |
| HIGH requires real authority, not just volume | `_meets_authority_gate()` — a company's own disclosure counts only if the section also holds a non-self claim |
| The reason is carried, not just the band | `ConfidenceReason` — needed by two consumers: the badge, and the re-search decision |
| The badge cannot drift from the score | Badge and working table both print from the same `ConfidenceAssessment`; `_working_row` reads `assessment.total_claims`, never re-counting the list |
| The API cannot recompute a band | `tests/test_api.py::test_the_api_never_recomputes_a_band` |
| Conflicts are surfaced, not resolved | Both sides rendered; a conflict can be shown without moving a band, by design |
| Excluded claims stay in the report with their reason | Otherwise the report looks thinner than the evidence and the reason is invisible |
| Gaps travel with the evidence | `EvidenceBundle` carries `GatherFailure`s; a failing source becomes a printed gap |
| A degraded model provider never reads as thin evidence | `PROVIDER_FAILURE` → 503, and `src/config.py` rejects placeholder credentials at startup for the same reason |
| A section can fail without the report failing | Renders its real band, sources and gaps, and says plainly that the *prose* is what is missing |

**Verified in output, not just in tests.** The Phase 7.1 thin-evidence report
carries all-LOW bands, a section with 6 claims from 1 source that says so, and
prose that declines to characterise competitive intensity at all. Side by side
with Monzo's report (107 claims, 22 source rows, mixed bands) the difference is
unmistakable.

**The gap.** Two of the six section-bands in Phase 7.1 were correct *for the
wrong reason* — the rule behaved exactly as specified on inputs that were wrong.
That is a grounding failure surfacing as an uncertainty artefact, and it is
argued honestly in `docs/phase-7.1-run-results.md` §4 rather than presented as
the mechanism working.

---

## 4. Product thinking

*Would an underwriter actually use this, and does it fail in ways that respect
the reader?*

**Satisfied.**

| Claim | Where |
|---|---|
| The output is shaped to the job | Three sections matching the brief; a badge stating band, reason and claim count; per-section sources; conflicts; gaps; a working table |
| Uncertainty is expressed in the reader's terms | "held at LOW by a MAJOR conflict between scored claims — the sources contradict each other on a core fact, and the disagreement is reported rather than resolved" |
| Disambiguation is the product, not an error path | `NEEDS_INPUT` is a `200` with candidates carrying status, SIC, incorporation date and address — enough to actually choose; `tests/test_api.py::test_needs_input_is_a_success_with_candidates` |
| Selecting a candidate closes the loop | Frontend re-submits by company number, which resolution normalises — not "retype the name" |
| "Not found" is a finding about a company, not a client error | `tests/test_api.py::test_not_found_is_a_finding_not_an_error` |
| The one real fault is distinguishable | `tests/test_api.py::test_provider_failure_is_the_one_http_error` |
| A retryable failure is not also free | `tests/test_api.py::test_a_provider_failure_still_consumes_budget` |
| Cost/latency characteristics are disclosed with measured numbers | README §8: cold first request **85.3s** (~13s container, ~72s agent), warm `/health` 0.09s |
| The keep-warm mitigation is described as a mitigation | Its two real failure modes (best-effort scheduling, 60-day auto-disable) are stated |
| Rate limits are explained rather than imposed | README §7 gives the reason and the arithmetic behind each number |
| The evaluator is told what to test and why | README §7 and design note §6 both point at disambiguation and the thin-company comparison first |

**Judgement worth noting:** the cold-start measurement *contradicted* the
assumption that motivated the mitigation — the council finding assumed a cold
container compounding with the agent run, and the container turned out to be
~15% of it. The README states the honest figure rather than implying the ping is
what makes the demo responsive.

---

## 5. Code quality

**Satisfied.**

| Claim | Where |
|---|---|
| 422 tests, all passing; lint and format clean | `pytest`, `ruff check`, `ruff format --check` |
| No live API call anywhere in the suite | CI runs deliberately without secrets so a test that reaches a real endpoint fails there |
| Layering is enforced by structure | `data_sources → agent → report → api`, with `src/llm/` beside them; shared helpers were *moved* rather than duplicated when two layers needed them (`LEGAL_SUFFIXES`, `GENERIC_NAME_TOKENS`, `CircuitBreaker`) |
| Every structured LLM output is a Pydantic model | No hand-parsed JSON; `src/llm/json_parsing.py` is the single shared fenced-JSON path |
| Typed outcomes rather than stringly-typed status | `ResolutionKind`, `RunOutcome`, `ConfidenceReason`, `FailureKind`, `ConflictSeverity` |
| Determinism where tests need it | Circuit breaker takes an injected clock — cooldown and half-open are tested without sleeping |
| Tests pin *reasons*, not just outcomes | `test_non_transient_error_does_not_trip_breaker`; controls paired with positive cases throughout (`entity_warning` has an unwarned twin; the over-fragmentation guard has a boundary control) |
| Provider classification is pinned against real SDK exceptions | `tests/test_providers.py` constructs real Anthropic/Google exception objects, so the suite fails if either SDK moves `status_code` |
| Seams exist where the system must be testable | `AgentDependencies` for the graph; `get_report_runner` for the endpoint, plus a test asserting the default is still the real runner |
| Modules document the decision, not the syntax | Module docstrings carry the rejected alternative — e.g. `confidence.py` explains why precedence is ordered as it is |

**Test debt, tracked and closed rather than carried silently.** Phase 0 recorded
`src/llm/providers.py` as untested and named it the highest-value gap; it was
closed in Phase 2. Phase 0's Companies House client debt was closed in Phase 1.

**The gap.** Nothing asserts the *quality* of a claim's wording, and nothing
can — that is the 7.1a rubric's job, and it is a manual pass rather than an
automated one. `SectionRender.warnings` is still not surfaced to the API.

---

## 6. Judgement on scope

*Did the right things get built, and were the omissions deliberate and stated?*

**Satisfied — and this is the dimension the project's process was built to
serve.**

| Claim | Where |
|---|---|
| Every deferral is named with a reason, not silently dropped | README §5, design note §4 — deeper register analysis, progress streaming, multi-instance rate limiting, second frontend deployment, trading names |
| Deferred ≠ missing data | PSC and full filing history are already gathered, tagged and in the bundle; what is deferred is the *analysis* |
| Limitations are disclosed to a reader who never opens the build log | README §5 carries all four, with mechanism and consequence |
| Disclosure was updated when a limitation got *worse* | README §5 was changed from "in that run it changed nothing" once the conflict misread was observed moving a band on the deployed service |
| Cost was sized against the binding resource, repeatedly and downward | Report cap 50/day → 200 → 130 → 80, each change forced by measured per-report credit cost, with `test_the_report_cap_still_fits_inside_the_free_tier` pinning the arithmetic |
| Scope was cut in the right place under pressure | Phase 3.5 reduced to one bounded pass, keeping the three properties that made it agentic |
| Infrastructure risk was pulled *forward*, not left to the end | Deployment stood up during Phase 3; found two build-context traps and a placeholder-credential bug that cost minutes then and hours later |
| The build log is an honest record, not a cleaned-up story | Its own rule forbids editing past entries; Phase 4's numbers carry an appended addendum saying they were measured while a bug was live and should be read as a floor |
| A wrong prediction was recorded as wrong | Phase 2 predicted the reputation query would disappoint; it was the best of the three, and the log says so |
| Findings were not patched under time pressure when the fix belonged elsewhere | Filing-history rendering, date conflation, conflict basis, entity identity — all disclosed with the reasoning, all located in the layer that owns them |

**The strongest evidence for this dimension is negative.** Four correctness
findings were left unfixed on purpose, each with a stated safe direction, a
located root cause, and a named owning layer — rather than being patched into
the grounding path on submission eve. Two attempted fixes to the filing-history
problem were tried and *rejected on evidence*, including one that made the
problem measurably worse (9 claims → 12), and that failure is recorded rather
than quietly reverted.

---

## Summary

| Dimension | Verdict | Principal gap |
|---|---|---|
| Agent design | Satisfied | 3.5 reduced to one bounded pass, disclosed |
| Data grounding | Satisfied | Entity identity fails on a *shared*-name lookalike (new, disclosed) |
| Uncertainty handling | Satisfied | Two of six bands correct for the wrong reason, traced |
| Product thinking | Satisfied | — |
| Code quality | Satisfied | No automated check on claim *wording*; `SectionRender.warnings` unsurfaced |
| Judgement on scope | Satisfied | — |
