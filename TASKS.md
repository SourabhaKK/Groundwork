# Tasks — Phased Build Plan

Each phase must be explained and approved (per `workflows.md`) before Claude Code implements it. Phases are sequential; sub-phases within a phase can be parallelised by Claude Code only after the phase-level decision is confirmed.

**Revised per LLM Council audit round 2** — five further fixes:
4. Added **Phase 2.3a — Source Authority Taxonomy**: the confidence rule and extraction prompts both assumed a source-tiering mechanism that was never actually defined. Now it is, before anything downstream depends on it.
5. **Conflict severity** is now explicitly defined (minor vs. major), not left as an undefined term inside the confidence rule.
6. **Phase 2.5 stretch item** expanded to include PSC (persons-with-significant-control / beneficial ownership) data — same API family, zero extra cost, directly relevant to underwriting.
7. **Phase 0.7's mock subject is locked**: Monzo Bank Ltd — well-documented, strong filing history, heavy trade press and review coverage, outside Allica's direct competitive space.
8. **Phase 7.1** now requires the real built output to be checked against the Phase 0.7 mock for structural conformance, closing the loop the first audit left open.

## Progress

Phase-level tracker. Tick a phase only once every sub-phase under it is ticked **and** its end-of-phase recap has been done per `workflows.md` — a phase isn't complete just because its code is written.

- [x] **Phase 0** — Project Setup & Core Infrastructure — *complete; 0.7 mock signed off 30 Jul 2026*
- [x] **Phase 1** — Company Resolution & Disambiguation — *complete; validated against the live register 31 Jul 2026*
- [x] **Phase 2** — Data Gathering Layer — *core complete 31 Jul 2026; 2.5 deferred by design*
- [x] **Phase 3** — Agent Orchestration (LangGraph) — *complete 1 Aug 2026; 3.5 simplified to a single bounded re-search pass, 4.1 pulled forward as its prerequisite*
- [x] **Phase 4** — Structured Extraction — *complete 1 Aug 2026, signed off and merged; two limitations disclosed in README §5 rather than fixed — conflict-severity misreads and register filing-history over-fragmentation, the latter needing a Phase 2 rendering decision. See `BUILD_LOG.md` Phase 4.*
- [x] **Phase 5** — Report Synthesis & Uncertainty Handling — *complete 2 Aug 2026, signed off and merged; 5.4 consumed unchanged from Phase 3. Cross-provider check passed with zero structural differences; the mock diff found 11 differences, all evidence volume rather than shape. A third limitation is now disclosed in README §5 — filing dates reported as the period they cover — which shares the filing-history document, and its root cause, with the enumeration limitation. See `BUILD_LOG.md` Phase 5.*
- [ ] **Phase 6** — API, Frontend, Deployment
- [ ] **Phase 7** — Testing, Design Note, Audit

## Phase 0 — Project Setup & Core Infrastructure

**Time budget: ≤1 day of the 6-day window.** Three council audit rounds have progressively hardened this plan; round 3 confirmed no further structural issues remain (diminishing returns reached — see `workflows.md` addendum). **This is the final planning revision. No further audit rounds. Build starts immediately after Phase 0 is complete.**

- [x] 0.1 Initialise GitHub repo (private), branch strategy (`main` + feature branches)
- [x] 0.2 Scaffold `pyproject.toml`, dependency lock, `.env.example` for API keys (Companies House, Tavily, Anthropic, Google/Gemini, demo access token)
- [x] 0.3 Directory structure: `src/agent/`, `src/data_sources/`, `src/report/`, `src/api/`, `frontend/`, `tests/`
- [x] 0.4 CI skeleton (GitHub Actions: lint + test on push)
- [x] 0.5 **Token-gate the FastAPI app skeleton** (shared access token check) + rate limiting (per-IP and/or global request cap) — built now so every phase after this is developed and tested against real auth/rate constraints, not exempted from them until Phase 6
- [x] 0.6 **LLM provider circuit breaker skeleton**: Claude (Haiku/Sonnet) primary, Gemini 2.0 Flash fallback, auto-switch on N consecutive failures, logged — built now so Phases 2-5 call LLMs through this router from day one, never directly against a single provider
- [x] 0.7 **Example report mock — requires its own explicit confirmation, separate from 0.1–0.6's scaffolding approval, since this is a design decision, not infrastructure.** Subject: **Monzo Bank Ltd**, chosen for strong Companies House filing history, heavy trade press coverage, and genuine customer reviews — gives a real HIGH-confidence worked example, and sits outside Allica's direct competitive space. Hand-write one realistic full report (all 3 sections, citations, per-section confidence bands) in the exact target output format. **Citations must resolve to real, actually-checked sources — verify each one before writing the claim it supports, don't write a plausible-sounding claim and back-fill a citation.** A fabricated-but-plausible mock is trivially easy to structurally match while being worse on substance, which defeats the point. **This mock is a floor on shape** (section presence, citation format, badge placement), **not a ceiling on quality** — the real system, once built against real Companies House/Tavily data, is expected to exceed it in rigor, not just replicate it. This is the concrete definition of "done" for Phase 5's *shape*; Phase 7's quality rubric is the concrete definition of *good*. Build nothing in Phases 4-5 without this existing first.
- [x] 0.8 `README.md` skeleton (filled in fully in Phase 6)

## Phase 1 — Company Resolution & Disambiguation
- [x] 1.1 Companies House API client: search-by-name, lookup-by-number
- [x] 1.2 Disambiguation logic: multiple-match handling (return candidates with distinguishing detail — status, SIC code, address — not just names). **Output shape revised during the build**: the "found one, but don't accept it silently" cases became a single `NEEDS_CONFIRMATION` kind carrying a *list* of reasons, rather than being folded into `AMBIGUOUS`. See `BUILD_LOG.md` Phase 1.
- [x] 1.3 Failure handling: no exact match (including rebrands/trading names vs. registered legal names — a real-world case, not an edge case), dissolved company, malformed input
- [x] 1.4 Unit tests: exact match, ambiguous match, no match, invalid input — plus direct `CompaniesHouseClient` tests (auth scheme, status classification, `Retry-After`, real-socket timeout enforcement)

## Phase 2 — Data Gathering Layer
- [x] 2.1 Companies House deep pull: filing history, officers, SIC codes, company status
- [x] 2.2 Web/news/trade-press research via Tavily: business model signals, competitor mentions, review/reputation signals
- [x] 2.3 Source-tagging: every fetched document/snippet stored with its URL, source type, and retrieval timestamp
- [x] 2.3a **Source authority taxonomy** — a short allowlist/tagging function, not a research project. Five tiers, highest authority first:
  - **`official_registry`** — Companies House, FCA, PRA, and other independent regulators. No stake in what they report.
  - **`official_self`** — the subject company's own regulated disclosures (e.g. an annual report). Primary and audited, but self-interested. Counts toward Phase 5.4's HIGH-band authority gate **only when corroborated by a non-self claim in the same section**.
  - **`trade_press`** — maintained allowlist of recognized UK business/trade outlets.
  - **`review`** — review platforms.
  - **`general_web`** — everything else, lowest weight.

  The `official_registry` / `official_self` split replaces a single `official` tier, which treated an independent regulator and a company's word about itself as equally authoritative. Found by building the Phase 0.7 mock, which had to cite both an FCA enforcement notice and Monzo's own annual report. Required before Phase 4.2's per-source-type extraction prompts and Phase 5.4's confidence rule, both of which assume this tiering exists.
- [x] 2.4 Rate-limit / failure handling per source (what happens if Companies House or Tavily is down or empty) — each source gets its own circuit-breaker instance, following the same open/half-open/cooldown state machine as Phase 0.6's LLM router, but as a separate instantiation with no fallback target — neither Companies House nor Tavily has an alternate provider to switch to. The *pattern* is shared; the LLM router object itself is not reused for non-LLM sources. Clarified during Phase 1.1 — the original wording ("route through the Phase 0.6 circuit breaker") was ambiguous between literal object reuse and pattern reuse; it meant the latter.
- [ ] 2.5 **[Stretch — if time permits after Phase 7]** Filing-pattern signals from Companies House: SIC code changes over time, officer turnover rate, accounts-filing punctuality, **plus PSC (persons-with-significant-control / beneficial ownership) data** — same API family, zero extra integration cost, and directly relevant to an underwriting context (who actually controls this company). Feeds into the Company Quality Signals section. Log as explicitly deferred in the design note if not completed; do not let this expand Phase 2's core scope.
- [x] 2.6 Unit tests with mocked API responses — plus the Phase 0 carry-forward debt (`providers.py`, `SearchQuotaTracker`, `lifespan`), now closed

## Phase 3 — Agent Orchestration (LangGraph)

**Implementation-order note:** Phase 3.5 (below) depends on Phase 4's `EvidenceClaim` extraction schema existing to evaluate sufficiency. Build Phase 4.1 (the schema) before wiring 3.5's conditional edge — the phases are still presented in this order because 3.1–3.4 (graph skeleton, stopping condition, fallback routing, logging) don't have that dependency and should be built first.

- [x] 3.1 Define the state graph: nodes for resolve → gather (parallel branches per source) → extract → **evaluate sufficiency → (conditional) re-search or proceed** → synthesize → uncertainty-check → render
- [x] 3.2 Explicit stopping condition for the gather phase (max tool calls / diminishing-returns heuristic — must be a real, statable rule, not "until it feels done"). This cap also bounds the re-search loop in 3.5 — the agent cannot re-search indefinitely.
- [x] 3.3 Failure/fallback routing — calls through the Phase 0.6 circuit breaker, plus source-unavailable handling
- [x] 3.4 Logging of every agent decision (which tool was called, why, what it returned, and — for 3.5 — why a re-search was or wasn't triggered) — this log is what makes Phase 3 auditable for the "Agent design" grading criterion
- [x] 3.5 **Adaptive re-search loop — scope simplified to a single bounded pass (one retry per under-evidenced section, not iterative refinement) under the 2-day timeline; the three non-negotiables below are all met. See `BUILD_LOG.md` Phase 3.** Original text follows.

  **Adaptive re-search loop — the genuine agentic decision point, added to close a Round 1 council finding that was raised but never actioned in Rounds 1–3.** After initial extraction (Phase 4) runs against the first gather pass, evaluate per-section claim sufficiency using the same claim-count logic as Phase 5.4's confidence rule. If a section has fewer claims than the MEDIUM threshold (i.e. it would currently score LOW) **and** the Phase 3.2 tool-call budget isn't exhausted, the graph conditionally routes back to a targeted follow-up gather step with a refined query (e.g. narrower to that specific section's gap) rather than proceeding straight to synthesis. This is implemented as a LangGraph conditional edge keyed on runtime extraction state — this is what actually distinguishes an agent from a fixed pipeline: the control flow itself depends on what was found, not just on which step comes next in a predetermined order.

## Phase 4 — Structured Extraction
- [x] 4.1 Pydantic schemas — **built in Phase 3**, per the implementation-order note above: 3.5's conditional edge cannot be wired or tested without the schema it evaluates. `src/agent/claims.py`; the granularity rule is stated on `EvidenceClaim` itself. Original text follows.

  Pydantic schemas: `EvidenceClaim` (text, source_url, source_type, confidence, **`counts_toward_confidence: bool = True`**), per-section schemas.

  Set `counts_toward_confidence = False` when a claim is retrieved and worth surfacing in the report's prose (e.g. as a caveated evidence gap) but is not reliable enough to count toward that section's confidence score — e.g. a single secondary source with no independent corroboration, or a claim substituted from an aggregator because the primary source returned an access error. **The full claim list — including excluded claims — still renders in the report's prose and evidence-gap notes; only the Phase 5.4 scoring math excludes them.** Found by building the Phase 0.7 mock, whose Section 2 manually excluded a secondary estimate from its scored claims while still discussing it in prose — the right instinct, but one a human made by hand. The pipeline needs it as a schema field, not an implicit judgment call reinvented every run.

  **Claim granularity rule:** one claim = one atomic, independently-citable fact. Concretely:
  - **Don't bundle** multiple distinct facts from the same source under a single claim just because they share a citation — e.g. an FCA notice stating a fine amount AND a restriction breach AND a remediation status is three claims, not one, even though all three cite the same document.
  - **Don't split** one fact into multiple claims by rephrasing it or citing the same sentence twice — that inflates claim count without adding evidence, and would let the confidence rule be gamed (deliberately or accidentally) by an extraction step that over-fragments.
  - **Test:** if two people extracting from the same source would likely disagree by more than one claim on how many facts it contains, the rule isn't tight enough yet — tighten it with a concrete example from whatever source type is causing the disagreement.

  Why this is a prerequisite, not a refinement: Phase 5.4 gates entirely on `len(scored) >= 3`, but until this rule exists nothing defines what "one claim" means. Two extraction runs — or two engineers — could chunk the same document differently and move a section's band with no actual change in evidence quality. Found while auditing the Phase 0.7 mock, whose Section 3 bundled several distinguishable facts from one FCA notice into a smaller claim count by hand. See the worked example in `docs/phase-4.1-claim-granularity-example.md`.
- [x] 4.2 Extraction prompts per source type (filings vs. news vs. reviews need different extraction framing) — **built as four framings across the five authority tiers** (`trade_press` and `general_web` share one; they differ in weight, which 5.4 applies at scoring, not in what counts as a fact). One model call per document, with provenance attached from the `TaggedDocument` rather than returned by the model. See `BUILD_LOG.md` Phase 4.

  **Required: honour `TaggedDocument.entity_warning`.** A document carrying a warning appears to describe a differently-named legal entity — e.g. "Monzo, Inc." in a report about MONZO BANK LIMITED. Extraction must either refuse to create claims from it, or create them marked `counts_toward_confidence=False` with the entity mismatch stated in the claim text. It must **not** silently absorb them: a complaint against a foreign affiliate rendered as a fact about the UK company being underwritten is a correctness failure, not a nuance.

  Found by the Phase 2 live Tavily trial, which returned a Better Business Bureau complaints page for the US entity in response to a reputation query about the UK one. Every snippet said only "Monzo". Phase 1 solved this class of problem for *resolution*; the flag is the evidence-side half, and 4.2 is the consumer that makes it mean anything — the warning is computed in `src/data_sources/sources.py` and pinned by `tests/test_sources.py::test_the_warning_travels_on_the_document`.

  **Done, and verified against four real BBB "Monzo, Inc." documents in the Phase 4 live pass**, not only under mocks. Implemented as the flagging route rather than refusal, enforced in code: a warned document's claims are unconditionally non-scoring with the mismatch written into the claim text by `src/agent/extract.py`, so the requirement holds whatever the model returns.
- [x] 4.3 Deduplication / conflict detection across sources (two sources disagree — this must surface, not silently resolve) — dedup is claim identity (`source_url` + `section` + `text`, fixed in Phase 4; see `BUILD_LOG.md`) plus `merge_claims`; conflict detection is a per-section comparison pass that references claims by list position, mapped back to real ids in code. Conflicts are recorded, never resolved, and both sides of a disagreement stay in the claim list.

## Phase 5 — Report Synthesis & Uncertainty Handling
- [x] 5.1 Business model summary generation (grounded in extracted claims only) — validate output shape against the Phase 0.7 mock
- [x] 5.2 Competitive landscape synthesis (structured intensity assessment, not a list) — validate against the Phase 0.7 mock
- [x] 5.3 Company quality signals synthesis + explicit signal-strength rating per section
- [x] 5.4 **Uncertainty-scoring rule — explicit, testable function, not a design intention:**

  **Conflict severity, defined first (this rule was previously undefined and is a prerequisite for the scoring logic below):**
  - **MINOR conflict:** sources differ on phrasing or peripheral detail but agree on the core fact (e.g. two sources give slightly different employee-count estimates, or differing dates for the same event within a plausible range).
  - **MAJOR conflict:** sources contradict a core fact (e.g. one source states the company was acquired/dissolved while Companies House shows it independently trading; or sources give opposite reputation signals on the same specific claim, not just differing tone).

  **Claim count is only meaningful if extraction followed the Phase 4.1 claim-granularity rule** (one claim = one atomic, independently-citable fact). Every count-based condition below inherits that dependency: an extraction step that bundles or over-fragments moves bands without any change in underlying evidence.

  **Precedence is the rule.** Conditions are evaluated strictly top to bottom and the first match returns. The previous version listed HIGH before the MINOR-conflict clause, which let any section satisfying HIGH's count-and-authority test short-circuit before the downgrade was ever checked — making that clause dead code in exactly the case the Phase 0.7 mock hit (7 claims, an official source present, one real MINOR conflict). Order below is load-bearing, not presentational.

  ```
  score_confidence(claims: list[EvidenceClaim]) -> ConfidenceBand:

      scored = [c for c in claims if c.counts_toward_confidence]  # Phase 4.1

      if len(scored) == 0:
          return LOW

      if any MAJOR conflict among scored:
          return LOW

      if all scored claims tagged `general_web`, OR (len(scored) == 1 AND
         that claim is `review`):
          return LOW

      if any MINOR conflict among scored:
          return MEDIUM   # capped here regardless of count/authority — a
                          # minor conflict is evidence the claim isn't
                          # fully settled, and can't be outvoted by
                          # having enough sources

      if len(scored) >= 3 AND at least one scored claim is
         `official_registry` or `trade_press` (an `official_self` claim
         only counts toward this condition if the section also contains
         at least one non-self claim — corroboration, not agreement, is
         what's required):
          return HIGH

      return MEDIUM
  ```
  This runs per-section, not once per report — each of the 3 sections gets its own confidence band, since evidence density varies by section (Companies House data is usually strong for business model, often weak for quality signals).
- [x] 5.5 Render the confidence band visibly per section in the output (badge/label), matching the Phase 0.7 mock — not a disclaimer appended at the end
- [x] 5.6 Citation rendering: every claim in the final report links back to its source
- [x] 5.7 **Cross-provider prompt consistency check — closes a Round 1 council finding (Executor) that was named but never fixed in Rounds 1–3.** Sonnet (primary) and Gemini 2.0 Flash (fallback) are different model families; the synthesis prompts from 5.1-5.3 must be validated against both, not just written once and assumed to transfer. Run the same synthesis prompt against both providers on the Phase 0.7 mock's underlying evidence and confirm structurally equivalent output (same section shape, citation format, confidence-band rendering) before considering Phase 5 complete — a fallback that produces a differently-shaped report is a real failure mode, not a cosmetic one.

## Phase 6 — API, Frontend, Deployment
- [ ] 6.1 FastAPI `/report` endpoint (async, streams progress if feasible) — built on top of the Phase 0.5 token-gated skeleton
- [ ] 6.2 Streamlit frontend: input box, disambiguation UI if needed, rendered report (confidence badges + citations), token entry field
- [ ] 6.3 Dockerize the FastAPI service for Cloud Run deployment
- [ ] 6.4 Deploy to **Google Cloud Run** (locked — see `TECH_STACK.md`), verify hosted URL works end-to-end including cold-start behaviour on the *first* request
- [ ] 6.4a **Cold-start mitigation — closes a Round 1 council finding (Contrarian) that was named but never fixed in Rounds 1–3.** A cold Cloud Run container plus a 30-90s agent run could mean an evaluator's first request takes 2+ minutes — bad enough to matter given it's the single most important moment of the whole submission. Fix: a scheduled keep-warm ping (a GitHub Actions cron job hitting a lightweight `/health` endpoint every ~10 minutes) keeps a container warm without needing paid min-instances. Backup: state the cold-start behaviour plainly in the README regardless, so if the ping ever lapses it reads as a disclosed, understood characteristic rather than an unnoticed flaw.
- [ ] 6.5 Full `README.md`: what was built, setup instructions, architecture summary, decisions, how evaluators should test edge cases, the access token needed to use the live demo

## Phase 7 — Testing, Design Note, Audit
- [ ] 7.1 End-to-end test runs: one well-documented company, one thin-evidence company, one ambiguous name, one invalid input — confirm confidence bands behave correctly across all four. **Additionally, diff the real built output for the Monzo Bank Ltd run against the Phase 0.7 mock for structural conformance** (section presence, citation format, confidence-band rendering) — closes the loop so the mock isn't just a discarded planning artifact.
- [ ] 7.1a **Independent quality rubric — deliberately decoupled from the Phase 0.7 mock**, to guard against the mock itself being an unconsciously easy target. Matching the mock's shape is necessary but not sufficient. Score every test run against these criteria, none of which reference the mock:
  1. No claim appears without a citation that actually resolves to the source it names.
  2. No section's confidence band is contradicted by its own listed evidence (e.g. a HIGH band resting on 2 claims is a rubric failure regardless of how confident the prose sounds).
  3. No claim states something stronger than its source actually supports (over-generalisation, not just fabrication).
  4. The thin-evidence company's report is visibly, honestly thinner than the well-documented one — if both look similarly confident, the uncertainty mechanism isn't working, no matter how well either matches the mock's format.

  A run can pass 7.1's structural diff and still fail this rubric — that's the intended check, not a bug in the process.
- [ ] 7.2 System design note — **lead with the product and the judgment behind it, not a change-log of tool choices.** Structure: what it does and why it's trustworthy (first) → architecture diagram → key decisions with rejected alternatives → what was deferred and why (Phase 2.5 stretch item goes here if not done) → what would change with more time
- [ ] 7.3 Full self-audit against the brief's 6 evaluation dimensions — explicit checklist pass
- [ ] 7.4 Final repo cleanup, commit history sanity check, submission
