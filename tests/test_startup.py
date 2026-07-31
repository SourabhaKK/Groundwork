"""The fail-fast startup path (Phase 2.6, closing Phase 0's debt).

`lifespan` calls `get_settings()` for one reason: a missing credential must kill
the process at startup rather than surfacing sixty seconds into an agent run as
a report with no evidence in it. That report would be indistinguishable from an
honest one about a thinly-documented company — the failure mode this project
treats as its worst — except that the cause would be a blank environment
variable rather than anything about the company.

Phase 0 built this and never tested it. It is one line whose only observable
behaviour is at process start, which is exactly the kind of thing that quietly
stops working.

Two separable claims, tested separately: that `Settings` rejects an incomplete
environment, and that `lifespan` actually consults it. A test that only did the
first would pass even if the call were deleted from `lifespan` entirely.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.api.main import app
from src.config import Settings, get_settings


def _settings_ignoring_dotenv() -> Settings:
    """Build `Settings` from the environment only.

    `_env_file=None` is load-bearing: without it a developer's real `.env` would
    supply the very key the test just removed, and this would pass locally for
    the wrong reason while testing nothing.
    """
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "missing",
    [
        "COMPANIES_HOUSE_API_KEY",
        "TAVILY_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "DEMO_ACCESS_TOKEN",
    ],
)
def test_every_required_key_is_actually_required(
    missing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each key is checked individually. A single "delete one key" test would
    pass while four others had quietly acquired defaults."""
    monkeypatch.delenv(missing, raising=False)
    get_settings.cache_clear()

    with pytest.raises(ValidationError) as raised:
        _settings_ignoring_dotenv()

    # The error names the key. A generic validation failure during a Phase 6
    # deployment is a far worse artifact than one that says which credential.
    assert missing.lower() in str(raised.value)


def test_a_complete_environment_validates() -> None:
    """The control. Without it, the tests above would pass even if `Settings`
    rejected every environment unconditionally."""
    assert _settings_ignoring_dotenv().companies_house_api_key == "test-ch-key"


def test_startup_consults_the_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """That `lifespan` actually calls `get_settings()` — the wiring, not the
    validation. If the call were removed, config errors would surface mid-run
    instead of at boot, which is the entire failure this guards against."""
    consulted = False

    def _tripwire():
        nonlocal consulted
        consulted = True
        raise RuntimeError("configuration rejected")

    monkeypatch.setattr("src.api.main.get_settings", _tripwire)

    with pytest.raises(RuntimeError, match="configuration rejected"), TestClient(app):
        pass  # pragma: no cover - startup raises before the body runs

    assert consulted


def test_a_valid_configuration_starts_and_serves_health() -> None:
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
