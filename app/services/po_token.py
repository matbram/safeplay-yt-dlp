"""Proof-of-Origin (PO) Token Manager for YouTube downloads.

This module manages PO tokens that allow direct YouTube downloads without
needing residential proxies. PO tokens prove to YouTube that we're a
legitimate browser client, bypassing IP-binding restrictions.

Token Lifecycle:
- Tokens are fetched using bgutil-ytdlp-pot-provider
- Tokens are cached in memory with TTL (default 6 hours)
- Tokens are automatically refreshed before expiration
- If token fetch fails, downloads fall back to proxy method
"""

import asyncio
import subprocess
import time
from dataclasses import dataclass
from typing import Optional
from threading import Lock

from app.services import logger


# Token configuration
TOKEN_TTL_SECONDS = 6 * 60 * 60  # 6 hours (tokens typically valid longer, but refresh often)
TOKEN_REFRESH_BUFFER = 30 * 60  # Refresh 30 minutes before expiration
TOKEN_FETCH_TIMEOUT = 60  # 60 seconds to fetch a token


@dataclass
class CachedToken:
    """A cached PO token with expiration tracking."""
    token: str
    visitor_data: str
    fetched_at: float
    expires_at: float

    def is_valid(self) -> bool:
        """Check if token is still valid (not expired)."""
        return time.time() < self.expires_at

    def needs_refresh(self) -> bool:
        """Check if token should be refreshed soon."""
        return time.time() > (self.expires_at - TOKEN_REFRESH_BUFFER)


class POTokenManager:
    """
    Manages Proof-of-Origin tokens for YouTube downloads.

    PO tokens allow direct downloads without IP-binding restrictions.
    This manager handles:
    - Token fetching via bgutil-ytdlp-pot-provider
    - Token caching with automatic refresh
    - Thread-safe token access

    Usage:
        token_manager = POTokenManager()
        token = await token_manager.get_token()
        if token:
            # Use token for yt-dlp: --extractor-args "youtube:player-client=web;po_token=web.gvs+{token}"
    """

    def __init__(self):
        self._cached_token: Optional[CachedToken] = None
        self._lock = Lock()
        self._fetching = False
        self._pot_provider_available: Optional[bool] = None

    def _check_pot_provider(self) -> bool:
        """Check if bgutil-ytdlp-pot-provider is available."""
        if self._pot_provider_available is not None:
            return self._pot_provider_available

        try:
            # Try to import the provider
            result = subprocess.run(
                ["python3", "-c", "import bgutil_ytdlp_pot_provider; print('ok')"],
                capture_output=True,
                text=True,
                timeout=10
            )
            self._pot_provider_available = result.returncode == 0 and "ok" in result.stdout

            if self._pot_provider_available:
                logger.info("PO token provider (bgutil) is available", "po_token")
            else:
                logger.warn(
                    "PO token provider not available - Tier 1 downloads disabled",
                    "po_token",
                    {"stderr": result.stderr[:200] if result.stderr else "none"}
                )
        except Exception as e:
            self._pot_provider_available = False
            logger.warn(
                f"Failed to check PO token provider: {str(e)[:100]}",
                "po_token"
            )

        return self._pot_provider_available

    async def get_token(self) -> Optional[CachedToken]:
        """
        Get a valid PO token, fetching a new one if needed.

        Returns:
            CachedToken if available, None if token fetch failed
        """
        # Check if provider is available
        if not self._check_pot_provider():
            return None

        # Check if we have a valid cached token
        with self._lock:
            if self._cached_token and self._cached_token.is_valid():
                # Check if we should background refresh
                if self._cached_token.needs_refresh() and not self._fetching:
                    # Schedule background refresh
                    asyncio.create_task(self._background_refresh())
                return self._cached_token

        # Need to fetch a new token
        return await self._fetch_token()

    async def _background_refresh(self):
        """Refresh token in background without blocking."""
        try:
            await self._fetch_token()
            logger.debug("Background token refresh completed", "po_token")
        except Exception as e:
            logger.warn(f"Background token refresh failed: {str(e)[:100]}", "po_token")

    async def _fetch_token(self) -> Optional[CachedToken]:
        """
        Fetch a new PO token using bgutil-ytdlp-pot-provider.

        The provider generates tokens by simulating a browser session.
        """
        with self._lock:
            # Double-check if another thread already fetched
            if self._cached_token and self._cached_token.is_valid():
                return self._cached_token

            if self._fetching:
                # Wait for ongoing fetch
                return self._cached_token

            self._fetching = True

        try:
            logger.info("Fetching new PO token...", "po_token")
            start_time = time.time()

            # Use bgutil to generate a token
            # The provider outputs JSON with token and visitor_data
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                self._run_token_fetch
            )

            if result:
                fetch_time = time.time() - start_time
                logger.success(
                    f"PO token fetched successfully in {fetch_time:.1f}s",
                    "po_token",
                    {"token_preview": result.token[:20] + "..."}
                )

                with self._lock:
                    self._cached_token = result

                return result
            else:
                logger.warn("Failed to fetch PO token - will use proxy fallback", "po_token")
                return None

        except Exception as e:
            logger.error(f"PO token fetch error: {str(e)[:100]}", "po_token")
            return None

        finally:
            with self._lock:
                self._fetching = False

    def _run_token_fetch(self) -> Optional[CachedToken]:
        """
        Synchronously run the token fetch subprocess.

        Uses bgutil-ytdlp-pot-provider to generate a token.
        """
        try:
            # The bgutil provider can be invoked as a script or module
            # It outputs a PO token that can be used with yt-dlp
            cmd = [
                "python3", "-c",
                """
import json
from bgutil_ytdlp_pot_provider import generate_token

try:
    result = generate_token()
    print(json.dumps({
        'token': result.get('potoken', result.get('po_token', '')),
        'visitor_data': result.get('visitor_data', result.get('visitorData', ''))
    }))
except Exception as e:
    print(json.dumps({'error': str(e)}))
"""
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=TOKEN_FETCH_TIMEOUT
            )

            if result.returncode != 0:
                logger.warn(
                    f"Token fetch subprocess failed: {result.stderr[:200]}",
                    "po_token"
                )
                return None

            # Parse the output
            import json
            try:
                data = json.loads(result.stdout.strip())
            except json.JSONDecodeError:
                logger.warn(f"Invalid token output: {result.stdout[:100]}", "po_token")
                return None

            if 'error' in data:
                logger.warn(f"Token generation error: {data['error'][:100]}", "po_token")
                return None

            token = data.get('token', '')
            visitor_data = data.get('visitor_data', '')

            if not token:
                logger.warn("Empty token received", "po_token")
                return None

            now = time.time()
            return CachedToken(
                token=token,
                visitor_data=visitor_data,
                fetched_at=now,
                expires_at=now + TOKEN_TTL_SECONDS
            )

        except subprocess.TimeoutExpired:
            logger.error(f"Token fetch timed out after {TOKEN_FETCH_TIMEOUT}s", "po_token")
            return None
        except Exception as e:
            logger.error(f"Token fetch exception: {str(e)[:100]}", "po_token")
            return None

    def get_ytdlp_args(self, token: CachedToken) -> dict:
        """
        Get yt-dlp arguments for using the PO token.

        Args:
            token: The CachedToken to use

        Returns:
            dict of yt-dlp options
        """
        return {
            "extractor_args": {
                "youtube": {
                    "player_client": ["web"],
                    "po_token": [f"web.gvs+{token.token}"],
                }
            }
        }

    def invalidate(self):
        """Invalidate the cached token (e.g., after a download failure)."""
        with self._lock:
            self._cached_token = None
        logger.debug("PO token cache invalidated", "po_token")

    def get_status(self) -> dict:
        """Get the current token manager status."""
        with self._lock:
            if self._cached_token:
                return {
                    "has_token": True,
                    "is_valid": self._cached_token.is_valid(),
                    "needs_refresh": self._cached_token.needs_refresh(),
                    "expires_in_seconds": max(0, self._cached_token.expires_at - time.time()),
                    "fetched_at": self._cached_token.fetched_at,
                }
            else:
                return {
                    "has_token": False,
                    "provider_available": self._pot_provider_available,
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
    """Convenience function to get a PO token."""
    return await get_token_manager().get_token()
