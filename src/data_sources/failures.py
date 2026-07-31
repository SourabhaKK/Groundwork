"""A stated gap, shared by every gathering source.

Companies House and Tavily fail in the same three ways that matter — the
provider is down, it had nothing, or we asked it something malformed — and both
gathers have the same obligation when it happens: record which source is
missing and why, so the report can say so.

This lives in its own module rather than in either gather because the two
consumers of it are Phase 2.4's per-source breaker (which must trip on
`UNAVAILABLE` and never on `ERROR`) and Phase 5's rendering (which must print
any gap at all). Neither of those should have to import the Companies House
module to understand a Tavily failure.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class FailureKind(StrEnum):
    """Why a source is missing.

    The split is the point. `UNAVAILABLE` is the provider's problem and may be
    worth retrying; `ERROR` is ours and must not be, or Phase 2.4's breaker
    trips on our own bugs and demotes a working provider — the same mistake
    Phase 0.6's transient/non-transient classification exists to prevent.
    """

    UNAVAILABLE = "unavailable"  # 5xx, 429, timeout, connection error
    NOT_FOUND = "not_found"  # the source had no record where one was expected
    EXHAUSTED = "exhausted"  # we are out of quota — our budget, not their outage
    ERROR = "error"  # anything else, including our own malformed request


class GatherFailure(BaseModel):
    """One source that could not be fetched, in a form the report can print.

    `source` is a plain string carrying a `RegistrySource` or `WebSection`
    value. Typed as `str` so this model doesn't have to know every source that
    will ever exist; both enums are `StrEnum`, so comparisons against their
    members still work.
    """

    source: str
    kind: FailureKind
    detail: str
    retry_after: float | None = None  # the provider's own number, for Phase 2.4
