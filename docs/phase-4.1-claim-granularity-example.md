# Claim Granularity — Worked Example

Concrete precedent for the Phase 4.1 rule: **one claim = one atomic, independently-citable fact.** Extraction (Phase 4.2) should follow this chunking, not reinvent it per run.

Source used: the FCA enforcement notice already cited in the Phase 0.7 mock, Section 3 — [FCA, "FCA fines Monzo £21m for failings in financial crime controls", 8 July 2025](https://www.fca.org.uk/news/press-releases/fca-fines-monzo-21m-failings-financial-crime-controls). Tier: `official_registry`.

## The tie-breaker

The rule's don't-bundle / don't-split pair is not self-applying — several facts in this notice sit near the boundary. The operative test:

> **Does the fact stand alone as something an underwriter could act on, or does it only make sense as a qualifier to another fact?** Stands alone → its own claim. Only makes sense as a qualifier → fold it into the claim it qualifies.

## Correct chunking — 5 claims

| # | Claim | Why it is its own claim |
|---|---|---|
| 1 | The FCA imposed a financial penalty of **£21,091,300** on Monzo Bank Ltd, published 8 July 2025. | The headline fact. Actionable alone. |
| 2 | The penalty concerned **inadequate anti-financial-crime systems and controls between October 2018 and August 2020**. | Recency is independently decisive for an underwriter — a 2018–2020 failing four years resolved reads very differently from a current one. Acted on separately from the amount. |
| 3 | Monzo onboarded customers on **limited and, in some cases, obviously implausible information** — the FCA's published example being customers giving well-known London landmarks as their home address. | Severity evidence. An underwriter weights "controls were weak" and "controls were this weak" differently, and the second stands without the first. **This is the borderline case** — see below. |
| 4 | Monzo **breached an FCA-imposed requirement**: barred in August 2020 from onboarding high-risk customers, it opened **more than 34,000 such accounts** between August 2020 and June 2022. | A distinct and more serious governance fact than the underlying control weakness — this is non-compliance with a direct regulatory instruction, not a systems gap. |
| 5 | Monzo **established and completed a financial crime change programme** to remediate and enhance its wider control framework. | The mitigating fact. Directly actionable and points the opposite way from 1–4. |

## Two boundary calls, resolved

**Folded in, not split out:** the notice states the penalty *would* have been £30,130,475 but attracted a **30% discount** for early resolution. This is **not** a sixth claim. It has no standalone meaning — it exists only to explain how the figure in claim 1 was arrived at, and an underwriter cannot act on "there was a 30% discount" without the penalty it discounts. It belongs inside claim 1 as a qualifier. Splitting it out would be exactly the count-inflation the don't-split clause prohibits.

**Kept separate, with the disagreement acknowledged:** claim 3 (implausible onboarding) could defensibly be folded into claim 2 as an illustration of the control failure. Two extractors could reasonably differ here — which is precisely the ±1 the rule's test tolerates. It is kept separate because the specific finding carries information the general one does not: "controls were inadequate" is a category, "landmarks were accepted as home addresses" is a severity reading that changes how an underwriter weights management competence. If a future source type produces disagreements *wider* than ±1, the rule says tighten it with an example from that source type rather than arguing the individual case.

## Contrast — what the mock did by hand

The Phase 0.7 mock bundled this notice into roughly **2** claims for Section 3 (the fine-and-failings, and the remediation). Under this rule it is **5**. That bundling was a defensible judgment call, but it is exactly the variance the rule exists to remove: the same document, the same evidence, a claim count differing by 3.

**No retroactive mock edit is required, and the band is unaffected.** Recomputing Section 3 under this rule: 5 FCA claims + at least 1 Companies House claim = **6+ scored** (up from 3), still `≥ 3` with an `official_registry` source present and no conflict → **HIGH**, unchanged. The mock's stated counts are left as written; they are a snapshot of hand-chunking, and this document is the precedent that supersedes it for Phase 4.

## Second source type, briefly

The same tie-breaker applied to a company self-disclosure — [Monzo, *Annual Report FY2026*](https://monzo.com/annual-report), tier `official_self`:

- **Separate claims:** revenue £1.7bn (+39%); adjusted profit before tax £172.6m (+20%); deposits £25.7bn (+55%); total customers 15.2m; monthly active users 10.4m; business-banking customer growth +45%.
- **Folded in, not split:** the growth percentages. "£1.7bn" and "+39%" are one fact about one metric stated two ways — reporting them as two claims doubles the count of a single disclosure and is the clearest form of over-fragmentation this rule prohibits.
- **Judgement to watch:** total customers (15.2m) and monthly active users (10.4m) are genuinely different facts — reach versus engagement — and stay separate. An extractor that treats every headline number in a results table as one claim per number will over-count; one that treats the whole table as a single "financial results" claim will under-count by an order of magnitude. Both failure modes move bands without changing evidence.
