# workflows.md — Operating Instructions for Claude Code

Read this file first, before touching `PRD.md`, `TECH_STACK.md`, or `TASKS.md`. This file governs *how* you work on this project, not *what* to build — those other three files define the what. This file defines the process, and the process is not optional.

## Why This File Exists

The person you're working with (Sourabha) needs to be able to explain every decision in this codebase to a human interviewer afterward, in his own words, under follow-up questioning. If you make decisions silently — even good ones — he cannot do that, and the project fails its actual purpose regardless of code quality. Treat "did I make sure he understands this, not just that it works" as part of the definition of done for every phase.

## The Core Rule: No Silent Implementation

Before you write or generate any code for a phase or sub-phase in `TASKS.md`:

1. **State the decision you're about to make.** Not "I'll build the disambiguation logic" — the actual decision, e.g. "I'm going to have Companies House search-by-name return up to N candidates and rank them by company status + SIC code relevance, rather than just returning the first match."
2. **State why**, grounded in the PRD's requirements or the brief's evaluation criteria — not "this is standard practice," but the specific reason it fits *this* project.
3. **State the real alternative(s) you considered and rejected**, and why they lost. If there genuinely was no alternative worth considering, say that explicitly rather than inventing a strawman.
4. **Wait for explicit confirmation** ("yes," "go," "sounds good," or a correction) before implementing. Do not proceed on silence or ambiguity — ask again if unclear.
5. **After implementing**, summarise in plain language what was actually built, in a way Sourabha could repeat to an interviewer without needing to re-read the code.

This applies at the sub-phase level for anything non-trivial (a library choice, a schema design, a control-flow decision, a prompt strategy) — not for genuinely mechanical steps (e.g. "creating the `tests/` directory" doesn't need a decision debate).

## Explaining, Not Just Announcing

When you explain a decision, teach it — assume Sourabha needs to defend this choice live, cold, in an interview. That means:
- Explain the mechanism, not just the label. "I used a state graph so failures route explicitly" is better than "I used LangGraph because it's good for agents."
- Name the tradeoff honestly. If the chosen approach has a real weakness, say so — don't oversell. A defensible "here's what this doesn't handle well, and here's why that's an acceptable tradeoff for a 6-day assessment" is worth more than false confidence.
- Connect back to the brief's grading criteria explicitly where relevant, so Sourabha can see *why* a decision matters to the evaluator, not just that it works.

## Phase Flow (matches TASKS.md)

For each phase in `TASKS.md`:
1. Restate the phase's goal in one sentence, and how it maps to the PRD/brief.
2. Walk through each sub-phase using the "Core Rule" process above.
3. At the end of the phase, run a short recap: what was built, what was deliberately deferred (and why — log this, it feeds the design note), and what the next phase depends on from this one.
4. Do not start the next phase until the current one is confirmed complete.

## Setup-to-Completion Lifecycle

1. **Repo init** — GitHub repo creation, clone, branch strategy, initial scaffold (Phase 0). Confirm the structure with Sourabha before filling in any real logic.
2. **Build phases 1–6** — as defined in `TASKS.md`, following the Core Rule throughout.
3. **Testing & audit (Phase 7)** — don't just run tests and report pass/fail. Walk through each of the 6 evaluation dimensions from the brief (agent design, data grounding, uncertainty handling, product thinking, code quality, judgement on scope) one at a time, and show concretely where the codebase satisfies each — this becomes the backbone of the design note.
4. **Design note** — write this collaboratively with Sourabha, not for him. Draft it, then walk him through each section so he can correct or add anything before it's finalised — he needs to be able to defend this document as his own thinking, because it will be read as such.
5. **Submission prep** — final review of README, repo cleanliness, hosted demo verification, before anything is sent to Allica.

## README Requirements

The README is a graded artifact, not documentation boilerplate. It must include, in this order:
1. **What this is** — one paragraph, plain language, as if explaining to the underwriter who'd use it.
2. **What was built** — a clear feature summary mapped to the brief's required output (business model, competitive landscape, quality signals, uncertainty handling).
3. **Architecture overview** — brief, diagram if feasible (even ASCII), matching what's in `TECH_STACK.md`.
4. **Key decisions** — condensed version of the decisions made throughout the build, each with its rejected alternative, so a reader can see the judgement behind the code without reading every commit.
5. **What was deliberately deferred, and why** — directly answers the brief's "judgement on scope" criterion.
6. **Setup instructions** — clone, install, environment variables needed (Companies House API key, Tavily key, LLM provider key), how to run locally, how to run tests.
7. **How to use it** — example input (a real UK company name), what the output looks like, and specifically how to trigger and observe the disambiguation and low-confidence/uncertainty behaviours, since those are the parts an evaluator will most want to test.
8. **Link to the hosted demo.**

Write the README last, after Phase 7's audit — it should describe what was actually built, not what was planned.

## Addendum — LLM Council Audit Round 1 (pre-build)

Before implementation began, the full plan was pressure-tested via a 5-advisor council review. Two structural gaps were found and fixed in `TASKS.md`:
1. **The uncertainty-scoring mechanism was asserted throughout every document but never actually specified.** Fixed: Phase 5.4 now contains an explicit, testable `score_confidence()` rule. Implement this exactly — do not substitute a vaguer "the LLM decides confidence" approach, since that reintroduces the exact problem the council caught.
2. **No one had defined what the actual output looks like** four documents into planning. Fixed: Phase 0.7 requires a hand-written example report *before* any extraction/synthesis code is written — treat this mock as the acceptance target for Phase 5, not a discardable sketch.

A third, lower-severity fix: token-gating and the provider circuit breaker were originally scheduled in Phase 6 (last) but are now Phase 0 (first) — build every later phase against real auth and provider-fallback constraints from the start, not exempted from them until the end.

The design note (Phase 7.2) should lead with the product and the judgment behind it, not read as a log of tool comparisons — an underwriter-reader cares whether the report is trustworthy, not that Exa was considered and rejected in favour of Tavily. Save the detailed rejected-alternatives reasoning for a clearly separated section, not the opening framing.

## Stack Status: Locked

As of this revision, every entry in `TECH_STACK.md` — including hosting (Google Cloud Run, card-verified), the Claude Haiku/Sonnet tiering, the Gemini fallback, Tavily as the search/grounding source, and the token-gating approach — is final. Do not re-open or re-litigate these choices during implementation; the "Core Rule" decision-explanation process still applies to *how* each is implemented (e.g. exact circuit-breaker thresholds, exact rate-limit numbers), but not to *whether* the chosen tool is used.

## Addendum — LLM Council Audit Round 2 (pre-build)

A second council pass, run against the round-1-revised plan, found the fixes held up structurally but surfaced five further gaps, now fixed in `TASKS.md`:
1. **Conflict severity was undefined** inside the confidence rule — "minor/reconcilable" vs. "major" were used without definition. Now explicitly defined (Phase 5.4).
2. **Source-authority tiering didn't exist as a mechanism** despite being assumed by both the confidence rule and the per-source-type extraction prompts. Added as Phase 2.3a, and both downstream consumers now reference it explicitly rather than assuming it.
3. **PSC/beneficial-ownership data** was an available zero-cost signal, left out of the stretch scope. Folded into the existing Phase 2.5 stretch item — same discipline, not new scope.
4. **The Phase 0.7 mock report is a design decision, not scaffolding**, even though it lives inside "Phase 0." **Claude Code must treat 0.7 as requiring its own explicit confirmation, separate from confirming 0.1–0.6.** Do not fold "the mock looks good" into the same approval as "the repo structure looks good" — they are different kinds of decisions and deserve separate scrutiny per the Core Rule above.
5. **Nothing closed the loop** between the hand-written mock and the system's real output. Phase 7.1 now requires an explicit structural diff against the mock before the project is considered done.

## Addendum — LLM Council Audit Round 3 (final)

A third pass found no new structural issues — the marginal finding rate dropped from structural (round 1) to specification gaps (round 2) to a single ordering clarification (round 3), which the council itself flagged as the signature of diminishing returns. Two fixes applied and closed out:
1. **Confidence-rule precedence**: a MAJOR conflict was implied to override HIGH/MEDIUM but never stated as checked first. Now explicit in Phase 5.4 — MAJOR conflict is evaluated before any claim-count or source-authority condition.
2. **Time budget**: three rounds of planning had no check against the fixed 6-day deadline. Phase 0 now carries an explicit ≤1-day budget note.

**This plan is final. No further council audits on this document set — build now.**

## Addendum — Closing Missed Round 1 Findings

A full re-check against all three council rounds found three Round 1 findings that were named by an advisor but never actually carried into any round's fix list — worth stating plainly rather than treating the earlier rounds as fully closed when they weren't:

1. **Agentic behavior (Contrarian).** The original graph was a fixed pipeline with good failure handling, not a system making autonomous, data-dependent decisions. Fixed via Phase 3.5's adaptive re-search loop — a section under-evidenced after initial extraction triggers a targeted follow-up search before synthesis, bounded by the existing stopping condition.
2. **Cloud Run cold starts (Contrarian).** A cold container behind a 30-90s agent run risked a 2+ minute first response for an evaluator — the worst possible moment for latency. Fixed via Phase 6.4a's keep-warm cron ping, with README disclosure as a backup if the ping ever lapses.
3. **Cross-provider prompt consistency (Executor).** Sonnet and Gemini are different model families; nothing required their synthesis output to actually match in shape. Fixed via Phase 5.7 — an explicit validation step against both providers before Phase 5 is considered done.

None of this reopens the council process — these are completions of findings already on record, not new findings. The plan is now fully closed against all three audit rounds.

## Addendum — Phase 0 Build Log (deviations and decisions)

Phase 0.1–0.6 and 0.8 are built. Nothing in `TECH_STACK.md` was substituted — every locked tool is the one in use. Four things are worth recording because they are either structural deviations from `TASKS.md` or numbers that changed after being challenged.

1. **New directory `src/llm/`, not listed in Phase 0.3.** The provider router and circuit breaker are consumed by both `src/agent/` (extraction) and `src/report/` (synthesis), so putting them inside either would make one import the other's internals. Rejected: `src/agent/llm/` — defensible, but it implies the report layer reaches into the agent package for a shared utility. This is a directory addition, not a tool substitution.

2. **Rate limiting is hand-rolled, not `slowapi`.** Roughly 60 lines in `src/api/rate_limit.py`. Rejected: `slowapi` — it does per-IP windows well but has no notion of a global budget, so that half would have been hand-written anyway, alongside a dependency whose semantics we don't control. The hand-rolled version takes an injected clock, which is why the hour-window tests assert exact boundaries without sleeping. `TECH_STACK.md` specifies "per-IP/global rate limiting" as an approach, not a library, so this is not a stack deviation.

3. **Rate-limit numbers were recalibrated before any code was written.** The originally proposed global cap of 50/day allowed ~1,500 runs/month against a Tavily free tier that supports ~250–400. The cap was chosen for feeling generous rather than by working back from the binding resource. It is now **200 reports total** across the review window. The per-IP limit stays at 5/hour.

4. **Phase 0.5 gained a search-quota health signal.** Exhausting Tavily's credits does not crash anything — every later report renders with no web evidence, LOW confidence throughout, and almost no citations. That degradation *mimics the product's own uncertainty feature*, so an evaluator could not distinguish it from a working system reporting honestly on a thin company. `src/data_sources/search_quota.py` records what the last real search actually did and `/health` reports it. It deliberately does not poll a credits endpoint — Tavily exposes none we can depend on, and inventing one would be worse than observing outcomes.

**Circuit-breaker thresholds (Phase 0.6), as implemented:** 3 consecutive transient failures → open → 60s cooldown routing to Gemini → a single half-open probe back to Claude. The load-bearing detail is the classification: only 5xx, 429, timeouts, and connection errors count. A 4xx from our own malformed prompt does **not** trip the breaker, because otherwise a bug in our schema would look like an Anthropic outage and silently demote every later call to the fallback model — debugging the wrong system. `tests/test_router.py::test_non_transient_error_does_not_trip_breaker` is the test that pins this.

**Disclosed reuse:** the breaker follows the same open/cooldown/half-open pattern as the SecureOps provider-fallback router. Pattern reused, code written fresh — recorded here rather than left to look like an unattributed lift, and to be restated in the design note.

**Known limitation, not a bug:** the rate limiter's counters are in-process. They reset when Cloud Run replaces the container (the likely case), and would multiply if the service scaled past one instance (the unlikely case, bounded by pinning `--max-instances=2` at deploy). Both are disclosed in the README rather than presented as airtight.

## Addendum — Spec Fixes Found by Building and Reading the Mock (Phase 0.7)

Constructing and then closely reading the mock against real Monzo/Starling/FCA source material surfaced five real gaps — three in the confidence/taxonomy spec, two in how the mock itself reported its own scoring:

1. **Mock-anchoring risk** (writer of the acceptance target may unconsciously set an easy bar) — mitigated via required source verification, the mock as floor-not-ceiling, and an independent Phase 7.1a rubric that never references the mock. A run can pass the Phase 7.1 shape-diff and still fail 7.1a on substance; that is the intended outcome of that rubric, not a gap in it.
2. **Confidence-rule precedence bug** (MINOR-conflict downgrade was unreachable) — fixed, MINOR check now runs before count/authority.
3. **Taxonomy gap** (no tier distinguishing an independent regulator from a company's own self-disclosure) — fixed via `official_registry` / `official_self` split.
4. **No mechanism to exclude an unreliable-but-worth-mentioning claim from scoring** while still surfacing it in prose — fixed via `counts_toward_confidence` on `EvidenceClaim`.
5. **The mock's own badge conflated claim count with distinct-source count** — fixed by requiring badges to always state claim count, matching what the rule actually scores on.

None of this changed any of the mock's three confidence bands — verified, not assumed. That the bands survived is not proof the original spec was correct; it's a coincidence of the specific evidence available for this one company.

## Phase 0 — End-of-Phase Recap (complete, 30 July 2026)

**Built:** repo and dependency scaffolding on `uv`; a `pydantic-settings` config that fails fast at startup so a missing key can never be mistaken for thin evidence; the `src/{agent,data_sources,report,api}/` layout plus `src/llm/`; CI running ruff and pytest with no secrets in the job; a bearer-token gate on `/report` using constant-time comparison, with per-IP (5/hour) and global (200 total) rate limits; an LLM router and circuit breaker that all model calls pass through, distinguishing provider outages from our own 4xx; an unauthenticated `/health` reporting breaker state, remaining budget and search-quota status; the README skeleton; and the Phase 0.7 Monzo mock, signed off.

**Deferred, with reasons (for the design note):** multi-instance-safe rate limiting — in-process counters reset on container replacement and would multiply if Cloud Run scaled out, bounded by pinning `--max-instances=2` and disclosed in the README rather than presented as airtight. Real search-credit polling — Tavily exposes no dependable endpoint, so `/health` reports what the last actual search did rather than a guessed balance.

**Spec fixes this phase produced** (all found by building, not by a new audit round): the 5.4 precedence bug, the `official_registry`/`official_self` split, `counts_toward_confidence`, the claim-granularity rule, and the badge claim-count/source-count conflation. Recorded in the addendum above.

**What Phase 1 inherits:** `LLMRouter` for every model call; `Settings` for the Companies House key; the gated `/report` stub to build the real endpoint behind; and CI that fails on an unmocked live API call.

## Non-Negotiables Carried Over From Earlier Instructions

- Every claim in the final report must be traceable to a source — this must be a structural property of the data model (per `TASKS.md` Phase 4.1's `EvidenceClaim` schema), not something enforced only by prompting.
- Uncertainty must be surfaced visibly in the actual output, not mentioned only in the README or design note.
- Any deviation from `TECH_STACK.md` during the build must be logged here as an addendum, with the same decision/why/alternative structure, not silently substituted.
- No UI polish time — functional over polished, per the brief.
- Do not reuse code, prompts, or architecture from FinSight, SecureOps, or ChronoScholar wholesale — this should read as its own project, built with the judgement those projects demonstrated, not a copy-paste of them.
