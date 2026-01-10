"""Health check endpoint."""

from datetime import datetime

from fastapi import APIRouter

from app.models.schemas import HealthCheck
from app.services.storage import test_supabase_connection
from app.services.proxy import test_proxy_connection


router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthCheck)
async def health_check() -> HealthCheck:
    """
    Health check endpoint for Railway.

    Returns system health status including:
    - yt-dlp availability
    - Supabase connection
    - Proxy reachability
    """
    # Check yt-dlp
    ytdlp_available = True
    try:
        import yt_dlp
        _ = yt_dlp.YoutubeDL
    except ImportError:
        ytdlp_available = False

    # Check Supabase
    supabase_connected = test_supabase_connection()

    # Check proxy (async)
    proxy_reachable = await test_proxy_connection()

    return HealthCheck(
        status="ok" if all([ytdlp_available, supabase_connected]) else "degraded",
        timestamp=datetime.utcnow().isoformat() + "Z",
        checks={
            "ytdlp": "available" if ytdlp_available else "unavailable",
            "supabase": "connected" if supabase_connected else "disconnected",
            "proxy": "reachable" if proxy_reachable else "unreachable",
        }
    )
