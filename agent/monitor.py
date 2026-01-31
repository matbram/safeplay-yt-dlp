"""
Log monitor - Watches the downloader for failures.

Supports both WebSocket real-time monitoring and polling fallback.
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Optional, Callable, Any
from dataclasses import dataclass
import aiohttp


@dataclass
class LogEntry:
    """Parsed log entry from the downloader."""
    seq: int
    timestamp: str
    level: str
    category: str
    message: str
    details: dict

    @property
    def is_error(self) -> bool:
        return self.level.upper() in ("ERROR", "WARN")

    @property
    def is_download_failure(self) -> bool:
        """Check if this is a download failure event."""
        if self.level.upper() != "ERROR":
            return False

        # Check for download failure indicators
        failure_keywords = [
            "download failed",
            "error downloading",
            "extraction failed",
            "unable to extract",
            "video unavailable",
            "private video",
            "sign in to confirm",
            "bot detection",
            "http error 403",
            "http error 429",
            "cdn access denied",
        ]

        message_lower = self.message.lower()
        return any(kw in message_lower for kw in failure_keywords)

    @property
    def job_id(self) -> Optional[str]:
        return self.details.get("job_id")

    @property
    def youtube_id(self) -> Optional[str]:
        return self.details.get("youtube_id")

    @property
    def error_code(self) -> Optional[str]:
        return self.details.get("error_code")


class LogMonitor:
    """Monitors downloader logs for failures."""

    def __init__(
        self,
        downloader_url: str,
        api_key: str,
        on_failure: Optional[Callable[[LogEntry], Any]] = None,
    ):
        self.downloader_url = downloader_url.rstrip("/")
        self.api_key = api_key
        self.on_failure = on_failure

        self._running = False
        self._last_seq = 0
        self._ws_task: Optional[asyncio.Task] = None
        self._poll_task: Optional[asyncio.Task] = None

        # Track recent failures to avoid duplicate processing
        self._recent_job_failures: dict[str, datetime] = {}
        self._failure_cooldown_seconds = 60  # Don't re-trigger for same job within 60s

    async def start(self, prefer_websocket: bool = True) -> None:
        """Start monitoring logs."""
        self._running = True

        if prefer_websocket:
            # Try WebSocket first, fall back to polling
            try:
                self._ws_task = asyncio.create_task(self._websocket_monitor())
                await asyncio.sleep(2)  # Give it a moment to connect

                # Check if WebSocket is running
                if self._ws_task.done():
                    raise Exception("WebSocket connection failed")

                print("Log monitor started (WebSocket mode)")
                return
            except Exception as e:
                print(f"WebSocket failed, falling back to polling: {e}")

        # Fall back to polling
        self._poll_task = asyncio.create_task(self._polling_monitor())
        print("Log monitor started (polling mode)")

    async def stop(self) -> None:
        """Stop monitoring."""
        self._running = False

        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    async def _websocket_monitor(self) -> None:
        """Monitor via WebSocket connection."""
        ws_url = self.downloader_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/ws/admin"

        while self._running:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(ws_url) as ws:
                        print(f"WebSocket connected to {ws_url}")

                        async for msg in ws:
                            if not self._running:
                                break

                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self._process_ws_message(msg.data)
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                print(f"WebSocket error: {ws.exception()}")
                                break

            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"WebSocket connection error: {e}")
                if self._running:
                    await asyncio.sleep(5)  # Reconnect delay

    async def _process_ws_message(self, data: str) -> None:
        """Process a WebSocket message."""
        try:
            payload = json.loads(data)

            # Handle different message types from the downloader
            msg_type = payload.get("type", "")

            if msg_type == "log":
                # Single log entry
                entry = self._parse_log_entry(payload.get("data", {}))
                if entry:
                    await self._handle_log_entry(entry)
            elif msg_type in ("logs", "logs_batch", "update"):
                # Batch of logs - check both "logs" and "data" keys
                logs_data = payload.get("logs") or payload.get("data") or []
                for log_data in logs_data:
                    entry = self._parse_log_entry(log_data)
                    if entry:
                        await self._handle_log_entry(entry)

        except json.JSONDecodeError:
            pass

    async def _polling_monitor(self) -> None:
        """Monitor via HTTP polling."""
        from .config import settings

        while self._running:
            try:
                await self._poll_logs()
                await asyncio.sleep(settings.LOG_POLL_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"Polling error: {e}")
                await asyncio.sleep(10)

    async def _poll_logs(self) -> None:
        """Poll for new logs."""
        url = f"{self.downloader_url}/api/admin/logs"
        params = {"since_seq": self._last_seq, "limit": 100}
        headers = {"X-API-Key": self.api_key}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    logs = data.get("logs", [])

                    for log_data in logs:
                        entry = self._parse_log_entry(log_data)
                        if entry:
                            self._last_seq = max(self._last_seq, entry.seq)
                            await self._handle_log_entry(entry)

    def _parse_log_entry(self, data: dict) -> Optional[LogEntry]:
        """Parse a log entry from raw data."""
        try:
            return LogEntry(
                seq=data.get("seq", 0),
                timestamp=data.get("timestamp", ""),
                level=data.get("level", "INFO"),
                category=data.get("category", "general"),
                message=data.get("message", ""),
                details=data.get("details", {})
            )
        except Exception:
            return None

    async def _handle_log_entry(self, entry: LogEntry) -> None:
        """Handle a log entry."""
        # Debug: show all error logs
        if entry.is_error:
            print(f"[Monitor] Received {entry.level}: {entry.message[:100]}")

        if not entry.is_download_failure:
            return

        print(f"[Monitor] FAILURE DETECTED: {entry.message[:100]}")

        job_id = entry.job_id
        if not job_id:
            print(f"[Monitor] No job_id in failure entry, skipping")
            return

        # Check cooldown to avoid processing same failure multiple times
        now = datetime.now(timezone.utc)
        if job_id in self._recent_job_failures:
            last_failure = self._recent_job_failures[job_id]
            if (now - last_failure).total_seconds() < self._failure_cooldown_seconds:
                return

        self._recent_job_failures[job_id] = now

        # Clean up old entries
        cutoff = now.timestamp() - self._failure_cooldown_seconds * 2
        self._recent_job_failures = {
            k: v for k, v in self._recent_job_failures.items()
            if v.timestamp() > cutoff
        }

        # Call the failure handler
        if self.on_failure:
            try:
                result = self.on_failure(entry)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                print(f"Error in failure handler: {e}")

    async def get_logs_for_job(self, job_id: str, limit: int = 100) -> list[LogEntry]:
        """Get all logs for a specific job."""
        url = f"{self.downloader_url}/api/admin/logs"
        params = {"job_id": job_id, "limit": limit}
        headers = {"X-API-Key": self.api_key}

        entries = []
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    for log_data in data.get("logs", []):
                        entry = self._parse_log_entry(log_data)
                        if entry:
                            entries.append(entry)

        return entries

    async def get_recent_errors(self, limit: int = 50) -> list[LogEntry]:
        """Get recent error logs."""
        url = f"{self.downloader_url}/api/admin/logs"
        params = {"level": "ERROR", "limit": limit}
        headers = {"X-API-Key": self.api_key}

        entries = []
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    for log_data in data.get("logs", []):
                        entry = self._parse_log_entry(log_data)
                        if entry:
                            entries.append(entry)

        return entries

    async def get_system_state(self) -> dict:
        """Get current system state from downloader."""
        headers = {"X-API-Key": self.api_key}

        async with aiohttp.ClientSession() as session:
            # Get system info
            async with session.get(
                f"{self.downloader_url}/api/admin/system",
                headers=headers
            ) as response:
                system_info = await response.json() if response.status == 200 else {}

            # Get current config
            async with session.get(
                f"{self.downloader_url}/api/admin/config",
                headers=headers
            ) as response:
                config = await response.json() if response.status == 200 else {}

            # Get download stats
            async with session.get(
                f"{self.downloader_url}/api/admin/download-stats",
                headers=headers
            ) as response:
                stats = await response.json() if response.status == 200 else {}

        return {
            "system": system_info,
            "config": config,
            "stats": stats,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

    async def get_current_config(self) -> dict:
        """Get current downloader configuration."""
        headers = {"X-API-Key": self.api_key}

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.downloader_url}/api/admin/config",
                headers=headers
            ) as response:
                if response.status == 200:
                    return await response.json()
                return {}

    async def update_config(self, config_updates: dict) -> bool:
        """Update downloader configuration."""
        headers = {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json"
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.downloader_url}/api/admin/config",
                headers=headers,
                json=config_updates
            ) as response:
                return response.status == 200
