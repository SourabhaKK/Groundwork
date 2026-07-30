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

## Non-Negotiables Carried Over From Earlier Instructions

- Every claim in the final report must be traceable to a source — this must be a structural property of the data model (per `TASKS.md` Phase 4.1's `EvidenceClaim` schema), not something enforced only by prompting.
- Uncertainty must be surfaced visibly in the actual output, not mentioned only in the README or design note.
- Any deviation from `TECH_STACK.md` during the build must be logged here as an addendum, with the same decision/why/alternative structure, not silently substituted.
- No UI polish time — functional over polished, per the brief.
- Do not reuse code, prompts, or architecture from FinSight, SecureOps, or ChronoScholar wholesale — this should read as its own project, built with the judgement those projects demonstrated, not a copy-paste of them.
