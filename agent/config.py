"""
Agent configuration management.

Loads settings from environment variables and Supabase agent_config table.
"""

import os
from typing import Optional, Any
from pydantic_settings import BaseSettings
from pydantic import Field
from functools import lru_cache


class AgentSettings(BaseSettings):
    """Agent settings loaded from environment variables."""

    # === Supabase (shared with main app) ===
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str

    # === LLM Providers ===
    # Claude (Anthropic)
    ANTHROPIC_API_KEY: Optional[str] = None

    # Gemini (Google)
    GOOGLE_API_KEY: Optional[str] = None

    # Default provider: 'claude' or 'gemini'
    LLM_PROVIDER: str = "gemini"

    # Models to use
    LLM_MODEL_ANALYSIS: str = "gemini-2.5-flash"  # For quick analysis
    LLM_MODEL_SYNTHESIS: str = "gemini-2.5-flash-preview-04-17"   # For deeper reasoning

    # === Downloader Connection ===
    DOWNLOADER_URL: str = "http://localhost:3002"
    DOWNLOADER_API_KEY: str = ""

    # === Git Configuration ===
    GIT_REPO_PATH: str = "/opt/safeplay-ytdlp"
    GIT_BRANCH: str = "agent/auto-fixes"
    GIT_AUTO_PUSH: bool = True
    GIT_REMOTE: str = "origin"

    # === Alerting ===
    # Email
    SMTP_HOST: Optional[str] = None
    SMTP_PORT: int = 587
    SMTP_USER: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    ALERT_EMAIL_FROM: Optional[str] = None
    ALERT_EMAIL_TO: Optional[str] = None  # Comma-separated for multiple

    # Website integration
    SAFEPLAY_WEBSITE_URL: Optional[str] = None
    SAFEPLAY_WEBSITE_API_KEY: Optional[str] = None

    # === Rate Limits (liberal for testing) ===
    MAX_FIX_ATTEMPTS_PER_HOUR: int = 20
    MAX_LLM_CALLS_PER_HOUR: int = 100
    MAX_RETRIES_PER_JOB: int = 5

    # === Intervals ===
    HEALTH_CHECK_INTERVAL_SECONDS: int = 300  # 5 minutes
    PATTERN_RECOMPUTE_INTERVAL_HOURS: int = 6
    TELEMETRY_RETENTION_DAYS: int = 30
    LOG_POLL_INTERVAL_SECONDS: int = 5

    # === Paths ===
    AGENT_LOG_DIR: str = "/var/log/safeplay-agent"
    JOURNAL_OUTPUT_PATH: str = "/opt/safeplay-ytdlp/docs/YOUTUBE_LEARNINGS.md"

    # === Service Restart ===
    DOWNLOADER_SERVICE_NAME: str = "safeplay-ytdlp"
    SUDO_RESTART_COMMAND: str = "sudo systemctl restart safeplay-ytdlp"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"  # Ignore extra env vars from main app


@lru_cache()
def get_agent_settings() -> AgentSettings:
    """Get cached agent settings instance."""
    return AgentSettings()


# Runtime configuration that can be updated from Supabase
class RuntimeConfig:
    """
    Runtime configuration loaded from Supabase agent_config table.
    Can be updated without restarting the agent.
    """

    def __init__(self):
        self._config: dict[str, Any] = {}
        self._loaded = False

    async def load(self, supabase_client) -> None:
        """Load configuration from Supabase."""
        try:
            response = supabase_client.table("agent_config").select("*").execute()
            self._config = {row["key"]: row["value"] for row in response.data}
            self._loaded = True
        except Exception as e:
            print(f"Warning: Could not load runtime config: {e}")
            self._config = {}

    def get(self, key: str, default: Any = None) -> Any:
        """Get a configuration value."""
        return self._config.get(key, default)

    async def set(self, supabase_client, key: str, value: Any, updated_by: str = "agent") -> None:
        """Update a configuration value."""
        import json
        supabase_client.table("agent_config").upsert({
            "key": key,
            "value": json.dumps(value) if not isinstance(value, str) else value,
            "updated_by": updated_by
        }).execute()
        self._config[key] = value

    @property
    def llm_provider(self) -> str:
        return self.get("llm_provider", "gemini")

    @property
    def max_fix_attempts_per_hour(self) -> int:
        return int(self.get("max_fix_attempts_per_hour", 20))

    @property
    def max_llm_calls_per_hour(self) -> int:
        return int(self.get("max_llm_calls_per_hour", 100))

    @property
    def auto_push_enabled(self) -> bool:
        return self.get("auto_push_enabled", True)

    @property
    def email_alerts_enabled(self) -> bool:
        return self.get("email_alerts_enabled", True)

    @property
    def website_alerts_enabled(self) -> bool:
        return self.get("website_alerts_enabled", True)


# Global runtime config instance
runtime_config = RuntimeConfig()


# Agent state tracking
class AgentState:
    """Tracks agent operational state."""

    def __init__(self):
        self.fix_attempts_this_hour: int = 0
        self.llm_calls_this_hour: int = 0
        self.last_hour_reset: float = 0
        self.is_paused: bool = False
        self.pause_reason: Optional[str] = None
        self.current_job_id: Optional[str] = None
        self.last_successful_fix: Optional[str] = None

    def reset_hourly_counters(self) -> None:
        """Reset hourly rate limit counters."""
        import time
        self.fix_attempts_this_hour = 0
        self.llm_calls_this_hour = 0
        self.last_hour_reset = time.time()

    def can_make_fix_attempt(self) -> bool:
        """Check if we can make another fix attempt."""
        return (
            not self.is_paused
            and self.fix_attempts_this_hour < runtime_config.max_fix_attempts_per_hour
        )

    def can_call_llm(self) -> bool:
        """Check if we can make another LLM call."""
        return (
            not self.is_paused
            and self.llm_calls_this_hour < runtime_config.max_llm_calls_per_hour
        )

    def record_fix_attempt(self) -> None:
        """Record a fix attempt."""
        self.fix_attempts_this_hour += 1

    def record_llm_call(self) -> None:
        """Record an LLM call."""
        self.llm_calls_this_hour += 1

    def pause(self, reason: str) -> None:
        """Pause the agent."""
        self.is_paused = True
        self.pause_reason = reason

    def resume(self) -> None:
        """Resume the agent."""
        self.is_paused = False
        self.pause_reason = None


# Global agent state
agent_state = AgentState()


# Settings instance
settings = get_agent_settings()
