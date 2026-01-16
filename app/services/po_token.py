"""Proof-of-Origin (PO) Token Provider Status for YouTube downloads.

The bgutil-ytdlp-pot-provider plugin handles PO tokens automatically through
yt-dlp's plugin system. This module checks if the bgutil HTTP server is
available so we know whether Tier 1 downloads will work.

Setup:
    1. Install bgutil-ytdlp-pot-provider (in requirements.txt)
    2. Run the bgutil server: docker run -d -p 4416:4416 brainicism/bgutil-ytdlp-pot-provider
    3. yt-dlp will automatically use the plugin

This module provides:
    - Server availability checking
    - Status reporting for admin dashboard
"""

import time
from dataclasses import dataclass
from typing import Optional
from threading import Lock
import urllib.request
import urllib.error
import json

from app.services import logger


# bgutil server configuration
BGUTIL_SERVER_URL = "http://127.0.0.1:4416"
BGUTIL_PING_TIMEOUT = 5  # seconds
SERVER_CHECK_INTERVAL = 60  # Re-check server availability every 60 seconds


@dataclass
class CachedToken:
    """Placeholder for compatibility - tokens are managed by yt-dlp plugin."""
    token: str = ""
    visitor_data: str = ""
    fetched_at: float = 0
    expires_at: float = 0


class POTokenManager:
    """
    Manages status of the bgutil PO token server.

    The actual PO token generation is handled by the bgutil-ytdlp-pot-provider
    plugin which yt-dlp loads automatically. This manager just checks if the
    server is available so we know whether Tier 1 downloads will work.
    """

    def __init__(self):
        self._lock = Lock()
        self._server_available: Optional[bool] = None
        self._server_version: Optional[str] = None
        self._last_check: float = 0

    def _check_server(self) -> bool:
        """Check if bgutil HTTP server is running and responding."""
        now = time.time()

        # Use cached result if recent
        with self._lock:
            if self._server_available is not None and (now - self._last_check) < SERVER_CHECK_INTERVAL:
                return self._server_available

        try:
            req = urllib.request.Request(
                f"{BGUTIL_SERVER_URL}/ping",
                headers={"User-Agent": "safeplay-ytdlp"}
            )
            with urllib.request.urlopen(req, timeout=BGUTIL_PING_TIMEOUT) as response:
                data = json.loads(response.read().decode())
                version = data.get("version", "unknown")

                with self._lock:
                    self._server_available = True
                    self._server_version = version
                    self._last_check = now

                # Only log on state change
                if self._server_available != True:
                    logger.info(
                        f"bgutil PO token server available (v{version}) - Tier 1 downloads enabled",
                        "po_token"
                    )

                return True

        except urllib.error.URLError as e:
            with self._lock:
                was_available = self._server_available
                self._server_available = False
                self._server_version = None
                self._last_check = now

            # Only log on state change
            if was_available != False:
                logger.warn(
                    f"bgutil server not reachable at {BGUTIL_SERVER_URL} - Tier 1 downloads disabled. "
                    f"Run: sudo bash deployment/setup-bgutil.sh",
                    "po_token"
                )
            return False

        except Exception as e:
            with self._lock:
                self._server_available = False
                self._server_version = None
                self._last_check = now

            logger.warn(f"Error checking bgutil server: {str(e)[:100]}", "po_token")
            return False

    def is_available(self) -> bool:
        """Check if PO token generation is available."""
        return self._check_server()

    async def get_token(self) -> Optional[CachedToken]:
        """
        Check if PO token generation is available.

        Returns a placeholder CachedToken if server is available, None otherwise.
        The actual token generation is handled by yt-dlp's plugin system.
        """
        if self._check_server():
            # Return a placeholder - actual token is managed by yt-dlp plugin
            return CachedToken(
                token="managed-by-ytdlp-plugin",
                visitor_data="",
                fetched_at=time.time(),
                expires_at=time.time() + 3600
            )
        return None

    def get_ytdlp_args(self, token: CachedToken) -> dict:
        """
        Get yt-dlp arguments for PO token usage.

        The bgutil plugin handles this automatically, so we just return
        empty args. yt-dlp will use the plugin if the server is available.
        """
        # The bgutil-ytdlp-pot-provider plugin handles everything automatically
        # No special args needed - it just works if the server is running
        return {}

    def invalidate(self):
        """Force re-check of server availability."""
        with self._lock:
            self._last_check = 0
        logger.debug("PO token server check invalidated", "po_token")

    def get_status(self) -> dict:
        """Get the current PO token server status."""
        self._check_server()  # Ensure we have fresh status

        with self._lock:
            return {
                "server_available": self._server_available,
                "server_url": BGUTIL_SERVER_URL,
                "server_version": self._server_version,
                "last_check": self._last_check,
                "tier1_enabled": self._server_available == True,
                "setup_command": "sudo bash deployment/setup-bgutil.sh" if not self._server_available else None,
            }


# Global token manager instance
_token_manager: Optional[POTokenManager] = None


def get_token_manager() -> POTokenManager:
    """Get the global PO token manager instance."""
    global _token_manager
    if _token_manager is None:
        _token_manager = POTokenManager()
    return _token_manager


async def get_po_token() -> Optional[CachedToken]:
    """Check if PO token generation is available."""
    return await get_token_manager().get_token()

