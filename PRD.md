# PRD — UK Company Underwriting Intelligence Agent

## 1. Origin & Purpose

This is a take-home technical assessment for Allica Bank's Graduate AI Engineer role (Round 2). Allica is a UK SME-lending fintech; the brief mirrors their real production problem — agentic credit/underwriting decisioning — at a scoped-down, evaluable size. The evaluator is grading judgement and engineering discipline as much as the output, so this PRD, the task breakdown, and the design note are themselves part of the deliverable, not just planning scaffolding.

**Deadline:** August 3, 2026 (6-day build window from kickoff).
**Submission:** hosted demo URL, source code (private repo during the process), a system design note.

## 2. Problem Statement

An underwriter needs a fast, trustworthy first-pass intelligence report on a UK company before a lending decision. Manually researching business model, competitive position, and reputation across scattered public sources is slow. This system automates that first pass — but a wrong or overconfident answer is worse than a slow one in a lending context, so **grounding and honest uncertainty are the actual product**, not the report's fluency.

## 3. User & Use Case

**User:** a credit/underwriting analyst (simulated — no real users in this assessment, but every design decision should be justifiable against what a real underwriter needs, per the brief's "Product thinking" criterion).

**Primary flow:** analyst enters a company name or Companies House number → system resolves the exact company (disambiguating if needed) → system researches autonomously → analyst receives a structured, citation-backed report they can act on or challenge.

## 4. Functional Requirements (from the brief, verbatim scope)

1. **Input & resolution:** accept company name OR Companies House registration number. If a name matches multiple entities, disambiguate — present options or ask a clarifying question. Never silently proceed on a guess.
2. **Autonomous data gathering:** pull from Companies House API (required, free) and other public sources of the system's choosing (web search, news, review sites).
3. **Structured evidence extraction:** every fact pulled into the report must retain its source.
4. **Report synthesis**, three required sections:
   - **Business model summary** — what they do, how they make money, who their customers are.
   - **Competitive landscape** — not a competitor list; a reasoned assessment of competitive intensity, sector- and geography-specific, with an explanation of why that intensity matters.
   - **Company quality signals** — synthesis from reviews/trade press/reputation sources, with an explicit signal-strength rating (is there enough data to be confident?).
5. **Uncertainty surfacing:** every section must be honest about thin or conflicting evidence rather than smoothing over it.
6. **Citations:** every claim traceable to its source.

## 5. Non-Functional Requirements / Evaluation Criteria (from the brief)

| Dimension | What "good" means here |
|---|---|
| Agent design | Clear reasoning about tool/source choice, explicit stopping criteria, explicit failure handling |
| Data grounding | No hallucination, no over-generalisation, claims traceable |
| Uncertainty handling | System states what it doesn't know, doesn't paper over gaps |
| Product thinking | Report shaped by underwriter needs, not just "what's easy to generate" |
| Code quality | Readable, reasonably structured, design choices explained |
| Judgement on scope | What was built vs. deferred, and why — an explicit, defensible decision |

## 6. Explicit Out-of-Scope (Phase 0 scope-cut decisions, defend these if asked)

- **No UI polish** — brief explicitly deprioritises this. Minimal functional frontend only.
- **No multi-company batch processing** — single company per run, matches the brief's framing.
- **No persistent user accounts / auth** — not required by the brief; would be real-product scope, not assessment scope.
- **No fine-tuned models** — off-the-shelf LLM + prompt engineering + retrieval is the right scope for a 6-day window; fine-tuning would be premature optimisation here.
- **Limited to UK companies** — Companies House is UK-only by design; this is a hard constraint of the required data source, not a shortcut.

Anything cut beyond this list must be logged in the design note with a one-line reason, per the "Judgement on scope" criterion.

## 7. Success Criteria

The system is "done" when: a disambiguation-requiring company name resolves correctly, a well-documented company (e.g. a large UK plc) produces a strong report with sections scoring HIGH confidence under the Phase 5.4 rule, a thinly-documented company (e.g. a small private firm) produces a report that *visibly and correctly* scores LOW/MEDIUM confidence per section rather than fabricating detail, and the design note can defend every major decision against a named alternative. The Phase 0.7 hand-written example report is the acceptance target for output shape and quality — the built system's real output should be judged against it, not against a vague sense of "looks reasonable."

## 8. Risks

- **Companies House API coverage gaps** for very new or dissolved companies — must be handled as a first-class failure mode, not an edge case.
- **Web search noise/quality** for smaller companies with little public footprint — this is exactly the uncertainty-handling case the brief is testing for; treat it as a feature to demonstrate, not a bug to hide.
- **LLM cost/latency** on a multi-step agentic pipeline — needs a hard stopping condition (per "Agent design" criterion) to avoid runaway tool calls. This risk is slightly higher now that the pipeline includes a conditional re-search loop (see `TASKS.md` Phase 3.5) — the same stopping condition caps it, but worth noting the tradeoff explicitly: genuine agentic behavior costs a bounded amount of extra latency/API calls versus a fixed pipeline, and that's an accepted tradeoff, not an oversight.
