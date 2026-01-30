"""
Agent telemetry hook - Sends download telemetry to the AI agent.

This module provides a lightweight hook for the downloader to report
telemetry to the AI agent's Supabase tables. The agent uses this data
for pattern analysis and learning.

The hook is designed to be non-blocking and fail-safe - if the agent
tables don't exist or there's an error, downloads continue normally.
"""

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Optional, Any
from dataclasses import dataclass, asdict
import json

# Try to import supabase, but don't fail if not available
try:
    from supabase import create_client
    SUPABASE_AVAILABLE = True
except ImportError:
    SUPABASE_AVAILABLE = False

from app.config import settings
from app.services import logger


@dataclass
class TelemetryEvent:
    """Telemetry data for a download attempt."""

    job_id: str
    youtube_id: str
    success: bool

    # Error info
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    error_category: Optional[str] = None  # 'permanent', 'retryable'

    # Timing
    total_duration_ms: Optional[int] = None
    phase_1_duration_ms: Optional[int] = None
    phase_2_duration_ms: Optional[int] = None
    phase_3_duration_ms: Optional[int] = None

    # Configuration
    player_client: Optional[str] = None
    proxy_country: Optional[str] = None
    proxy_session_id: Optional[str] = None
    ytdlp_version: Optional[str] = None
    config_snapshot: Optional[dict] = None

    # Video metadata
    video_title: Optional[str] = None
    video_duration_seconds: Optional[int] = None
    file_size_bytes: Optional[int] = None
    format_id: Optional[str] = None

    # Context
    attempt_number: int = 1
    was_cache_hit: bool = False
    verbose_logs: Optional[str] = None


class AgentHook:
    """
    Hook for sending telemetry to the AI agent.

    This class manages the connection to Supabase and provides
    a simple interface for the downloader to report events.
    """

    def __init__(self):
        self._client = None
        self._enabled = False
        self._buffer: list[dict] = []
        self._flush_lock = asyncio.Lock()
        self._ytdlp_version: Optional[str] = None

    def _get_client(self):
        """Get or create Supabase client."""
        if not SUPABASE_AVAILABLE:
            return None

        if self._client is None:
            try:
                self._client = create_client(
                    settings.SUPABASE_URL,
                    settings.SUPABASE_SERVICE_KEY
                )
                self._enabled = True
            except Exception as e:
                logger.debug(f"Agent hook: Could not create Supabase client: {e}", "agent")
                self._enabled = False

        return self._client

    def _get_ytdlp_version(self) -> str:
        """Get the current yt-dlp version."""
        if self._ytdlp_version is None:
            try:
                import yt_dlp
                self._ytdlp_version = yt_dlp.version.__version__
            except Exception:
                self._ytdlp_version = "unknown"
        return self._ytdlp_version

    async def emit(self, event: TelemetryEvent) -> None:
        """
        Emit a telemetry event to the agent.

        This method is non-blocking and fail-safe. If there's any error,
        it logs a warning and continues without affecting the download.
        """
        try:
            client = self._get_client()
            if not client:
                logger.info(f"[AgentHook] Supabase client not available, skipping telemetry", "agent")
                return

            status = "SUCCESS" if event.success else "FAILURE"
            logger.info(f"[AgentHook] Emitting telemetry: {status} for {event.youtube_id} (job: {event.job_id})", "agent")

            # Build record
            now = datetime.now(timezone.utc)
            record = {
                "job_id": event.job_id,
                "youtube_id": event.youtube_id,
                "success": event.success,
                "error_code": event.error_code,
                "error_message": event.error_message[:500] if event.error_message else None,
                "error_category": event.error_category,
                "total_duration_ms": event.total_duration_ms,
                "phase_1_duration_ms": event.phase_1_duration_ms,
                "phase_2_duration_ms": event.phase_2_duration_ms,
                "phase_3_duration_ms": event.phase_3_duration_ms,
                "player_client": event.player_client,
                "proxy_country": event.proxy_country,
                "proxy_session_id": event.proxy_session_id,
                "ytdlp_version": event.ytdlp_version or self._get_ytdlp_version(),
                "config_snapshot": event.config_snapshot,
                "video_title": event.video_title[:200] if event.video_title else None,
                "video_duration_seconds": event.video_duration_seconds,
                "file_size_bytes": event.file_size_bytes,
                "format_id": event.format_id,
                "attempt_number": event.attempt_number,
                "was_cache_hit": event.was_cache_hit,
                "hour_of_day": now.hour,
                "day_of_week": now.weekday(),
                # Truncate verbose logs to prevent huge records
                "verbose_logs": event.verbose_logs[:5000] if event.verbose_logs else None,
            }

            # Add to buffer
            async with self._flush_lock:
                self._buffer.append(record)

                # Flush immediately for every event
                # This ensures we don't lose telemetry data
                await self._flush()

        except Exception as e:
            # Never let telemetry errors affect downloads
            logger.warn(f"[AgentHook] Error emitting telemetry: {e}", "agent")

    async def _flush(self) -> None:
        """Flush buffered records to Supabase."""
        if not self._buffer:
            return

        client = self._get_client()
        if not client:
            self._buffer.clear()
            return

        records = self._buffer.copy()
        self._buffer.clear()

        try:
            # Insert records
            client.table("agent_telemetry").insert(records).execute()
            logger.info(f"[AgentHook] ✓ Flushed {len(records)} telemetry records to Supabase", "agent")
        except Exception as e:
            # Table might not exist yet - log the error
            logger.warn(f"[AgentHook] ✗ Could not flush telemetry to Supabase: {e}", "agent")

    async def flush(self) -> None:
        """Public method to flush buffer."""
        async with self._flush_lock:
            await self._flush()

    async def emit_download_complete(
        self,
        job_id: str,
        youtube_id: str,
        success: bool,
        duration_ms: int,
        result: dict,
        error: Optional[Exception] = None,
        attempt_number: int = 1,
        was_cache_hit: bool = False,
    ) -> None:
        """
        Convenience method to emit a download completion event.

        Args:
            job_id: Job ID
            youtube_id: YouTube video ID
            success: Whether download succeeded
            duration_ms: Total duration in milliseconds
            result: Download result dict
            error: Exception if failed
            attempt_number: Which attempt this was
            was_cache_hit: Whether this was a cache hit
        """
        error_code = None
        error_message = None
        error_category = None

        if error:
            error_message = str(error)
            if hasattr(error, 'error_code'):
                error_code = error.error_code
            if hasattr(error, 'retryable'):
                error_category = "retryable" if error.retryable else "permanent"

        event = TelemetryEvent(
            job_id=job_id,
            youtube_id=youtube_id,
            success=success,
            error_code=error_code,
            error_message=error_message,
            error_category=error_category,
            total_duration_ms=duration_ms,
            video_title=result.get("title"),
            video_duration_seconds=result.get("duration_seconds"),
            file_size_bytes=result.get("filesize_bytes"),
            player_client=result.get("player_client"),
            attempt_number=attempt_number,
            was_cache_hit=was_cache_hit,
        )

        await self.emit(event)


# Global singleton instance
agent_hook = AgentHook()


# Convenience functions for easy use

async def emit_download_success(
    job_id: str,
    youtube_id: str,
    duration_ms: int,
    result: dict,
) -> None:
    """Emit a successful download event."""
    await agent_hook.emit_download_complete(
        job_id=job_id,
        youtube_id=youtube_id,
        success=True,
        duration_ms=duration_ms,
        result=result,
        attempt_number=result.get("attempts", 1),
    )


async def emit_download_failure(
    job_id: str,
    youtube_id: str,
    duration_ms: int,
    error: Exception,
    result: dict,
) -> None:
    """Emit a failed download event."""
    await agent_hook.emit_download_complete(
        job_id=job_id,
        youtube_id=youtube_id,
        success=False,
        duration_ms=duration_ms,
        result=result,
        error=error,
        attempt_number=result.get("attempts", 1),
    )


async def emit_cache_hit(
    job_id: str,
    youtube_id: str,
) -> None:
    """Emit a cache hit event."""
    event = TelemetryEvent(
        job_id=job_id,
        youtube_id=youtube_id,
        success=True,
        was_cache_hit=True,
        total_duration_ms=0,
    )
    await agent_hook.emit(event)


async def flush_telemetry() -> None:
    """Flush any buffered telemetry."""
    await agent_hook.flush()
