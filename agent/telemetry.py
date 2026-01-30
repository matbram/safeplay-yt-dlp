"""
Telemetry logging for download attempts.

Records every download attempt to Supabase for pattern analysis.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, Any
from dataclasses import dataclass, asdict
import json


@dataclass
class DownloadTelemetry:
    """Data structure for a single download attempt."""

    job_id: str
    youtube_id: str
    success: bool

    # Error info (if failed)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    error_category: Optional[str] = None  # 'permanent', 'retryable', 'unknown'

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
    video_category: Optional[str] = None
    file_size_bytes: Optional[int] = None
    format_id: Optional[str] = None

    # Context
    attempt_number: int = 1
    was_cache_hit: bool = False
    verbose_logs: Optional[str] = None

    def to_db_record(self) -> dict:
        """Convert to database record format."""
        now = datetime.now(timezone.utc)
        record = asdict(self)

        # Add temporal context
        record["hour_of_day"] = now.hour
        record["day_of_week"] = now.weekday()

        # Convert config_snapshot to JSON string if present
        if record.get("config_snapshot"):
            record["config_snapshot"] = json.dumps(record["config_snapshot"])

        return record


class TelemetryLogger:
    """Logs download telemetry to Supabase."""

    def __init__(self, supabase_client):
        self.supabase = supabase_client
        self._buffer: list[dict] = []
        self._buffer_lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None

    async def log(self, telemetry: DownloadTelemetry) -> None:
        """Log a download attempt."""
        record = telemetry.to_db_record()

        async with self._buffer_lock:
            self._buffer.append(record)

            # Flush if buffer is getting large
            if len(self._buffer) >= 10:
                await self._flush_buffer()

    async def log_dict(self, data: dict) -> None:
        """Log telemetry from a dictionary (for integration with existing code)."""
        telemetry = DownloadTelemetry(
            job_id=data.get("job_id", "unknown"),
            youtube_id=data.get("youtube_id", "unknown"),
            success=data.get("success", False),
            error_code=data.get("error_code"),
            error_message=data.get("error_message"),
            error_category=data.get("error_category"),
            total_duration_ms=data.get("total_duration_ms"),
            phase_1_duration_ms=data.get("phase_1_duration_ms"),
            phase_2_duration_ms=data.get("phase_2_duration_ms"),
            phase_3_duration_ms=data.get("phase_3_duration_ms"),
            player_client=data.get("player_client"),
            proxy_country=data.get("proxy_country"),
            proxy_session_id=data.get("proxy_session_id"),
            ytdlp_version=data.get("ytdlp_version"),
            config_snapshot=data.get("config_snapshot"),
            video_title=data.get("video_title"),
            video_duration_seconds=data.get("video_duration_seconds"),
            video_category=data.get("video_category"),
            file_size_bytes=data.get("file_size_bytes"),
            format_id=data.get("format_id"),
            attempt_number=data.get("attempt_number", 1),
            was_cache_hit=data.get("was_cache_hit", False),
            verbose_logs=data.get("verbose_logs"),
        )
        await self.log(telemetry)

    async def _flush_buffer(self) -> None:
        """Flush buffered records to Supabase."""
        if not self._buffer:
            return

        records = self._buffer.copy()
        self._buffer.clear()

        try:
            # Insert in batches
            self.supabase.table("agent_telemetry").insert(records).execute()
        except Exception as e:
            print(f"Failed to flush telemetry: {e}")
            # Put records back in buffer for retry
            self._buffer.extend(records)

    async def start_periodic_flush(self, interval_seconds: int = 30) -> None:
        """Start periodic buffer flushing."""
        async def flush_loop():
            while True:
                await asyncio.sleep(interval_seconds)
                async with self._buffer_lock:
                    await self._flush_buffer()

        self._flush_task = asyncio.create_task(flush_loop())

    async def stop(self) -> None:
        """Stop periodic flushing and flush remaining records."""
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

        async with self._buffer_lock:
            await self._flush_buffer()

    async def get_recent_failures(
        self,
        limit: int = 50,
        hours: int = 24
    ) -> list[dict]:
        """Get recent failed downloads."""
        response = self.supabase.table("agent_telemetry") \
            .select("*") \
            .eq("success", False) \
            .order("created_at", desc=True) \
            .limit(limit) \
            .execute()
        return response.data

    async def get_telemetry_for_job(self, job_id: str) -> list[dict]:
        """Get all telemetry entries for a specific job."""
        response = self.supabase.table("agent_telemetry") \
            .select("*") \
            .eq("job_id", job_id) \
            .order("created_at") \
            .execute()
        return response.data

    async def get_success_rate(self, hours: int = 24) -> dict:
        """Calculate success rate for the given time period."""
        # Query all records in time window
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        response = self.supabase.table("agent_telemetry") \
            .select("success") \
            .gte("created_at", cutoff) \
            .execute()

        if not response.data:
            return {"success_rate": 100.0, "total": 0, "successful": 0}

        total = len(response.data)
        successful = sum(1 for r in response.data if r["success"])
        rate = (successful / total) * 100 if total > 0 else 100.0

        return {
            "success_rate": round(rate, 2),
            "total": total,
            "successful": successful
        }

    async def get_error_distribution(self, hours: int = 24) -> list[dict]:
        """Get distribution of error codes."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        response = self.supabase.table("agent_telemetry") \
            .select("error_code") \
            .eq("success", False) \
            .gte("created_at", cutoff) \
            .execute()

        if not response.data:
            return []

        # Count by error code
        counts: dict[str, int] = {}
        for row in response.data:
            code = row.get("error_code") or "unknown"
            counts[code] = counts.get(code, 0) + 1

        # Sort by count
        return [
            {"error_code": code, "count": count}
            for code, count in sorted(counts.items(), key=lambda x: -x[1])
        ]

    async def get_player_client_performance(self, hours: int = 168) -> list[dict]:
        """Get performance stats by player client (last 7 days by default)."""
        response = self.supabase.table("player_client_performance") \
            .select("*") \
            .execute()
        return response.data

    async def get_hourly_pattern(self, days: int = 7) -> list[dict]:
        """Get success rate by hour of day."""
        response = self.supabase.table("hourly_success_pattern") \
            .select("*") \
            .execute()
        return response.data
