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


@pytest.mark.parametrize(
    ("key", "placeholder"),
    [
        # Verbatim from .env.example — the copied-the-example case.
        ("COMPANIES_HOUSE_API_KEY", "your-companies-house-key"),
        ("TAVILY_API_KEY", "tvly-your-key"),
        ("ANTHROPIC_API_KEY", "sk-ant-your-key"),
        ("GOOGLE_API_KEY", "your-google-ai-studio-key"),
        ("DEMO_ACCESS_TOKEN", "generate-a-long-random-string"),
        # The shape actually found in the working .env during Phase 3, which
        # deployed to Cloud Run green and could not have served a single report.
        ("ANTHROPIC_API_KEY", "REPLACE_ME_WITH_ANTHROPIC_KEY"),
        ("COMPANIES_HOUSE_API_KEY", "REPLACE_ME_WITH_LIVE_COMPANIES_HOUSE_KEY"),
        ("GOOGLE_API_KEY", "REPLACE_ME_WITH_GOOGLE_AI_STUDIO_KEY"),
        # Present-but-empty passes a bare `str` annotation, and would otherwise
        # reach the provider as an empty Authorization header.
        ("TAVILY_API_KEY", ""),
        ("ANTHROPIC_API_KEY", "   "),
    ],
)
def test_a_placeholder_credential_is_rejected_like_a_missing_one(
    key: str, placeholder: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Presence is not usability.

    A missing key fails at startup; a placeholder key starts cleanly and fails
    on every outbound call, surfacing as a report with no evidence — which is
    indistinguishable from an honest report about a thin company. That is the
    one failure mode this project treats as unacceptable, so the two cases have
    to behave identically.
    """
    monkeypatch.setenv(key, placeholder)
    get_settings.cache_clear()

    with pytest.raises(ValidationError) as raised:
        _settings_ignoring_dotenv()

    assert key.lower() in str(raised.value)


@pytest.mark.parametrize("token", ["local-dev-token-abc123", "dev-token-xyz", "local-whatever"])
def test_a_development_demo_token_is_rejected(token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Separate from the placeholder rule because such a token genuinely works —
    it is simply not the one that should gate a public URL running on a personal
    API key, and nothing downstream would ever flag it."""
    monkeypatch.setenv("DEMO_ACCESS_TOKEN", token)
    get_settings.cache_clear()

    with pytest.raises(ValidationError) as raised:
        _settings_ignoring_dotenv()

    assert "demo_access_token" in str(raised.value)


def test_test_fixture_credentials_are_not_rejected() -> None:
    """The guard against over-reach.

    `conftest.py` sets `test-*` credentials deliberately, because no unit test
    in this project may reach a live API. If the placeholder rule grew to catch
    those, the suite would fail for doing exactly the right thing — so the
    boundary is pinned here rather than left to be rediscovered.
    """
    settings = _settings_ignoring_dotenv()

    assert settings.anthropic_api_key == "test-anthropic-key"
    assert settings.tavily_api_key == "test-tavily-key"


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
