"""Observable status for the search provider's free-tier quota.

Why this exists: exhausting Tavily's credits does not crash anything. Every
subsequent report still renders — with no web evidence, LOW confidence in every
section, and almost no citations. To an evaluator opening the demo cold, that
is indistinguishable from a system that never worked. Silent degradation into
"honest uncertainty" is the single worst failure mode this project has, because
it mimics the feature.

Tavily does not expose a credits-remaining endpoint we can rely on, so this
does not poll one. It records what the last real search actually did, and
/health reports it. Observed state, not a guess.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum


class SearchQuotaStatus(StrEnum):
    UNKNOWN = "unknown"  # no search has run yet this container lifetime
    OK = "ok"
    EXHAUSTED = "exhausted"  # provider rejected us for quota reasons
    ERROR = "error"  # provider failed for some other reason


@dataclass(frozen=True)
class SearchQuotaSnapshot:
    status: SearchQuotaStatus
    searches_this_container: int
    credits_this_container: int = 0
    detail: str | None = None


class SearchQuotaTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status = SearchQuotaStatus.UNKNOWN
        self._count = 0
        self._credits = 0
        self._detail: str | None = None

    def record_success(self, *, credits: int = 1) -> None:
        """Record a search that worked, and what it cost.

        Credits are counted, not just searches, because Tavily prices by depth
        (basic 1, advanced 2) and the global report budget in `config.py` is
        derived from a monthly credit ceiling. Counting searches alone would
        under-report spend by the exact factor the budget was sized against —
        which is how the first version of this budget was wrong.
        """
        with self._lock:
            self._status = SearchQuotaStatus.OK
            self._count += 1
            self._credits += credits
            self._detail = None

    def record_exhausted(self, detail: str) -> None:
        with self._lock:
            self._status = SearchQuotaStatus.EXHAUSTED
            self._detail = detail

    def record_error(self, detail: str) -> None:
        """Record a non-quota failure, without masking a known exhaustion.

        Found by writing this module's first direct tests in Phase 2.6. The
        original rule was "last observation wins", which reads as sensible until
        the searches run concurrently: a report fires three at once, and if one
        comes back 429 (out of credits) and another 503, whichever resolved last
        decided what /health said. "Last" is not a meaningful ordering between
        simultaneous calls, so the tie is now broken by specificity instead —
        `exhausted` is the actionable diagnosis and `error` must not overwrite
        it. A genuine success still clears it, since that is real evidence the
        quota is available again.
        """
        with self._lock:
            if self._status is SearchQuotaStatus.EXHAUSTED:
                self._detail = f"{self._detail} (also: {detail})" if self._detail else detail
                return
            self._status = SearchQuotaStatus.ERROR
            self._detail = detail

    def snapshot(self) -> SearchQuotaSnapshot:
        with self._lock:
            return SearchQuotaSnapshot(
                status=self._status,
                searches_this_container=self._count,
                credits_this_container=self._credits,
                detail=self._detail,
            )

    def reset(self) -> None:
        """Test hook."""
        with self._lock:
            self._status = SearchQuotaStatus.UNKNOWN
            self._count = 0
            self._credits = 0
            self._detail = None


tracker = SearchQuotaTracker()
