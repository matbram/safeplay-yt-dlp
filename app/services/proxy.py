"""OxyLabs residential proxy configuration for yt-dlp with speed optimizations."""

from app.config import settings
from app.services import logger


# OxyLabs endpoints:
# - pr.oxylabs.io:7777 - HTTP/HTTPS residential (rotating)
# - dc.oxylabs.io:8000 - Datacenter with country targeting
# - residential.oxylabs.io:60000 - SOCKS5 residential

# Target US proxies for fastest YouTube CDN connection
PROXY_COUNTRY = "US"


def get_proxy_config() -> dict:
    """
    Get optimized proxy configuration for yt-dlp.
    Uses US-based proxy for faster YouTube CDN routing.

    Returns:
        dict: Proxy configuration options for yt-dlp
    """
    # OxyLabs format for country targeting:
    # customer-USERNAME-cc-COUNTRY:PASSWORD@pr.oxylabs.io:7777
    # Note: OXYLABS_USERNAME already contains "customer-" prefix
    proxy_url = (
        f"http://{settings.OXYLABS_USERNAME}-cc-{PROXY_COUNTRY}"
        f":{settings.OXYLABS_PASSWORD}@pr.oxylabs.io:7777"
    )

    logger.debug(f"Proxy configured: US residential via pr.oxylabs.io:7777", "proxy")

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
