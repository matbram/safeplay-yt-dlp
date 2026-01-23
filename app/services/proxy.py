"""OxyLabs residential proxy configuration for yt-dlp with speed optimizations.

SMART SESSION ROTATION
======================
This module implements intelligent proxy session management:
- Sticky sessions keep same IP for download consistency
- Fresh sessions on demand when bot detection occurs
- Multiple country options for geo-restricted content
"""

import uuid
import time
from dataclasses import dataclass
from typing import Optional, Set
from app.config import settings
from app.services import logger


# OxyLabs endpoints:
# - pr.oxylabs.io:7777 - SOCKS5/HTTP residential (rotating or sticky)
# - dc.oxylabs.io:8000 - Datacenter with country targeting

# Target US proxies for fastest YouTube CDN connection (NYC droplet = close to US CDNs)
PROXY_COUNTRY = "US"

# Alternative countries to try if US fails (geo-diversification)
FALLBACK_COUNTRIES = ["CA", "GB", "DE", "AU"]

# Sticky session duration in minutes (max 1440 = 24 hours)
# Keeps same IP for entire download - critical for yt-dlp multi-request pattern
SESSION_TIME_MINUTES = 30


# === BAD PROXY EXIT DETECTION ===
# Track which session IDs have hit bot detection or bad exits
_bad_sessions: Set[str] = set()
_session_failures: dict[str, int] = {}  # session_id -> failure count

# Bad proxy symptoms that should trigger immediate session rotation
BAD_PROXY_SYMPTOMS = [
    "confirm you're not a bot",
    "confirm your not a bot",
    "sign in to confirm",
    "login_required",
    "bot detection",
    "unusual traffic",
    "automated requests",
    "429",  # Rate limited
    "403",  # Forbidden (IP banned)
]


@dataclass
class ProxySession:
    """Represents a proxy session with tracking metadata."""
    session_id: str
    proxy_url: str
    country: str
    created_at: float
    failure_count: int = 0
    is_bad: bool = False


def is_bad_proxy_symptom(error_msg: str) -> bool:
    """
    Check if an error message indicates a bad proxy exit.

    Bad proxy exits include:
    - Bot detection
    - IP bans (403)
    - Rate limiting (429)
    - Login required prompts

    Args:
        error_msg: The error message to check

    Returns:
        bool: True if this indicates a bad proxy that should be rotated
    """
    if not error_msg:
        return False
    error_lower = error_msg.lower()
    return any(symptom in error_lower for symptom in BAD_PROXY_SYMPTOMS)


def mark_session_bad(session_id: str, reason: str = "unknown") -> None:
    """
    Mark a proxy session as bad so it won't be reused.

    Args:
        session_id: The session ID to mark
        reason: Why the session is bad (for logging)
    """
    _bad_sessions.add(session_id)
    _session_failures[session_id] = _session_failures.get(session_id, 0) + 1
    logger.warn(
        f"Marked proxy session as bad: {session_id[:20]}... ({reason})",
        "proxy",
        {"session_id": session_id[:20], "reason": reason, "total_bad": len(_bad_sessions)}
    )


def is_session_bad(session_id: str) -> bool:
    """Check if a session has been marked as bad."""
    return session_id in _bad_sessions


def get_fresh_session_id(prefix: str = "") -> str:
    """
    Generate a completely fresh session ID guaranteed not to reuse a bad session.

    Args:
        prefix: Optional prefix for the session ID

    Returns:
        str: A fresh unique session ID
    """
    # Include timestamp to ensure uniqueness even across restarts
    timestamp = int(time.time() * 1000) % 1000000
    unique = uuid.uuid4().hex[:8]
    session_id = f"{prefix}-{timestamp}-{unique}" if prefix else f"fresh-{timestamp}-{unique}"

    # Ensure we don't accidentally reuse a bad session
    while session_id in _bad_sessions:
        unique = uuid.uuid4().hex[:8]
        session_id = f"{prefix}-{timestamp}-{unique}" if prefix else f"fresh-{timestamp}-{unique}"

    return session_id


def get_proxy_config(job_id: str = None, country: str = None, force_fresh: bool = False) -> dict:
    """
    Get proxy configuration for yt-dlp with sticky sessions.

    Uses HTTP proxy with:
    - Country targeting (default US) for YouTube CDN proximity
    - Sticky sessions to maintain same IP across all requests in a download
    - Smart session rotation to avoid bad IPs

    Args:
        job_id: Optional job ID to use as session identifier. If not provided,
                a unique session ID is generated.
        country: Optional country code override (default: US)
        force_fresh: If True, always generate a new session ID (for bad proxy rotation)

    Returns:
        dict: Proxy configuration options for yt-dlp
    """
    # Use specified country or default
    target_country = country or PROXY_COUNTRY

    # Generate session ID
    if force_fresh or not job_id:
        session_id = get_fresh_session_id(job_id or "")
    else:
        session_id = job_id
        # Check if this session was marked bad
        if is_session_bad(session_id):
            logger.warn(f"Session {session_id[:20]}... was bad, generating fresh", "proxy")
            session_id = get_fresh_session_id(job_id)

    # OxyLabs HTTP with sticky session
    # Format: USERNAME-cc-COUNTRY-sessid-ID-sesstime-MINUTES
    # - cc-XX: Country targeting (no city - adds overhead without benefit)
    # - sessid: Unique session ID keeps same IP for all requests
    # - sesstime: Session duration in minutes
    proxy_url = (
        f"http://{settings.OXYLABS_USERNAME}-cc-{target_country}"
        f"-sessid-{session_id}-sesstime-{SESSION_TIME_MINUTES}"
        f":{settings.OXYLABS_PASSWORD}@pr.oxylabs.io:7777"
    )

    logger.debug(
        f"Proxy configured: {target_country} HTTP sticky session ({session_id}, {SESSION_TIME_MINUTES}min)",
        "proxy"
    )

    return {
        "proxy": proxy_url,
        "session_id": session_id,  # Return session ID for tracking
        "country": target_country,
        # Reduced timeout - fail fast, retry with new proxy
        "socket_timeout": 30,
        # Fewer retries per session - we handle rotation at higher level
        "retries": 2,
    }


def get_rotating_proxy() -> str:
    """
    Get a rotating residential proxy URL.
    Each request gets a different IP automatically.

    Returns:
        str: Proxy URL string
    """
    return (
        f"http://{settings.OXYLABS_USERNAME}:{settings.OXYLABS_PASSWORD}"
        f"@pr.oxylabs.io:7777"
    )


def get_country_proxy(country_code: str = "us") -> str:
    """
    Get a country-specific residential proxy.

    Args:
        country_code: Two-letter country code (e.g., 'us', 'uk', 'de')

    Returns:
        str: Country-specific proxy URL
    """
    return (
        f"http://{settings.OXYLABS_USERNAME}-country-{country_code}"
        f":{settings.OXYLABS_PASSWORD}@pr.oxylabs.io:7777"
    )


def get_next_fallback_country(attempt: int) -> str:
    """
    Get a fallback country for geo-diversification.

    Cycles through fallback countries on repeated failures.

    Args:
        attempt: Current attempt number (0-indexed)

    Returns:
        str: Country code to try
    """
    if attempt == 0:
        return PROXY_COUNTRY
    # Cycle through fallback countries
    idx = (attempt - 1) % len(FALLBACK_COUNTRIES)
    return FALLBACK_COUNTRIES[idx]


def clear_bad_sessions() -> int:
    """
    Clear all bad session tracking (e.g., on service restart).

    Returns:
        int: Number of sessions cleared
    """
    count = len(_bad_sessions)
    _bad_sessions.clear()
    _session_failures.clear()
    if count > 0:
        logger.info(f"Cleared {count} bad proxy sessions", "proxy")
    return count


def get_bad_session_count() -> int:
    """Get count of sessions marked as bad."""
    return len(_bad_sessions)


async def test_proxy_connection() -> bool:
    """
    Test if proxy connection is working.

    Returns:
        bool: True if proxy is reachable
    """
    import httpx

    proxy_url = get_rotating_proxy()

    try:
        async with httpx.AsyncClient(proxy=proxy_url, timeout=10.0) as client:
            response = await client.get("https://httpbin.org/ip")
            return response.status_code == 200
    except Exception:
        return False
