"""Video download method cache for smart Tier 1/Tier 2 selection.

This module tracks which videos work with which download method:
- Tier 1 (PO Token): Direct download without proxy
- Tier 2 (Proxy): Full proxy download

By caching this information, we avoid wasting time retrying Tier 1
on videos we know require Tier 2 (proxy).

Cache entries expire after 24 hours since YouTube's behavior can change.
"""

import time
from dataclasses import dataclass
from enum import Enum
from threading import Lock
from typing import Dict, Optional

from app.services import logger


class DownloadMethod(Enum):
    """Download method used for a video."""
    TIER1_PO_TOKEN = "tier1_po_token"  # Direct download with PO token
    TIER2_PROXY = "tier2_proxy"  # Full proxy download
    UNKNOWN = "unknown"  # Not yet determined


class FailureReason(Enum):
    """Reason why Tier 1 failed for a video."""
    IP_BINDING = "ip_binding"  # URL was IP-bound (403 on direct download)
    AGE_RESTRICTED = "age_restricted"  # Video requires age verification
    REGION_LOCKED = "region_locked"  # Video not available in region
    BOT_DETECTION = "bot_detection"  # YouTube detected bot/automation
    TOKEN_EXPIRED = "token_expired"  # PO token was expired/invalid
    FORMAT_UNAVAILABLE = "format_unavailable"  # Requested format not available
    UNKNOWN = "unknown"  # Unknown failure reason


@dataclass
class MethodCacheEntry:
    """Cache entry for a video's download method."""
    youtube_id: str
    method: DownloadMethod
    failure_reason: Optional[FailureReason]
    recorded_at: float
    expires_at: float
    success_count: int = 0  # How many times this method worked
    failure_count: int = 0  # How many times this method failed

    def is_expired(self) -> bool:
        """Check if this cache entry has expired."""
        return time.time() > self.expires_at


# Cache configuration
CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 hours
MAX_CACHE_SIZE = 10000  # Maximum entries to prevent memory bloat


class MethodCache:
    """
    In-memory cache for video download method preferences.

    Tracks which videos work with Tier 1 (PO token) vs Tier 2 (proxy).
    This avoids wasting time and resources retrying Tier 1 on videos
    that we know require Tier 2.

    Usage:
        cache = get_method_cache()

        # Check preferred method before downloading
        entry = cache.get(youtube_id)
        if entry and entry.method == DownloadMethod.TIER2_PROXY:
            # Skip Tier 1, go straight to proxy
            pass

        # Record result after download
        cache.record_success(youtube_id, DownloadMethod.TIER1_PO_TOKEN)
        # or
        cache.record_failure(youtube_id, DownloadMethod.TIER1_PO_TOKEN, FailureReason.IP_BINDING)
    """

    def __init__(self):
        self._cache: Dict[str, MethodCacheEntry] = {}
        self._lock = Lock()
        self._stats = {
            "tier1_attempts": 0,
            "tier1_successes": 0,
            "tier2_attempts": 0,
            "tier2_successes": 0,
            "cache_hits": 0,
            "cache_misses": 0,
        }

    def get(self, youtube_id: str) -> Optional[MethodCacheEntry]:
        """
        Get the cached method for a video.

        Args:
            youtube_id: YouTube video ID

        Returns:
            MethodCacheEntry if found and not expired, None otherwise
        """
        with self._lock:
            entry = self._cache.get(youtube_id)
            if entry:
                if entry.is_expired():
                    # Remove expired entry
                    del self._cache[youtube_id]
                    self._stats["cache_misses"] += 1
                    return None
                self._stats["cache_hits"] += 1
                return entry
            self._stats["cache_misses"] += 1
            return None

    def should_skip_tier1(self, youtube_id: str) -> bool:
        """
        Check if we should skip Tier 1 and go straight to Tier 2.

        Returns True if we've previously determined this video needs proxy.
        """
        entry = self.get(youtube_id)
        if entry and entry.method == DownloadMethod.TIER2_PROXY:
            # Only skip if we've seen consistent failures
            if entry.failure_count >= 2:
                logger.debug(
                    f"Skipping Tier 1 for {youtube_id} - previously required proxy",
                    "method_cache",
                    {"failure_reason": entry.failure_reason.value if entry.failure_reason else "unknown"}
                )
                return True
        return False

    def record_success(self, youtube_id: str, method: DownloadMethod):
        """
        Record a successful download with the specified method.

        Args:
            youtube_id: YouTube video ID
            method: The method that succeeded
        """
        with self._lock:
            now = time.time()
            entry = self._cache.get(youtube_id)

            if entry:
                entry.method = method
                entry.success_count += 1
                entry.failure_reason = None
                entry.recorded_at = now
                entry.expires_at = now + CACHE_TTL_SECONDS
            else:
                self._cache[youtube_id] = MethodCacheEntry(
                    youtube_id=youtube_id,
                    method=method,
                    failure_reason=None,
                    recorded_at=now,
                    expires_at=now + CACHE_TTL_SECONDS,
                    success_count=1,
                    failure_count=0,
                )

            # Update stats
            if method == DownloadMethod.TIER1_PO_TOKEN:
                self._stats["tier1_successes"] += 1
            elif method == DownloadMethod.TIER2_PROXY:
                self._stats["tier2_successes"] += 1

            # Cleanup if cache is too large
            self._cleanup_if_needed()

    def record_failure(
        self,
        youtube_id: str,
        method: DownloadMethod,
        reason: FailureReason
    ):
        """
        Record a failed download attempt.

        Args:
            youtube_id: YouTube video ID
            method: The method that failed
            reason: Why it failed
        """
        with self._lock:
            now = time.time()
            entry = self._cache.get(youtube_id)

            if entry:
                entry.failure_count += 1
                entry.failure_reason = reason
                entry.recorded_at = now
                entry.expires_at = now + CACHE_TTL_SECONDS

                # If Tier 1 failed, mark as needing Tier 2
                if method == DownloadMethod.TIER1_PO_TOKEN:
                    entry.method = DownloadMethod.TIER2_PROXY
            else:
                # New entry - mark as needing Tier 2 if Tier 1 failed
                target_method = (
                    DownloadMethod.TIER2_PROXY
                    if method == DownloadMethod.TIER1_PO_TOKEN
                    else method
                )
                self._cache[youtube_id] = MethodCacheEntry(
                    youtube_id=youtube_id,
                    method=target_method,
                    failure_reason=reason,
                    recorded_at=now,
                    expires_at=now + CACHE_TTL_SECONDS,
                    success_count=0,
                    failure_count=1,
                )

            # Cleanup if cache is too large
            self._cleanup_if_needed()

    def record_attempt(self, method: DownloadMethod):
        """Record that we attempted a download with the specified method."""
        with self._lock:
            if method == DownloadMethod.TIER1_PO_TOKEN:
                self._stats["tier1_attempts"] += 1
            elif method == DownloadMethod.TIER2_PROXY:
                self._stats["tier2_attempts"] += 1

    def _cleanup_if_needed(self):
        """Remove oldest entries if cache is too large."""
        if len(self._cache) > MAX_CACHE_SIZE:
            # Remove oldest 10% of entries
            entries = sorted(
                self._cache.items(),
                key=lambda x: x[1].recorded_at
            )
            for key, _ in entries[:MAX_CACHE_SIZE // 10]:
                del self._cache[key]

    def get_stats(self) -> dict:
        """Get cache statistics."""
        with self._lock:
            tier1_rate = (
                self._stats["tier1_successes"] / max(1, self._stats["tier1_attempts"])
            ) * 100
            tier2_rate = (
                self._stats["tier2_successes"] / max(1, self._stats["tier2_attempts"])
            ) * 100

            return {
                "cache_size": len(self._cache),
                "cache_hits": self._stats["cache_hits"],
                "cache_misses": self._stats["cache_misses"],
                "tier1_attempts": self._stats["tier1_attempts"],
                "tier1_successes": self._stats["tier1_successes"],
                "tier1_success_rate": round(tier1_rate, 1),
                "tier2_attempts": self._stats["tier2_attempts"],
                "tier2_successes": self._stats["tier2_successes"],
                "tier2_success_rate": round(tier2_rate, 1),
            }

    def clear(self):
        """Clear the entire cache."""
        with self._lock:
            self._cache.clear()
            logger.info("Method cache cleared", "method_cache")


# Global cache instance
_method_cache: Optional[MethodCache] = None


def get_method_cache() -> MethodCache:
    """Get the global method cache instance."""
    global _method_cache
    if _method_cache is None:
        _method_cache = MethodCache()
    return _method_cache
