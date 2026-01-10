"""SafePlay YT-DLP Download Service - Main FastAPI Application."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routes import download, status, health


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler for startup and shutdown."""
    # Startup
    print(f"SafePlay Download Service starting on port {settings.PORT}")

    # Verify yt-dlp is available
    try:
        import yt_dlp
        print(f"yt-dlp version: {yt_dlp.version.__version__}")
    except ImportError as e:
        raise RuntimeError("yt-dlp not installed") from e

    # Create temp directory if it doesn't exist
    from pathlib import Path
    Path(settings.TEMP_DIR).mkdir(parents=True, exist_ok=True)
    print(f"Temp directory: {settings.TEMP_DIR}")

    print("SafePlay Download Service started successfully")

    yield

    # Shutdown
    print("SafePlay Download Service shutting down")

    # Clean up old temp files on shutdown
    from app.services.youtube import cleanup_old_temp_files
    cleaned = cleanup_old_temp_files(max_age_hours=1)
    if cleaned > 0:
        print(f"Cleaned up {cleaned} old temp directories")


# Create FastAPI app
app = FastAPI(
    title="SafePlay Download Service",
    description="YouTube video download service using yt-dlp and OxyLabs proxies",
    version="1.0.0",
    docs_url="/docs" if settings.ENVIRONMENT == "development" else None,
    redoc_url=None,
    lifespan=lifespan,
)

# CORS middleware - Internal service, but add minimal config
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Internal service only
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Include routers
app.include_router(download.router)
app.include_router(status.router)
app.include_router(health.router)


@app.get("/", include_in_schema=False)
async def root():
    """Root endpoint redirects to health check."""
    return {"service": "safeplay-downloader", "status": "running"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.PORT,
        reload=settings.ENVIRONMENT == "development",
    )
