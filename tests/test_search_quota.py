"""Direct tests for the search-quota tracker (Phase 2.6, closing Phase 0's debt).

This tracker exists because of the failure mode BUILD_LOG names as the project's
worst: when Tavily's credits run out, nothing crashes. Every later report renders
with no web evidence, LOW confidence throughout and almost no citations — which
is exactly what an honest report about a thinly-documented company looks like.
An evaluator opening the demo cold could not tell the two apart.

So the thing being tested is not really arithmetic. It is that the tracker
cannot quietly report health it doesn't have: an exhausted status must not be
overwritten by a later unrelated error, and a container that has run no searches
at all must say `unknown` rather than `ok`.
"""

from __future__ import annotations

from src.data_sources.search_quota import SearchQuotaStatus, SearchQuotaTracker


def test_a_fresh_tracker_claims_nothing() -> None:
    """`unknown` is not a hedge — a container that has run no searches has no
    evidence the search tier works, and reporting `ok` would be a claim."""
    snapshot = SearchQuotaTracker().snapshot()

    assert snapshot.status is SearchQuotaStatus.UNKNOWN
    assert snapshot.searches_this_container == 0
    assert snapshot.credits_this_container == 0
    assert snapshot.detail is None


def test_credits_accumulate_separately_from_search_count() -> None:
    """Tavily prices by depth, and the global report budget is derived from a
    credit ceiling. Counting searches alone under-reports spend by exactly the
    factor the budget was sized against — the Phase 2.2 reconciliation."""
    tracker = SearchQuotaTracker()
    tracker.record_success(credits=2)
    tracker.record_success(credits=2)
    tracker.record_success(credits=1)

    snapshot = tracker.snapshot()
    assert snapshot.searches_this_container == 3
    assert snapshot.credits_this_container == 5
    assert snapshot.status is SearchQuotaStatus.OK


def test_exhaustion_does_not_increment_the_search_count() -> None:
    """A rejected call did not run a search and did not spend a credit.
    Counting it would inflate spend and make the ceiling look closer than it
    is — the one error direction that would cause us to throttle ourselves."""
    tracker = SearchQuotaTracker()
    tracker.record_success(credits=1)
    tracker.record_exhausted("429")

    snapshot = tracker.snapshot()
    assert snapshot.searches_this_container == 1
    assert snapshot.credits_this_container == 1
    assert snapshot.status is SearchQuotaStatus.EXHAUSTED
    assert snapshot.detail == "429"


def test_a_later_error_does_not_downgrade_exhausted_to_something_vaguer() -> None:
    """`error` is a weaker statement than `exhausted`, and /health is read to
    decide whether the demo is trustworthy. Letting a subsequent connection
    blip overwrite "we are out of credits" would hide the specific, actionable
    diagnosis behind a generic one."""
    tracker = SearchQuotaTracker()
    tracker.record_exhausted("out of credits")
    tracker.record_error("connection reset")

    snapshot = tracker.snapshot()
    assert snapshot.status is SearchQuotaStatus.EXHAUSTED
    # The second failure is not thrown away, just subordinated.
    assert "connection reset" in (snapshot.detail or "")


def test_a_success_clears_an_earlier_error() -> None:
    """Recovery has to be observable, or a single transient failure would leave
    /health permanently pessimistic and the signal would stop being read."""
    tracker = SearchQuotaTracker()
    tracker.record_error("timeout")
    tracker.record_success(credits=1)

    snapshot = tracker.snapshot()
    assert snapshot.status is SearchQuotaStatus.OK
    assert snapshot.detail is None


def test_reset_returns_a_tracker_to_knowing_nothing() -> None:
    tracker = SearchQuotaTracker()
    tracker.record_success(credits=2)
    tracker.record_exhausted("429")
    tracker.reset()

    snapshot = tracker.snapshot()
    assert snapshot.status is SearchQuotaStatus.UNKNOWN
    assert snapshot.credits_this_container == 0
    assert snapshot.detail is None
