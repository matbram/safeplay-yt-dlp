"""OxyLabs residential proxy configuration for yt-dlp with speed optimizations."""

import uuid
from app.config import settings
from app.services import logger


# OxyLabs endpoints:
# - pr.oxylabs.io:7777 - SOCKS5/HTTP residential (rotating or sticky)
# - dc.oxylabs.io:8000 - Datacenter with country targeting

# Target US proxies for fastest YouTube CDN connection (NYC droplet = close to US CDNs)
PROXY_COUNTRY = "US"

# Sticky session duration in minutes (max 1440 = 24 hours)
# Keeps same IP for entire download - critical for yt-dlp multi-request pattern
SESSION_TIME_MINUTES = 30


def get_proxy_config(job_id: str = None) -> dict:
    """
    Get proxy configuration for yt-dlp with sticky sessions.

    Uses HTTP proxy with:
    - US country targeting for YouTube CDN proximity
    - Sticky sessions to maintain same IP across all requests in a download

    Args:
        job_id: Optional job ID to use as session identifier. If not provided,
                a unique session ID is generated.

    Returns:
        dict: Proxy configuration options for yt-dlp
    """
    # Generate unique session ID for sticky session
    session_id = job_id if job_id else f"sess_{uuid.uuid4().hex[:12]}"

    # OxyLabs HTTP with sticky session
    # Format: USERNAME-cc-COUNTRY-sessid-ID-sesstime-MINUTES
    # - cc-US: Country targeting (no city - adds overhead without benefit)
    # - sessid: Unique session ID keeps same IP for all requests
    # - sesstime: Session duration in minutes
    proxy_url = (
        f"http://{settings.OXYLABS_USERNAME}-cc-{PROXY_COUNTRY}"
        f"-sessid-{session_id}-sesstime-{SESSION_TIME_MINUTES}"
        f":{settings.OXYLABS_PASSWORD}@pr.oxylabs.io:7777"
    )

    logger.debug(
        f"Proxy configured: US HTTP sticky session ({session_id}, {SESSION_TIME_MINUTES}min)",
        "proxy"
    )

    return {
        "proxy": proxy_url,
        # Increased timeout for video downloads
        "socket_timeout": 60,
        # More retries for reliability
        "retries": 5,
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
