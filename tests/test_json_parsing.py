"""Tests for the shared JSON finder/loader (`src/llm/json_parsing.py`).

The load-bearing case here is the one found by the Phase 5 live pass: **Sonnet
writes multi-paragraph prose into a JSON string with real newlines in it, and
Gemini escapes them.** Strict `json.loads` accepts one and rejects the other, and
a rejected response is not a visible error anywhere — in extraction it means the
document contributes no claims, which moves a claim count and therefore a
confidence band. That is why this module now loads with `strict=False`, and why
both shapes are pinned below.
"""

from __future__ import annotations

import json

from src.llm.json_parsing import json_object, json_payload


def test_literal_newlines_inside_a_string_still_load() -> None:
    """The exact shape Sonnet returns. Strict JSON rejects this."""
    raw = '{"prose": "First paragraph.\n\nSecond paragraph.", "gaps": []}'

    # Establish that the input really is strict-invalid, so this test cannot
    # quietly stop testing anything if the input is ever "tidied up".
    try:
        json.loads(raw)
    except ValueError:
        pass
    else:  # pragma: no cover - guards the premise of the test
        raise AssertionError("the fixture is no longer strict-invalid")

    payload = json_payload(raw)
    assert payload is not None
    assert payload["prose"] == "First paragraph.\n\nSecond paragraph."


def test_escaped_newlines_still_load() -> None:
    """The shape Gemini returns. Both providers must parse identically."""
    payload = json_payload('{"prose": "First.\\n\\nSecond.", "gaps": ["a gap"]}')

    assert payload is not None
    assert payload["prose"] == "First.\n\nSecond."
    assert payload["gaps"] == ["a gap"]


def test_a_fenced_object_is_found() -> None:
    payload = json_payload('```json\n{"prose": "text", "gaps": []}\n```')

    assert payload is not None
    assert payload["prose"] == "text"


def test_a_truncated_object_is_rejected_rather_than_half_parsed() -> None:
    """The realistic cap failure: no closing brace, so nothing usable.

    Returning a partial object would be worse than returning nothing — a caller
    would render half a section as though it were whole.
    """
    assert json_payload('{"prose": "text that stops mid-sen') is None


def test_prose_around_the_object_is_ignored() -> None:
    payload = json_payload('Here is the JSON:\n{"prose": "x", "gaps": []}\nHope that helps.')

    assert payload is not None
    assert payload["prose"] == "x"


def test_no_object_at_all_returns_none() -> None:
    assert json_payload("I could not complete this request.") is None
    assert json_object("no braces here") is None
