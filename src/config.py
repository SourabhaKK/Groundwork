"""Single source of truth for configuration.

Every external key the system needs is declared here. A missing key raises at
startup rather than surfacing 60 seconds into an agent run as a mysteriously
empty report — the failure mode that would otherwise be indistinguishable from
"this company has thin evidence", which is exactly the signal the report is
supposed to carry honestly.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    fallback_model: str = "gemini-2.0-flash"
    max_output_tokens: int = 16_000

    # --- Circuit breaker (Phase 0.6) ---
    breaker_failure_threshold: int = 3
    breaker_cooldown_seconds: float = 60.0

    # --- Rate limiting (Phase 0.5) ---
    # Per-IP window is generous enough for an evaluator to run all four Phase 7.1
    # scenarios back to back. The global budget is a total across the whole review
    # window, sized against Tavily's 1,000-credit free tier (~250-400 runs), not
    # against a per-day intuition.
    per_ip_reports_per_hour: int = 5
    global_report_budget: int = 200


@lru_cache
def get_settings() -> Settings:
    """Cached accessor. Tests call `get_settings.cache_clear()` between cases."""
    return Settings()  # type: ignore[call-arg]  # values come from env/.env
