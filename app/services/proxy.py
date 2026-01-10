"""OxyLabs residential proxy configuration for yt-dlp."""

from app.config import settings


def get_proxy_config() -> dict:
    """
    Get proxy configuration for yt-dlp.
    OxyLabs residential proxy format: http://user:pass@pr.oxylabs.io:7777

    Returns:
        dict: Proxy configuration options for yt-dlp
    """
    proxy_url = (
        f"http://{settings.OXYLABS_USERNAME}:{settings.OXYLABS_PASSWORD}"
        f"@pr.oxylabs.io:7777"
    )

    return {
        "proxy": proxy_url,
        # Additional options for better success rate
        "socket_timeout": 30,
        "retries": 3,
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
