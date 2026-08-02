"""Finding the JSON object inside a model response.

Extracted in Phase 4 because two callers now need the identical step and a
second copy would drift: Phase 3.5's query refinement (`agent/research.py`) and
Phase 4.2's claim extraction (`agent/extract.py`).

This module deliberately does **not** parse fields. AGENTS.md's rule is that no
LLM response is picked apart by hand — the object found here is handed to
Pydantic's `model_validate_json`, which is what actually decides whether the
response was usable. All this does is get past the prose and code fences models
wrap JSON in even when told not to.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_OPEN = re.compile(r"^```[a-zA-Z]*\n?")
_FENCE_CLOSE = re.compile(r"\n?```$")


def json_object(raw: str) -> str | None:
    """The outermost `{...}` in `raw`, or None if there isn't one.

    Braces are matched by outermost-pair rather than by counting depth: a
    truncated response — the realistic failure, since extraction runs against a
    token cap — leaves an unbalanced object that a depth counter would return as
    a partial string and Pydantic would then reject anyway. Slicing first and
    last is the same outcome with less machinery.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        text = _FENCE_CLOSE.sub("", _FENCE_OPEN.sub("", text)).strip()

    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    return text[start : end + 1]


def json_payload(raw: str) -> Any | None:
    """The JSON object in `raw`, loaded leniently about literal newlines.

    **Why this exists, and why it is not a style preference.** Strict JSON
    forbids a raw control character inside a string; a newline must be written
    `\\n`. Sonnet writes multi-paragraph prose into a JSON string field with the
    paragraph breaks as *actual* newlines, and Gemini escapes them. Both are the
    same response as far as the model is concerned, and `json.loads` accepts one
    and rejects the other.

    Found in the Phase 5 live pass, and it was not only a Phase 5 problem: the
    same failure was dropping whole documents in Phase 4 extraction, where a
    rejected response means the document contributes **no claims at all**. That
    is a claim count moving — and therefore a confidence band moving — because a
    model used a newline. Nothing surfaced it except a warning line in the log.

    `strict=False` is the standard library's own switch for exactly this: it
    permits control characters inside strings and changes nothing else. The
    alternative considered and rejected was a regex that escapes newlines inside
    string literals before parsing — hand-repairing a model response, which is
    precisely the class of thing AGENTS.md's no-hand-parsing rule exists to stop.

    Returns the loaded object, or None if there is no object or it will not
    parse even leniently. Callers still hand the result to Pydantic — this
    decides *whether there is JSON*, never what the fields mean.
    """
    payload = json_object(raw)
    if payload is None:
        return None
    try:
        return json.loads(payload, strict=False)
    except ValueError:
        return None


__all__ = ["json_object", "json_payload"]
