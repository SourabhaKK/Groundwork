"""Structural signature of a rendered report (Phase 5.7, and Phase 7.1's diff).

Phase 5.7 requires the fallback provider's output to be *structurally
equivalent* to the primary's — and "structurally equivalent" judged by eye is
how a differently-shaped fallback ships. This module reduces a rendered report
to its shape so the comparison is an equality assertion between two values
rather than an impression.

It reads the rendered markdown rather than the objects that produced it, on
purpose. Comparing the `SectionRender` inputs would prove only that the same
data went in; the question is what came out. Reading the output also lets the
same function run against the Phase 0.7 mock, which is a hand-written document
with no objects behind it at all — that is Phase 7.1's structural diff, and
building the comparator now means 7.1 is a run, not a build.

A signature deliberately holds nothing model-authored except the set of citation
markers used. Prose differs between providers and is supposed to; a *missing*
citation is a different matter, since a factual sentence without one fails Phase
7.1a's first criterion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.report.citations import markers_in

_HEADING = re.compile(r"^## (\d+)\.\s+(.+?)\s*$")
_BADGE = re.compile(r"^> ### \S+\s+(HIGH|MEDIUM|LOW)\s*$")
_TIER = re.compile(r"`(official_registry|official_self|trade_press|review|general_web)`")
_TABLE_ROW = re.compile(r"^\|\s*(\d+)\s*\|")

_WORKING_TABLE_HEADING = "Confidence Scoring Applied"
_SOURCES_HEADING = "### Sources — Section"
_CONFLICT_MARKER = "⚠️ Conflict detected"
_GAPS_MARKER = "⚠️ Evidence gaps"
_EXCLUSION_MARKER = "**Excluded from scoring"


@dataclass(frozen=True)
class SectionSignature:
    """One section's shape."""

    number: int
    title: str
    band: str | None
    has_badge: bool
    has_conflict_block: bool
    has_gaps_block: bool
    has_sources_table: bool
    source_rows: int
    source_tiers: tuple[str, ...]
    has_exclusion_note: bool
    citation_markers: tuple[int, ...]


@dataclass(frozen=True)
class ReportSignature:
    """The whole report's shape."""

    has_header: bool
    sections: tuple[SectionSignature, ...] = ()
    has_working_table: bool = False
    working_table_rows: int = 0
    trailing_blocks: tuple[str, ...] = field(default=())

    def differences(self, other: ReportSignature) -> list[str]:
        """Every way these two shapes differ, in a form a human can act on.

        Returns a list rather than a bool because the useful output of a
        cross-provider check is *what* moved, not merely that something did.
        """
        deltas: list[str] = []
        if self.has_header != other.has_header:
            deltas.append(f"header block: {self.has_header} vs {other.has_header}")
        if self.has_working_table != other.has_working_table:
            deltas.append(f"working table: {self.has_working_table} vs {other.has_working_table}")
        if self.working_table_rows != other.working_table_rows:
            deltas.append(
                f"working table rows: {self.working_table_rows} vs {other.working_table_rows}"
            )
        if len(self.sections) != len(other.sections):
            deltas.append(f"section count: {len(self.sections)} vs {len(other.sections)}")
        for left, right in zip(self.sections, other.sections, strict=False):
            deltas += _section_deltas(left, right)
        if self.trailing_blocks != other.trailing_blocks:
            deltas.append(
                f"trailing content after the working table: "
                f"{list(self.trailing_blocks)} vs {list(other.trailing_blocks)}"
            )
        return deltas

    def matches(self, other: ReportSignature) -> bool:
        return not self.differences(other)


def _section_deltas(left: SectionSignature, right: SectionSignature) -> list[str]:
    where = f"section {left.number}"
    checks = (
        ("title", left.title, right.title),
        ("band", left.band, right.band),
        ("badge present", left.has_badge, right.has_badge),
        ("conflict block", left.has_conflict_block, right.has_conflict_block),
        ("gaps block", left.has_gaps_block, right.has_gaps_block),
        ("sources table", left.has_sources_table, right.has_sources_table),
        ("source rows", left.source_rows, right.source_rows),
        ("source tiers", left.source_tiers, right.source_tiers),
        ("exclusion note", left.has_exclusion_note, right.has_exclusion_note),
        ("citation markers", left.citation_markers, right.citation_markers),
    )
    return [f"{where} {name}: {a!r} vs {b!r}" for name, a, b in checks if a != b]


def _split_sections(markdown: str) -> tuple[list[str], list[tuple[int, str, list[str]]], list[str]]:
    """Preamble, numbered sections, and everything after the last one."""
    preamble: list[str] = []
    sections: list[tuple[int, str, list[str]]] = []
    tail: list[str] = []
    current: list[str] | None = None

    for line in markdown.splitlines():
        heading = _HEADING.match(line)
        if heading:
            current = []
            sections.append((int(heading.group(1)), heading.group(2), current))
            continue
        if line.startswith("## ") and _WORKING_TABLE_HEADING in line:
            current = None
            tail.append(line)
            continue
        if current is not None:
            current.append(line)
        elif sections:
            tail.append(line)
        else:
            preamble.append(line)
    return preamble, sections, tail


def _section_signature(number: int, title: str, lines: list[str]) -> SectionSignature:
    body = "\n".join(lines)

    band: str | None = None
    has_badge = False
    for line in lines:
        badge = _BADGE.match(line)
        if badge:
            has_badge = True
            band = badge.group(1)
            break

    # The sources table is everything from its heading to the next blank-line
    # break after the rows start, which is all we need to count rows and tiers.
    source_rows = 0
    tiers: list[str] = []
    in_table = False
    for line in lines:
        if line.startswith(_SOURCES_HEADING):
            in_table = True
            continue
        if in_table:
            if _TABLE_ROW.match(line):
                source_rows += 1
                tiers += _TIER.findall(line)
            elif line.startswith("**") or line.startswith("## "):
                break

    return SectionSignature(
        number=number,
        title=title,
        band=band,
        has_badge=has_badge,
        has_conflict_block=_CONFLICT_MARKER in body,
        has_gaps_block=_GAPS_MARKER in body,
        has_sources_table=_SOURCES_HEADING in body,
        source_rows=source_rows,
        source_tiers=tuple(tiers),
        has_exclusion_note=_EXCLUSION_MARKER in body,
        citation_markers=tuple(sorted(markers_in(body))),
    )


def structural_signature(markdown: str) -> ReportSignature:
    """Reduce a rendered report to its shape."""
    preamble, raw_sections, tail = _split_sections(markdown)

    has_header = any(line.startswith("# ") for line in preamble)
    tail_text = "\n".join(tail)
    has_working_table = _WORKING_TABLE_HEADING in markdown
    working_rows = sum(1 for line in tail if re.match(r"^\|\s*\d+\.\s", line))

    # Anything that is its own heading after the working table. The mock has
    # none, and TASKS.md 5.5 rules out an end-of-report disclaimer, so this
    # exists to make its appearance visible rather than to permit it.
    trailing = tuple(
        line.strip()
        for line in tail_text.splitlines()
        if line.startswith("## ") and _WORKING_TABLE_HEADING not in line
    )

    return ReportSignature(
        has_header=has_header,
        sections=tuple(
            _section_signature(number, title, lines) for number, title, lines in raw_sections
        ),
        has_working_table=has_working_table,
        working_table_rows=working_rows,
        trailing_blocks=trailing,
    )


__all__ = [
    "ReportSignature",
    "SectionSignature",
    "structural_signature",
]
