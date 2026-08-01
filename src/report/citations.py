"""Per-section citation numbering (Phase 5.6).

The numbers are assigned **here**, from the claim list, and the synthesis model
never sees a URL at all — only the marker already bound to each claim. That is
the same mechanism Phase 4.2 used one layer down: extraction has no field
through which a `source_url` can arrive, so a fabricated citation is
unrepresentable rather than discouraged. Synthesis inherits the property by
never being handed a URL-to-number mapping it could extend.

Numbering is **per section**, matching the Phase 0.7 mock: each section's source
table restarts at [1]. That is not cosmetic — Phase 5.4 scores per section, and
a reader checking a band against its own evidence should not have to scan a
report-wide list to do it.

Excluded claims are numbered too. Their sources appear in the section's table
and their facts appear in the prose; only the scoring math leaves them out
(Phase 4.1). A section whose excluded evidence had no citation would be exactly
the "looks thinner than the evidence gathered" failure Phase 4 carried forward.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from src.agent.claims import EvidenceClaim
from src.data_sources.sources import SourceType, TaggedDocument

#: A citation marker in generated prose. Deliberately narrow: a bare `[n]`, the
#: only form the prompts ask for and the only form the validator can check.
_MARKER = re.compile(r"\[(\d+)\]")


@dataclass(frozen=True)
class Citation:
    """One numbered source in one section's table."""

    number: int
    url: str
    source_type: SourceType
    title: str | None = None
    retrieved_at: datetime | None = None

    @property
    def label(self) -> str:
        """The display text for the source table's first column."""
        host = self.url.split("//", 1)[-1].split("/", 1)[0]
        return f"{self.title} · [{host}]({self.url})" if self.title else f"[{host}]({self.url})"


class SectionCitations:
    """The numbering for one section, plus the lookups both consumers need."""

    def __init__(self, citations: Sequence[Citation]) -> None:
        self._citations = list(citations)
        self._by_url = {citation.url: citation.number for citation in self._citations}

    def __iter__(self) -> Iterable[Citation]:  # type: ignore[override]
        return iter(self._citations)

    def __len__(self) -> int:
        return len(self._citations)

    @property
    def citations(self) -> list[Citation]:
        return list(self._citations)

    @property
    def numbers(self) -> set[int]:
        return {citation.number for citation in self._citations}

    def number_for(self, url: str) -> int | None:
        return self._by_url.get(url)

    def marker_for(self, url: str) -> str:
        number = self._by_url.get(url)
        return f"[{number}]" if number else ""


def build_citations(
    claims: Sequence[EvidenceClaim], documents: Sequence[TaggedDocument] = ()
) -> SectionCitations:
    """Number one section's distinct sources, in first-appearance order.

    First-appearance rather than sorted-by-authority so the numbering matches
    the order a reader meets the facts in, which is how the mock reads. Sorting
    by tier would renumber every citation whenever a single new source arrived.

    `documents` supplies title and retrieval timestamp for the source table.
    They are metadata for display only — the claim list alone decides which
    sources are cited and in what order, so a document that produced no claims
    can never appear in a section's table.
    """
    titles = {document.url: document for document in documents}

    citations: list[Citation] = []
    seen: dict[str, int] = {}
    for claim in claims:
        if claim.source_url in seen:
            continue
        number = len(citations) + 1
        seen[claim.source_url] = number
        document = titles.get(claim.source_url)
        citations.append(
            Citation(
                number=number,
                url=claim.source_url,
                source_type=claim.source_type,
                title=(document.title if document else None),
                retrieved_at=(document.retrieved_at if document else None),
            )
        )
    return SectionCitations(citations)


def validate_markers(text: str, valid: set[int]) -> tuple[str, list[str]]:
    """Strip any citation marker the section cannot resolve, and say so.

    The prompt asks the model to carry through the marker already attached to
    each claim, but a prompt is a request. `[5]` in a four-source section is
    either a miscount or an invention, and both render as a citation an
    underwriter would click and not find. Stripping is the safe direction: the
    sentence survives without a citation it never had, rather than carrying one
    that resolves to the wrong source or to nothing.

    Every strip is returned as a warning rather than swallowed — an unreported
    silent fix would hide a synthesis problem behind clean-looking output.
    """
    warnings: list[str] = []

    def replace(match: re.Match[str]) -> str:
        number = int(match.group(1))
        if number in valid:
            return match.group(0)
        warnings.append(
            f"stripped citation {match.group(0)}: this section has "
            f"{len(valid)} source(s), numbered {sorted(valid) or 'none'}"
        )
        return ""

    cleaned = _MARKER.sub(replace, text)
    # Stripping can leave doubled spaces or a space before punctuation.
    cleaned = re.sub(r" {2,}", " ", cleaned)
    cleaned = re.sub(r" ([.,;:])", r"\1", cleaned)
    return cleaned, warnings


def markers_in(text: str) -> set[int]:
    """Every citation marker present in a block of prose."""
    return {int(match.group(1)) for match in _MARKER.finditer(text)}


__all__ = [
    "Citation",
    "SectionCitations",
    "build_citations",
    "markers_in",
    "validate_markers",
]
