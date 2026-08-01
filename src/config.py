"""Single source of truth for configuration.

Every external key the system needs is declared here. A missing key raises at
startup rather than surfacing 60 seconds into an agent run as a mysteriously
empty report — the failure mode that would otherwise be indistinguishable from
"this company has thin evidence", which is exactly the signal the report is
supposed to carry honestly.
"""

from functools import lru_cache

from pydantic import ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Values that satisfy "the variable is set" while guaranteeing every call made
# with them fails. Every entry is either verbatim from `.env.example` or a
# convention someone reaches for when stubbing a key out.
#
# Why this exists: the check below started as "the key must be present", which a
# copied-from-example `.env` passes trivially. A container then deploys green,
# `/health` returns 200, and every Companies House and Anthropic call fails at
# run time — surfacing as a report with no evidence in it, which is
# indistinguishable from an honest report about a thinly-documented company.
# That specific confusion is the failure this module was written to prevent, so
# leaving it reachable one layer down defeated the point.
#
# Found in Phase 3 by deploying to Cloud Run with three of five keys still set
# to `REPLACE_ME_*`. Nothing caught it: the deploy succeeded, health was green,
# and the test suite could not catch it because no test in this project is
# allowed to touch a live API.
_PLACEHOLDER_VALUES = frozenset(
    {
        "your-companies-house-key",
        "tvly-your-key",
        "sk-ant-your-key",
        "your-google-ai-studio-key",
        "generate-a-long-random-string",
    }
)

_PLACEHOLDER_PREFIXES = ("replace_me", "replace-me", "changeme", "change-me", "your-")

# Deliberately NOT rejected: `test-*`. `tests/conftest.py` sets exactly those,
# because no unit test may reach a live API — rejecting them here would fail the
# suite for doing the right thing. This validator guards deployment
# configuration, not test fixtures.


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Required credentials (no defaults: absence is a startup error) ---
    companies_house_api_key: str
    tavily_api_key: str
    anthropic_api_key: str
    google_api_key: str
    demo_access_token: str

    # --- LLM routing (TECH_STACK.md: Haiku extraction, Sonnet synthesis only) ---
    extraction_model: str = "claude-haiku-4-5"
    synthesis_model: str = "claude-sonnet-5"
    # Was gemini-2.0-flash, per the original TECH_STACK lock. Changed in Phase 3
    # after a live check: the entire 2.0 family (flash, flash-001, flash-lite,
    # flash-lite-001) returns 429 RESOURCE_EXHAUSTED on our key, while 2.5-flash
    # succeeds on that same key — so this is quota, not credentials. Left as-is,
    # the fallback could never succeed and the circuit breaker would be a no-op
    # that turns any Anthropic outage into total failure. Deviation logged in
    # workflows.md. Pinned, not `gemini-flash-latest`: a floating alias could
    # change model behaviour mid-review-window.
    fallback_model: str = "gemini-2.5-flash"
    max_output_tokens: int = 16_000

    # --- Circuit breaker (Phase 0.6) ---
    breaker_failure_threshold: int = 3
    breaker_cooldown_seconds: float = 60.0

    # --- Rate limiting (Phase 0.5, budget revised in Phase 2.2) ---
    # Per-IP window is generous enough for an evaluator to run all four Phase 7.1
    # scenarios back to back.
    #
    # The global budget is a total across the whole review window, worked back
    # from the binding resource — Tavily's 1,000-credit monthly free tier — and
    # it must be assumed to be spent inside a single month: the review window is
    # days long, and this counter is in-process, so a container replacement
    # resets it and it cannot be relied on to spread load across a month
    # boundary.
    #
    # Phase 0 set this to 200 assuming ~3 credits per report (three basic
    # searches). Phase 2.2 costs a report at 5 credits — one basic search for
    # business model, two advanced for competitive landscape and reputation,
    # where deeper retrieval genuinely changes what comes back — plus up to 1
    # more for Phase 3.5's re-search, so 6 in the worst case. Reserving 200
    # credits for development and Phase 7.1 manual runs (which bypass this gate
    # entirely) leaves 800 to serve from: 800 / 6 = 133.
    per_ip_reports_per_hour: int = 5
    global_report_budget: int = 130

    @field_validator(
        "companies_house_api_key",
        "tavily_api_key",
        "anthropic_api_key",
        "google_api_key",
        "demo_access_token",
    )
    @classmethod
    def _reject_unusable_credential(cls, value: str, info: ValidationInfo) -> str:
        """A credential that is present but cannot work is worse than a missing one.

        A missing key fails loudly at startup. A placeholder key starts cleanly
        and then fails on every outbound call, at which point the symptom is an
        empty report rather than a configuration error — see the note on
        `_PLACEHOLDER_VALUES` above.

        The error names the field, for the same reason the missing-key error
        does: a generic validation failure in a Cloud Run startup log is a far
        worse artifact than one that says which credential to go and fix.
        """
        stripped = value.strip()
        name = info.field_name

        if not stripped:
            raise ValueError(f"{name} is set but empty — provide the real value or unset it")

        lowered = stripped.lower()
        if lowered in _PLACEHOLDER_VALUES or lowered.startswith(_PLACEHOLDER_PREFIXES):
            raise ValueError(
                f"{name} is still a placeholder ({stripped[:24]!r}). Copying .env.example "
                "satisfies the presence check but every call made with it will fail, which "
                "surfaces as a report with no evidence rather than as a config error."
            )

        return stripped

    @field_validator("demo_access_token")
    @classmethod
    def _reject_development_token(cls, value: str) -> str:
        """The demo token is the only credential an evaluator ever handles.

        Checked separately from the placeholder rule because a `local-dev-*`
        token is not a stub — it works. It is simply not the one that should be
        gating a public URL running on a personal API key, and the moment it
        reaches Cloud Run nothing else will ever flag it.
        """
        if value.strip().lower().startswith(("local-dev", "dev-", "local-")):
            raise ValueError(
                "demo_access_token looks like a local development token. The hosted demo "
                "is public and runs on a personal API key — generate a strong random token "
                "for it (e.g. `python -c \"import secrets; print(secrets.token_urlsafe(32))\"`)."
            )
        return value


@lru_cache
def get_settings() -> Settings:
    """Cached accessor. Tests call `get_settings.cache_clear()` between cases."""
    return Settings()  # type: ignore[call-arg]  # values come from env/.env
