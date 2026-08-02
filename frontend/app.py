"""Streamlit frontend for the hosted demo (Phase 6.2).

Runs locally against the deployed API rather than being deployed itself. The
hosted demo is the Cloud Run URL; this is the client for it. A second hosting
account, a second secrets path and a second cold start, on submission eve, to
host a UI the brief explicitly says not to spend polish time on, was not worth
it — and `.gcloudignore` already excludes this directory from the image.

**This file renders. It does not compute.** Every confidence band, badge,
citation marker and source table in the report was produced once, by
`score_confidence` and `src/report/render.py`, and arrives here as finished
markdown. Nothing below re-derives a band or re-counts a claim: Phase 5's whole
architecture is that the shape comes from data, and a frontend that scored a
second time could disagree with the report displayed next to it.

Run it with:

    streamlit run frontend/app.py
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import streamlit as st

DEFAULT_API_URL = os.environ.get(
    "GROUNDWORK_API_URL", "https://groundwork-213351312105.europe-west2.run.app"
)

#: A cold container plus a full graph run — resolution, two parallel gathers,
#: extraction across every document, up to one re-search, then three synthesis
#: calls. Cloud Run's own request timeout is 600s; this sits just inside it so
#: the client reports the server's error rather than inventing its own.
REQUEST_TIMEOUT = 590.0

BAND_ICON = {"high": "🟢", "medium": "🟡", "low": "🔴"}

SECTION_TITLES = {
    "business_model": "Business model",
    "competitive_landscape": "Competitive landscape",
    "reputation": "Quality signals",
}

st.set_page_config(page_title="Groundwork", page_icon="🏛️", layout="wide")


def request_report(api_url: str, token: str, query: str) -> tuple[int, dict[str, Any] | str]:
    """One call to the gated endpoint. Returns the status and the parsed body."""
    try:
        response = httpx.post(
            f"{api_url.rstrip('/')}/report",
            json={"query": query},
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT,
        )
    except httpx.TimeoutException:
        return 0, "The request timed out before the service answered."
    except httpx.HTTPError as exc:
        return 0, f"Could not reach the service: {exc}"

    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, response.text


def render_candidates(resolution: dict[str, Any]) -> None:
    """The disambiguation UI, and it closes the loop rather than just listing.

    Selecting a candidate re-submits **by company number**, which Phase 1.1
    normalises. A UI that showed candidates and made the user retype the name
    would be a screenshot of the feature rather than the feature.
    """
    reasons = resolution.get("reasons") or []
    if reasons:
        st.warning(
            "This needs confirmation before a report is generated — "
            + ", ".join(r.replace("_", " ") for r in reasons)
        )

    candidates = resolution.get("candidates") or []
    if not candidates:
        return

    st.subheader("Which company did you mean?")
    for candidate in candidates:
        with st.container(border=True):
            left, right = st.columns([5, 1])
            with left:
                st.markdown(f"**{candidate['name']}** — `{candidate['number']}`")
                bits = [
                    f"Status: {candidate.get('status') or 'unknown'}",
                    f"Incorporated: {candidate.get('incorporated_on') or 'unknown'}",
                ]
                if candidate.get("sic_codes"):
                    bits.append("SIC: " + ", ".join(candidate["sic_codes"]))
                st.caption(" · ".join(bits))
                if candidate.get("address"):
                    st.caption(candidate["address"])
                if candidate.get("matched_previous_name"):
                    # Phase 1 found this matters: a former *registered* name is
                    # findable, a brand name that was never registered is not,
                    # and the two look identical to a user.
                    st.caption(
                        f"Matched on a former registered name: {candidate['matched_previous_name']}"
                    )
            with right:
                if st.button("Select", key=f"pick-{candidate['number']}"):
                    st.session_state["pending_query"] = candidate["number"]
                    st.rerun()


def render_assessments(assessments: dict[str, Any]) -> None:
    """The bands, exactly as the graph scored them."""
    if not assessments:
        return
    columns = st.columns(len(assessments))
    for column, (section, assessment) in zip(columns, assessments.items(), strict=False):
        with column:
            icon = BAND_ICON.get(assessment["band"], "⚪")
            st.metric(
                SECTION_TITLES.get(section, section.replace("_", " ").title()),
                f"{icon} {assessment['band'].upper()}",
                help=assessment["summary"],
            )
            st.caption(assessment["summary"])


def render_decisions(decisions: list[dict[str, Any]]) -> None:
    """The decision log — the evidence for the agent-design criterion.

    Surfaced in the UI rather than left in Cloud Run's logs, which are not part
    of the deliverable. It deliberately includes the steps where the agent
    decided to do *nothing*: a log of only the times it acted could not be
    audited for judgement.
    """
    if not decisions:
        return
    with st.expander(f"Agent decision log ({len(decisions)} steps)"):
        for entry in decisions:
            line = f"**[{entry['step']}]** {entry['action']} — {entry['rationale']}"
            if entry.get("outcome"):
                line += f" → _{entry['outcome']}_"
            st.markdown(line)


def render_claims(claims: list[dict[str, Any]]) -> None:
    """Every claim with its source, including the ones that did not score.

    Excluded claims are shown *with their reason*. Hiding them would make the
    report look thinner than the evidence gathered and make the reason for the
    thinness invisible — which is the failure Phase 4 explicitly carried
    forward into Phase 5's rendering.
    """
    if not claims:
        return
    excluded = [c for c in claims if not c["counts_toward_confidence"]]
    label = f"Evidence: {len(claims)} claims, {len(claims) - len(excluded)} scoring"
    with st.expander(label):
        st.caption(
            "A claim that does not score still appears in the report's prose. "
            "Only the confidence math excludes it."
        )
        st.dataframe(
            [
                {
                    "Section": SECTION_TITLES.get(c["section"], c["section"]),
                    "Claim": c["text"],
                    "Source type": c["source_type"],
                    "Scores": "yes" if c["counts_toward_confidence"] else "no",
                    "Why not": c.get("exclusion_reason") or "",
                    "Source": c["source_url"],
                }
                for c in claims
            ],
            use_container_width=True,
            hide_index=True,
        )


def render_result(body: dict[str, Any]) -> None:
    outcome = body.get("outcome")
    resolution = body.get("resolution") or {}

    if outcome == "needs_input":
        render_candidates(resolution)
        return

    if outcome == "not_found":
        st.error(resolution.get("detail") or "No company on the register matched that query.")
        st.caption(
            "Note that a company is findable under its registered names, current or former — "
            "a brand name that was never registered has no record to match."
        )
        return

    if outcome == "invalid_input":
        st.error(resolution.get("detail") or "That input could not be a company.")
        return

    if outcome == "no_evidence":
        st.warning(
            "The company was identified on the register, but no usable evidence was gathered "
            "for it. This is a statement about the evidence, not a service fault."
        )

    name = body.get("company_name") or body.get("query")
    number = body.get("company_number")
    st.success(f"Report generated for **{name}**" + (f" (`{number}`)" if number else ""))

    render_assessments(body.get("assessments") or {})
    st.divider()

    if body.get("report"):
        st.markdown(body["report"])

    st.divider()
    render_decisions(body.get("decisions") or [])
    render_claims(body.get("claims") or [])
    st.caption(
        f"Search credits spent on this report: {body.get('credits_spent', 0)} · "
        f"gather rounds: {body.get('gather_rounds', 0)}"
    )


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

st.title("Groundwork")
st.caption("First-pass underwriting intelligence on a UK company, with per-section confidence.")

with st.sidebar:
    st.header("Connection")
    api_url = st.text_input("API URL", value=DEFAULT_API_URL)
    # Password-masked, and never written to disk or logged. The demo runs on a
    # personal API key, so the token is what caps that exposure.
    token = st.text_input(
        "Access token", type="password", value=os.environ.get("GROUNDWORK_TOKEN", "")
    )
    st.caption(
        "The hosted demo is token-gated and rate limited to 5 reports per hour per IP. "
        "A cold container adds to the first request — see the README."
    )

query = st.text_input(
    "Company name or registration number",
    value=st.session_state.pop("pending_query", ""),
    placeholder="Monzo Bank Ltd — or 09446231",
)

if st.button("Generate report", type="primary", disabled=not (query and token)):
    with st.spinner("Resolving, gathering, extracting, synthesising… this takes a minute or two."):
        status, body = request_report(api_url, token, query)

    if status == 200 and isinstance(body, dict):
        render_result(body)
    elif status == 401:
        st.error("The access token was rejected.")
    elif status == 429:
        st.error("Rate limit reached. The demo allows 5 reports per hour per IP.")
    elif status == 503:
        # Kept visibly distinct from a thin report: this is the service failing,
        # not the company being thinly documented.
        detail = body.get("detail") if isinstance(body, dict) else body
        st.error(f"Service fault, not a finding about the company: {detail}")
    else:
        detail = body.get("detail") if isinstance(body, dict) else body
        st.error(f"Request failed ({status or 'no response'}): {detail}")
