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

import re

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


__all__ = ["json_object"]
