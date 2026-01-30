"""YouTube audio download service with proxy-based extraction.

DOWNLOAD STRATEGY
=================
This service downloads audio from YouTube using residential proxies:

- Uses Oxylabs residential proxy for extraction AND download
- Downloads lowest quality audio (targets ~64kbps) for smallest file sizes
- Uses aria2c for fast multi-connection downloads (8 connections)
- Supports smart retry with proxy rotation and player client cycling

AUDIO-FIRST APPROACH:
- Default mode downloads audio-only (smallest files, fastest processing)
- Video download functionality is preserved but not the default
- Audio quality targets lowest available bitrate (~64kbps)

EXPECTED COSTS:
- ~$0.12/video (proxy bandwidth)
"""

import asyncio
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

import yt_dlp

from app.config import settings
from app.services.proxy import (
    get_proxy_config,
    is_bad_proxy_symptom,
    mark_session_bad,
    get_fresh_session_id,
)
from app.services import logger
from app.utils.exceptions import (
    VideoUnavailableError,
    PrivateVideoError,
    AgeRestrictedError,
    CopyrightBlockedError,
    LiveStreamError,
    PremiumContentError,
    DownloadError,
    DownloadTimeoutError,
    BotDetectionError,
    CDNAccessError,
    PERMANENT_ERRORS,
)

# === VIDEO RESTRICTION DETECTION ===
# Age limit threshold - YouTube uses 18 for age-restricted content
AGE_RESTRICTION_THRESHOLD = 18

# === RELIABILITY CONFIGURATION ===
DOWNLOAD_TIMEOUT_SECONDS = 180  # Max time for a single download attempt (3 min for large files + ffmpeg)
MAX_RETRY_ATTEMPTS = 4  # Reduced - mweb first should succeed quickly
QUICK_RETRY_BATCH = 2  # Quick retry same client once, then rotate
QUICK_RETRY_DELAY = 0.5  # Very short delay for quick retries (just get a fresh IP)
RETRY_BACKOFF_SECONDS = [1, 2, 3]  # Shorter backoff for remaining retries
CIRCUIT_BREAKER_DELAY = 5  # Reduced - faster circuit breaker with better clients
MIN_DOWNLOAD_SPEED_KB = 250  # Minimum acceptable download speed in KB/s (abort if slower)

# === PLAYER CLIENT ROTATION ===
# Optimized for residential proxy usage (tested with Oxylabs):
# Key insights from production testing (Jan 2026):
#   - ios: BEST with proxies - progressive URLs, avoids SABR, no bot detection
#   - mweb: Previously reliable but now triggers bot detection with proxies
#   - android_sdkless: DEPRECATED - yt-dlp skips as "unsupported"
#   - web_safari/web: Gets SABR-blocked formats ("Requested format not available")
#   - tv_embedded: Alternative for progressive URLs, low bot detection
# Order prioritizes ios which provides progressive download URLs without SABR
PLAYER_CLIENTS = [
    ["ios"],                   # BEST with proxies: Progressive URLs, avoids SABR
    ["mweb"],                  # Fallback: Mobile web (may trigger bot detection)
    ["ios", "mweb"],           # Combined: ios primary with mweb backup
    ["tv_embedded"],           # TV client: Progressive URLs, low bot detection
]

# Thread pool for running blocking downloads (8 workers for parallel capacity)
_download_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ytdlp")


def _check_aria2c_available() -> bool:
    """Check if aria2c is available for faster downloads."""
    try:
        result = subprocess.run(["aria2c", "--version"], capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def _check_nodejs_available() -> bool:
    """Check if Node.js is available for YouTube JS challenges."""
    try:
        result = subprocess.run(["node", "--version"], capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


# Check aria2c availability at startup
ARIA2C_AVAILABLE = _check_aria2c_available()
if ARIA2C_AVAILABLE:
    logger.info("aria2c detected - will use for faster multi-connection downloads", "ytdlp")
else:
    logger.warn("aria2c not found - downloads will be slower. Install with: apt install aria2", "ytdlp")

# Check Node.js availability at startup (required for YouTube bot challenges)
NODEJS_AVAILABLE = _check_nodejs_available()
if NODEJS_AVAILABLE:
    logger.info("Node.js detected - will use for YouTube JS challenges", "ytdlp")
else:
    logger.error("Node.js NOT found - YouTube downloads may fail with bot detection! Install with: apt install nodejs", "ytdlp")


# === COST TRACKING ===
COST_PER_DOWNLOAD = 0.12  # ~$0.12 per video (proxy bandwidth)


@dataclass
class DownloadResult:
    """Result of an audio/video download operation."""
    success: bool
    file_path: Optional[str] = None
    title: str = "Unknown"
    duration_seconds: int = 0
    youtube_id: str = ""
    ext: str = "m4a"
    filesize_bytes: int = 0
    download_time: float = 0.0
    method: str = "proxy"
    cost: float = 0.0
    error: Optional[str] = None
    cancelled: bool = False


# In-memory job progress tracking
job_progress: dict[str, dict] = {}

# Set of job IDs that should be cancelled
cancelled_jobs: set[str] = set()


def cancel_job(job_id: str) -> bool:
    """
    Request cancellation of a specific job.

    Args:
        job_id: The job ID to cancel

    Returns:
        bool: True if job was found and marked for cancellation
    """
    if job_id in job_progress:
        cancelled_jobs.add(job_id)
        job_progress[job_id]["status"] = "cancelling"
        logger.info(f"Job cancellation requested: {job_id}", "download", {"job_id": job_id})
        return True
    return False


def cancel_all_jobs() -> int:
    """
    Cancel all active jobs.

    Returns:
        int: Number of jobs marked for cancellation
    """
    count = 0
    for job_id, progress in job_progress.items():
        if progress.get("status") in ("pending", "downloading", "uploading"):
            cancelled_jobs.add(job_id)
            job_progress[job_id]["status"] = "cancelling"
            count += 1
    if count > 0:
        logger.info(f"Cancelled {count} active jobs", "download")
    return count


def clear_completed_jobs() -> int:
    """
    Clear all completed/failed/cancelled jobs from tracking.

    Returns:
        int: Number of jobs cleared
    """
    to_remove = [
        job_id for job_id, progress in job_progress.items()
        if progress.get("status") in ("completed", "failed", "cancelled")
    ]
    for job_id in to_remove:
        del job_progress[job_id]
        cancelled_jobs.discard(job_id)
    return len(to_remove)


def is_job_cancelled(job_id: str) -> bool:
    """Check if a job has been cancelled."""
    return job_id in cancelled_jobs


class DownloadCancelled(Exception):
    """Raised when a download is cancelled."""
    pass


def _progress_hook(d: dict, job_id: str) -> None:
    """Track download progress with detailed logging."""
    # Check for cancellation
    if is_job_cancelled(job_id):
        logger.info(f"Download cancelled by user", "download", {"job_id": job_id})
        raise DownloadCancelled(f"Job {job_id} was cancelled")

    if d["status"] == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
        downloaded = d.get("downloaded_bytes", 0)
        speed = d.get("speed", 0)
        eta = d.get("eta", 0)

        if total > 0:
            progress = int((downloaded / total) * 75)
            job_progress[job_id] = {
                "status": "downloading",
                "progress": progress,
                "downloaded": downloaded,
                "total": total,
                "speed": speed,
                "eta": eta,
            }

            # Log progress every 10%
            if progress % 10 == 0 and progress > 0:
                speed_mb = (speed or 0) / (1024 * 1024)
                logger.debug(
                    f"Download progress: {progress}% ({downloaded}/{total} bytes, {speed_mb:.2f} MB/s, ETA: {eta}s)",
                    "ytdlp",
                    {"job_id": job_id, "progress": progress, "speed_mbps": speed_mb}
                )

    elif d["status"] == "finished":
        filename = d.get("filename", "unknown")
        logger.info(f"Download finished: {filename}", "ytdlp", {"job_id": job_id})
        job_progress[job_id] = {
            "status": "uploading",
            "progress": 75,
        }

    elif d["status"] == "error":
        logger.error(f"Download error in progress hook", "ytdlp", {"job_id": job_id, "data": str(d)})


def _is_bot_detection_error(error_msg: str) -> bool:
    """Check if the error is a bot detection error."""
    error_lower = error_msg.lower()
    return "confirm you're not a bot" in error_lower or "confirm your not a bot" in error_lower


async def check_video_restrictions(youtube_id: str, job_id: str) -> Optional[dict]:
    """
    Early check for video restrictions BEFORE attempting download.

    This is much faster than failing during download because it only fetches
    metadata without downloading any media. Detects:
    - Age-restricted content (requires sign-in)
    - Private videos
    - Unavailable videos
    - Live streams
    - Premium/members-only content

    Args:
        youtube_id: YouTube video ID
        job_id: Job tracking ID for logging

    Returns:
        None if video is downloadable, or dict with restriction info if blocked

    Raises:
        Appropriate exception for permanent restrictions
    """
    url = f"https://www.youtube.com/watch?v={youtube_id}"

    logger.info(
        f"Checking video restrictions: {youtube_id}",
        "download",
        {"job_id": job_id, "youtube_id": youtube_id}
    )

    # Use proxy for metadata check too (some restrictions are geo-based)
    proxy_session_id = f"check-{job_id}-{uuid.uuid4().hex[:8]}"
    proxy_config = get_proxy_config(proxy_session_id)

    ydl_opts = {
        **proxy_config,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,  # We need full info for age_limit
        "socket_timeout": 15,
    }

    def _blocking_check():
        """Run blocking metadata extraction."""
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        loop = asyncio.get_event_loop()
        info = await asyncio.wait_for(
            loop.run_in_executor(_download_executor, _blocking_check),
            timeout=30  # 30 second timeout for metadata check
        )

        # Check for age restriction
        age_limit = info.get("age_limit", 0)
        if age_limit and age_limit >= AGE_RESTRICTION_THRESHOLD:
            logger.warn(
                f"Age-restricted video detected: {youtube_id} (age_limit={age_limit})",
                "download",
                {"job_id": job_id, "youtube_id": youtube_id, "age_limit": age_limit}
            )
            raise AgeRestrictedError(
                f"Video is age-restricted (age_limit={age_limit}). "
                "YouTube requires sign-in to verify age for this content."
            )

        # Check for live stream
        is_live = info.get("is_live", False)
        was_live = info.get("was_live", False)
        if is_live:
            logger.warn(
                f"Live stream detected: {youtube_id}",
                "download",
                {"job_id": job_id, "youtube_id": youtube_id}
            )
            raise LiveStreamError("This is a live stream and cannot be processed until it ends.")

        # Check for premium/members-only content
        availability = info.get("availability", "")
        if availability in ("premium_only", "subscriber_only"):
            logger.warn(
                f"Premium content detected: {youtube_id} (availability={availability})",
                "download",
                {"job_id": job_id, "youtube_id": youtube_id, "availability": availability}
            )
            raise PremiumContentError(
                f"Video requires YouTube Premium or channel membership (availability={availability})."
            )

        # Video is accessible - return metadata for potential use
        logger.debug(
            f"Video is accessible: {youtube_id}",
            "download",
            {
                "job_id": job_id,
                "youtube_id": youtube_id,
                "title": info.get("title", "Unknown"),
                "duration": info.get("duration", 0),
                "age_limit": age_limit,
            }
        )

        return {
            "accessible": True,
            "title": info.get("title"),
            "duration": info.get("duration"),
            "age_limit": age_limit,
        }

    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        logger.warn(
            f"Video restriction check failed: {error_msg[:100]}",
            "download",
            {"job_id": job_id, "youtube_id": youtube_id}
        )
        # Classify the error and raise appropriate exception
        raise _classify_error(error_msg)

    except asyncio.TimeoutError:
        # Metadata check timed out - don't block on this, let download try
        logger.warn(
            f"Video restriction check timed out, proceeding with download",
            "download",
            {"job_id": job_id, "youtube_id": youtube_id}
        )
        return {"accessible": True, "check_timed_out": True}

    except PERMANENT_ERRORS:
        # Re-raise permanent errors (already classified)
        raise


def _classify_error(error_msg: str) -> Exception:
    """
    Classify an error message into the appropriate exception type.

    This determines whether the orchestration service should retry
    or show an error to the user immediately.
    """
    error_lower = error_msg.lower()

    # Check for bot detection FIRST - this is retryable with new IP
    if _is_bot_detection_error(error_msg):
        return BotDetectionError(error_msg)  # Special handling - needs longer delays

    # Check for aria2c HTTP errors - these indicate CDN access issues
    # aria2c exit code 22 = HTTP request was not successful (4xx/5xx)
    if "aria2c exited with code 22" in error_lower:
        return CDNAccessError(
            "aria2c download failed: YouTube CDN returned HTTP error (likely 403 Forbidden). "
            "This means the download URL was rejected, possibly due to IP mismatch or URL expiration."
        )

    # Check for explicit HTTP 403/Forbidden errors
    if "status=403" in error_lower or "403 forbidden" in error_lower:
        return CDNAccessError(
            f"YouTube CDN access denied (HTTP 403): {error_msg}"
        )

    # aria2c exit code 3 = resource not found (404)
    if "aria2c exited with code 3" in error_lower:
        return DownloadError(
            "aria2c download failed: Resource not found (HTTP 404). "
            "The download URL may have expired. Retrying with fresh URL."
        )

    # aria2c exit code 6 = network problem
    if "aria2c exited with code 6" in error_lower:
        return DownloadError(
            "aria2c download failed: Network error. "
            "Connection issue with proxy or YouTube CDN."
        )

    # aria2c exit code 9 = not enough disk space
    if "aria2c exited with code 9" in error_lower:
        return DownloadError(
            "aria2c download failed: Not enough disk space."
        )

    # aria2c exit code 24 = HTTP authorization failed
    if "aria2c exited with code 24" in error_lower:
        return CDNAccessError(
            "aria2c download failed: HTTP authorization failed. "
            "The download URL signature may be invalid."
        )

    # Generic aria2c errors - provide helpful context
    if "aria2c exited with code" in error_lower:
        # Extract the exit code for the message
        import re
        match = re.search(r'aria2c exited with code (\d+)', error_lower)
        exit_code = match.group(1) if match else "unknown"
        return DownloadError(
            f"aria2c download failed with exit code {exit_code}. "
            "External downloader encountered an error. Retrying with fresh connection."
        )

    # Permanent errors - video cannot be downloaded
    if "video unavailable" in error_lower or "removed" in error_lower or "does not exist" in error_lower:
        return VideoUnavailableError(error_msg)
    elif "private video" in error_lower or "video is private" in error_lower:
        return PrivateVideoError(error_msg)
    elif "age" in error_lower and "restrict" in error_lower:
        return AgeRestrictedError(error_msg)
    elif "copyright" in error_lower or "blocked" in error_lower:
        return CopyrightBlockedError(error_msg)
    elif "live" in error_lower and ("stream" in error_lower or "event" in error_lower):
        return LiveStreamError(error_msg)
    elif "premium" in error_lower or "members only" in error_lower or "membership" in error_lower:
        return PremiumContentError(error_msg)
    elif "join this channel" in error_lower:
        return PremiumContentError(error_msg)
    else:
        # Default to retryable download error
        return DownloadError(error_msg)


# =============================================================================
# PHASE 1: METADATA & URL EXTRACTION (WITH PROXY)
# =============================================================================

async def extract_audio_url(
    youtube_id: str,
    job_id: str,
    proxy_session_id: str,
) -> dict:
    """
    PHASE 1: Extract audio metadata and signed CDN URL using proxy.

    This function uses yt-dlp with the Oxylabs proxy to:
    1. Fetch video metadata (title, duration, etc.)
    2. Extract the direct download URL for the lowest bitrate audio format
    3. Return all info WITHOUT downloading the actual file

    Bandwidth usage: ~500KB-1MB per video (metadata only)

    Args:
        youtube_id: YouTube video ID
        job_id: Job tracking ID
        proxy_session_id: Unique session ID for proxy sticky session

    Returns:
        dict with:
            - title: Video title
            - duration_seconds: Video duration
            - cdn_url: Signed URL for direct download (googlevideo.com)
            - ext: File extension (m4a, webm, etc.)
            - filesize_approx: Approximate file size in bytes

    Raises:
        Various exceptions for permanent errors (age-restricted, private, etc.)
    """
    url = f"https://www.youtube.com/watch?v={youtube_id}"

    # Get proxy config for metadata extraction
    proxy_config = get_proxy_config(proxy_session_id)
    if proxy_config.get("proxy"):
        proxy_url = proxy_config["proxy"]
        masked_proxy = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
        logger.info(
            f"[PHASE 1] Extracting metadata via proxy: {masked_proxy}",
            "proxy",
            {"job_id": job_id, "youtube_id": youtube_id, "session_id": proxy_session_id}
        )

    # Create custom logger for yt-dlp
    ytdlp_logger = logger.YtdlpLogger(job_id)

    # yt-dlp options for metadata extraction only
    ydl_opts = {
        **proxy_config,
        "quiet": False,
        "verbose": True,
        "logger": ytdlp_logger,
        "skip_download": True,  # CRITICAL: Don't download, just extract info
        "extract_flat": False,  # We need full format info with URLs
        # Audio-only - lowest bitrate for smallest files
        # IMPORTANT: Do NOT include 'best' fallback as it includes video
        # Chain of fallbacks: HTTPS worstaudio -> any worstaudio -> m4a bestaudio -> any bestaudio
        "format": "worstaudio[protocol=https]/worstaudio/bestaudio[ext=m4a]/bestaudio",
        "format_sort": ["abr"],
        "geo_bypass": True,
        "socket_timeout": 30,
        # Prefer English audio track
        # Use ios client for progressive URLs without SABR
        "extractor_args": {
            "youtube": {
                "lang": ["en", "en-US", "en-GB"],
                "player_client": ["ios"],
                # SABR Prevention - force single-file progressive downloads
                "skip": ["dash", "hls"],  # Skip DASH/HLS manifests entirely
                "formats": "missing_pot:skip",  # Skip formats that would require SABR (missing PO token URLs)
            }
        },
    }

    # Enable Node.js for YouTube bot challenge solving
    if NODEJS_AVAILABLE:
        ydl_opts["js_runtimes"] = {"node": {}}
        # Enable remote EJS challenge solver for n parameter deobfuscation
        ydl_opts["remote_components"] = {"ejs:github"}

    start_time = time.time()

    def _blocking_extract():
        """Run blocking metadata extraction."""
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info

    try:
        loop = asyncio.get_event_loop()
        info = await asyncio.wait_for(
            loop.run_in_executor(_download_executor, _blocking_extract),
            timeout=60  # 60 second timeout for metadata extraction
        )

        extraction_time = time.time() - start_time

        # Check for age restriction (may have been missed in earlier check)
        age_limit = info.get("age_limit", 0)
        if age_limit and age_limit >= AGE_RESTRICTION_THRESHOLD:
            raise AgeRestrictedError(
                f"Video is age-restricted (age_limit={age_limit}). "
                "YouTube requires sign-in to verify age for this content."
            )

        # Extract the direct URL from the selected format
        # When format is selected, yt-dlp populates 'url' in the info dict
        cdn_url = info.get("url")

        # If not directly available, look in requested_formats
        if not cdn_url and info.get("requested_formats"):
            # Get the audio format
            for fmt in info["requested_formats"]:
                if fmt.get("url"):
                    cdn_url = fmt.get("url")
                    break

        # Fallback: look in formats list for audio-only formats
        if not cdn_url and info.get("formats"):
            # Find audio-only formats (no video codec)
            audio_formats = [
                f for f in info["formats"]
                if f.get("vcodec") == "none" and f.get("acodec") != "none"
            ]
            if audio_formats:
                # Sort by bitrate (abr) ascending to get lowest quality audio
                audio_formats.sort(key=lambda f: f.get("abr") or f.get("filesize") or 0)
                cdn_url = audio_formats[0].get("url")

        if not cdn_url:
            raise DownloadError(
                f"Could not extract CDN URL for {youtube_id}. "
                "No audio format URLs found in metadata."
            )

        # Get file extension and size
        ext = info.get("ext", "m4a")
        filesize_approx = info.get("filesize") or info.get("filesize_approx") or 0

        logger.success(
            f"[PHASE 1 COMPLETE] Metadata extracted via proxy in {extraction_time:.1f}s",
            "download",
            {
                "job_id": job_id,
                "youtube_id": youtube_id,
                "title": info.get("title", "Unknown")[:50],
                "duration": info.get("duration", 0),
                "ext": ext,
                "filesize_approx_mb": round(filesize_approx / (1024 * 1024), 2) if filesize_approx else "unknown",
                "cdn_host": cdn_url.split("/")[2] if cdn_url else "unknown",
                "extraction_time_seconds": round(extraction_time, 2),
            }
        )

        return {
            "title": info.get("title", "Unknown"),
            "duration_seconds": info.get("duration", 0),
            "cdn_url": cdn_url,
            "ext": ext,
            "filesize_approx": filesize_approx,
            "youtube_id": youtube_id,
        }

    except asyncio.TimeoutError:
        duration = time.time() - start_time
        logger.error(
            f"[PHASE 1] Metadata extraction timed out after {duration:.1f}s",
            "download",
            {"job_id": job_id, "youtube_id": youtube_id}
        )
        raise DownloadTimeoutError(f"Metadata extraction timed out after 60s")

    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        logger.warn(
            f"[PHASE 1] Metadata extraction failed: {error_msg[:100]}",
            "download",
            {"job_id": job_id, "youtube_id": youtube_id}
        )
        raise _classify_error(error_msg)


# =============================================================================
# PHASE 2: CDN DOWNLOAD (with aria2c multi-connection)
# =============================================================================

async def download_from_cdn(
    cdn_url: str,
    youtube_id: str,
    job_id: str,
    ext: str,
    title: str = "Unknown",
    proxy_url: str = None,
) -> dict:
    """
    PHASE 2: Download video file from CDN using aria2c multi-connection.

    This function downloads the file from Google's CDN using aria2c for
    fast multi-connection downloads. Due to YouTube's IP-binding, the
    proxy_url should be the SAME proxy session used in Phase 1.

    Benefits of this approach:
    - 8 parallel connections for faster downloads
    - Same proxy session ensures IP-bound URLs work
    - Better error handling with separate phases

    Args:
        cdn_url: Signed CDN URL from Phase 1 (googlevideo.com)
        youtube_id: YouTube video ID
        job_id: Job tracking ID
        ext: File extension (m4a, webm, etc.)
        title: Video title for logging
        proxy_url: Proxy URL (same session as Phase 1 for IP-bound URLs)

    Returns:
        dict with:
            - success: True if download succeeded
            - file_path: Path to downloaded file
            - filesize_bytes: Actual file size

    Raises:
        DownloadError: If download fails
    """
    temp_dir = Path(settings.TEMP_DIR) / job_id
    temp_dir.mkdir(parents=True, exist_ok=True)
    output_file = temp_dir / f"{youtube_id}.{ext}"

    proxy_status = "via same proxy session" if proxy_url else "NO PROXY"
    logger.info(
        f"[PHASE 2] Starting CDN download ({proxy_status})",
        "download",
        {
            "job_id": job_id,
            "youtube_id": youtube_id,
            "cdn_host": cdn_url.split("/")[2] if "/" in cdn_url else "unknown",
            "output_file": str(output_file),
            "using_proxy": bool(proxy_url),
        }
    )

    start_time = time.time()

    if ARIA2C_AVAILABLE:
        # Use aria2c for multi-connection download (fastest)
        result = await _download_with_aria2c(cdn_url, output_file, job_id, proxy_url)
    else:
        # Fallback to wget/curl (doesn't support proxy in this path)
        if proxy_url:
            logger.warn(
                "[PHASE 2] aria2c not available, falling back to wget/curl without proxy",
                "download",
                {"job_id": job_id}
            )
        result = await _download_with_fallback(cdn_url, output_file, job_id)

    if not result.get("success"):
        raise DownloadError(result.get("error", "CDN download failed"))

    download_time = time.time() - start_time
    filesize = output_file.stat().st_size if output_file.exists() else 0

    logger.success(
        f"[PHASE 2 COMPLETE] Download finished in {download_time:.1f}s ({proxy_status})",
        "download",
        {
            "job_id": job_id,
            "youtube_id": youtube_id,
            "title": title[:50],
            "filesize_mb": round(filesize / (1024 * 1024), 2),
            "download_time_seconds": round(download_time, 2),
            "speed_mbps": round((filesize / (1024 * 1024)) / download_time, 2) if download_time > 0 else 0,
            "using_proxy": bool(proxy_url),
        }
    )

    return {
        "success": True,
        "file_path": str(output_file),
        "filesize_bytes": filesize,
        "download_time": download_time,
    }


async def _download_with_aria2c(
    cdn_url: str,
    output_file: Path,
    job_id: str,
    proxy_url: str = None,
) -> dict:
    """
    Download file using aria2c with multi-connection support.

    Uses the same aria2c configuration as the existing yt-dlp integration:
    - 8 connections per server
    - 8 concurrent segments
    - 1MB minimum segment size
    - Optional proxy support for IP-bound YouTube URLs

    Args:
        cdn_url: The CDN URL to download from
        output_file: Path to save the downloaded file
        job_id: Job ID for logging
        proxy_url: Optional proxy URL (required for IP-bound YouTube URLs)
    """
    # aria2c command with same options as existing yt-dlp integration
    cmd = [
        "aria2c",
        "-x", "8",  # Max connections per server
        "-s", "8",  # Split file into segments
        "-k", "1M",  # Min split size
        "--max-connection-per-server=8",
        "--min-split-size=1M",
        "--split=8",
        "--retry-wait=1",
        "--max-tries=0",
        "--timeout=30",
        "--file-allocation=none",
        "--auto-file-renaming=false",
        "--allow-overwrite=true",
        "-d", str(output_file.parent),
        "-o", output_file.name,
    ]

    # Add proxy if provided (required for IP-bound YouTube URLs)
    if proxy_url:
        cmd.extend(["--all-proxy", proxy_url])

    # Add URL last
    cmd.append(cdn_url)

    using_proxy = "via same proxy" if proxy_url else "no proxy"
    logger.debug(
        f"[PHASE 2] Executing aria2c (8 connections, {using_proxy})",
        "download",
        {"job_id": job_id, "output": str(output_file), "using_proxy": bool(proxy_url)}
    )

    def _run_aria2c():
        """Run aria2c in subprocess."""
        process = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )
        return process

    try:
        loop = asyncio.get_event_loop()
        process = await asyncio.wait_for(
            loop.run_in_executor(_download_executor, _run_aria2c),
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )

        if process.returncode != 0:
            # Capture full error for debugging
            full_error = (process.stderr or "") + (process.stdout or "")
            # Check for HTTP 403 which indicates IP-binding
            is_ip_bound = "403" in full_error or "Forbidden" in full_error
            logger.warn(
                f"[PHASE 2] aria2c failed (code {process.returncode}): {full_error[:300]}",
                "download",
                {"job_id": job_id, "is_ip_bound": is_ip_bound, "full_error": full_error[:500]}
            )
            return {
                "success": False,
                "error": f"aria2c error: {full_error[:200]}",
                "is_ip_bound": is_ip_bound,
            }

        if not output_file.exists():
            return {"success": False, "error": "aria2c completed but file not found"}

        return {"success": True}

    except asyncio.TimeoutError:
        logger.error(
            f"[PHASE 2] aria2c timed out after {DOWNLOAD_TIMEOUT_SECONDS}s",
            "download",
            {"job_id": job_id}
        )
        return {"success": False, "error": f"Download timed out after {DOWNLOAD_TIMEOUT_SECONDS}s"}

    except Exception as e:
        logger.error(
            f"[PHASE 2] aria2c exception: {str(e)[:100]}",
            "download",
            {"job_id": job_id}
        )
        return {"success": False, "error": str(e)}


async def _download_with_fallback(
    cdn_url: str,
    output_file: Path,
    job_id: str,
) -> dict:
    """
    Fallback download using wget or curl when aria2c is not available.
    """
    # Try wget first, then curl
    for tool, cmd in [
        ("wget", ["wget", "-q", "-O", str(output_file), cdn_url]),
        ("curl", ["curl", "-s", "-L", "-o", str(output_file), cdn_url]),
    ]:
        try:
            logger.debug(
                f"[PHASE 2] Trying {tool} fallback (no proxy)",
                "download",
                {"job_id": job_id}
            )

            def _run_cmd():
                return subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=DOWNLOAD_TIMEOUT_SECONDS,
                )

            loop = asyncio.get_event_loop()
            process = await asyncio.wait_for(
                loop.run_in_executor(_download_executor, _run_cmd),
                timeout=DOWNLOAD_TIMEOUT_SECONDS,
            )

            if process.returncode == 0 and output_file.exists():
                return {"success": True}

        except Exception as e:
            logger.debug(f"[PHASE 2] {tool} failed: {str(e)[:50]}", "download", {"job_id": job_id})
            continue

    return {"success": False, "error": "All download methods failed (wget, curl)"}


# =============================================================================
# LEGACY FUNCTION (kept for reference, no longer used in main flow)
# =============================================================================

async def _download_single_attempt(
    youtube_id: str,
    job_id: str,
    attempt: int,
    proxy_session_id: str,
    player_client: list[str] = None,
    country: str = None,
    resume_enabled: bool = True,
    is_final_attempt: bool = False,
) -> dict:
    """
    Execute a single download attempt with timeout.

    Args:
        youtube_id: YouTube video ID
        job_id: Job tracking ID
        attempt: Current attempt number (1-based)
        proxy_session_id: Unique session ID for proxy sticky session
        player_client: List of player clients to try (default: android)
        country: Country code for proxy (default: US)
        resume_enabled: Whether to enable download resume (default: True)
        is_final_attempt: If True, disable speed limit to ensure download completes

    Returns:
        dict with download result or raises exception
    """
    url = f"https://www.youtube.com/watch?v={youtube_id}"
    temp_dir = Path(settings.TEMP_DIR) / job_id
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Use specified player client or default
    if player_client is None:
        player_client = ["android"]

    # Get proxy config with unique session for this attempt
    proxy_config = get_proxy_config(proxy_session_id, country=country, force_fresh=True)
    actual_session_id = proxy_config.get("session_id", proxy_session_id)
    actual_country = proxy_config.get("country", "US")

    if proxy_config.get("proxy"):
        proxy_url = proxy_config["proxy"]
        masked_proxy = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
        logger.info(
            f"Attempt {attempt}: Using proxy {masked_proxy} (country={actual_country}, client={player_client})",
            "proxy",
            {"job_id": job_id, "attempt": attempt, "session_id": actual_session_id[:20], "country": actual_country, "player_client": player_client}
        )

    # Create custom logger for yt-dlp
    ytdlp_logger = logger.YtdlpLogger(job_id)

    # yt-dlp options optimized for SPEED with fail-fast
    ydl_opts = {
        "proxy": proxy_config.get("proxy"),
        "socket_timeout": proxy_config.get("socket_timeout", 30),
        "retries": proxy_config.get("retries", 2),
        "outtmpl": str(temp_dir / f"{youtube_id}.%(ext)s"),
        # Audio-only - lowest bitrate for smallest files
        # IMPORTANT: Do NOT include 'best' fallback as it includes video
        # Chain of fallbacks: HTTPS worstaudio -> any worstaudio -> m4a bestaudio -> any bestaudio
        "format": "worstaudio[protocol=https]/worstaudio/bestaudio[ext=m4a]/bestaudio",
        "format_sort": ["abr"],
        "progress_hooks": [lambda d: _progress_hook(d, job_id)],
        "verbose": True,
        "logger": ytdlp_logger,
        # Reduced retries for speed - we handle retries at higher level
        "fragment_retries": 2,
        "noplaylist": True,
        "geo_bypass": True,
        # Speed optimizations
        "concurrent_fragment_downloads": 8,
        "buffersize": 1024 * 64,
        "http_chunk_size": 10485760,
        # Prefer English audio track
        # Use rotating player client to find one that works
        "extractor_args": {
            "youtube": {
                "lang": ["en", "en-US", "en-GB"],
                "player_client": player_client,
                # SABR Prevention - force single-file progressive downloads
                "skip": ["dash", "hls"],  # Skip DASH/HLS manifests entirely
                "formats": "missing_pot:skip",  # Skip formats that would require SABR (missing PO token URLs)
            }
        },
    }

    # Enable Node.js for YouTube bot challenge solving
    if NODEJS_AVAILABLE:
        ydl_opts["js_runtimes"] = {"node": {}}
        # Enable remote EJS challenge solver for n parameter deobfuscation
        # This is required for web clients to get working download URLs
        ydl_opts["remote_components"] = {"ejs:github"}

    # Use aria2c if available with resume support
    # IMPORTANT: Disable aria2c when using proxies because:
    # - YouTube CDN URLs are signed with the requester's IP
    # - aria2c spawns separate connections that may exit through different IPs
    # - This causes HTTP 403 errors due to IP mismatch
    # - yt-dlp's internal downloader maintains the same connection
    use_aria2c = ARIA2C_AVAILABLE and not proxy_config.get("proxy")
    if use_aria2c:
        ydl_opts["external_downloader"] = "aria2c"
        aria2c_args = [
            # Reduced parallel connections for better proxy stability
            # 8 connections can overwhelm residential proxies causing SSL failures
            "-x", "4", "-s", "4", "-k", "1M",
            "--file-allocation=none",
            "--max-connection-per-server=4",
            "--min-split-size=1M", "--split=4",
            "--retry-wait=2", "--max-tries=5",  # More retries with longer wait
            "--timeout=30",
            "--connect-timeout=10",  # Faster connection timeout
            "--max-resume-failure-tries=5",  # Better resume handling
        ]
        # Only enforce speed limit if NOT the final attempt
        # On final attempt, let the download complete even if slow
        if not is_final_attempt:
            aria2c_args.append(f"--lowest-speed-limit={MIN_DOWNLOAD_SPEED_KB}K")
        else:
            logger.info("Final attempt: speed limit disabled, will complete even if slow", "download")
        # Enable resume if requested
        if resume_enabled:
            aria2c_args.extend(["--continue=true", "--auto-file-renaming=false"])
        ydl_opts["external_downloader_args"] = {"aria2c": aria2c_args}
    elif ARIA2C_AVAILABLE and proxy_config.get("proxy"):
        logger.debug("aria2c disabled for proxy download (using yt-dlp internal downloader to maintain IP consistency)", "download")

    start_time = time.time()

    def _blocking_download():
        """Run blocking yt-dlp download."""
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            ext = info.get("ext", "m4a")
            output_file = temp_dir / f"{youtube_id}.{ext}"
            filesize = output_file.stat().st_size if output_file.exists() else 0

            return {
                "info": info,
                "ext": ext,
                "output_file": output_file,
                "filesize": filesize,
                "duration": time.time() - start_time,
            }

    try:
        # Run with timeout
        loop = asyncio.get_event_loop()
        download_task = loop.run_in_executor(_download_executor, _blocking_download)
        download_result = await asyncio.wait_for(download_task, timeout=DOWNLOAD_TIMEOUT_SECONDS)

        info = download_result["info"]
        return {
            "success": True,
            "file_path": str(download_result["output_file"]),
            "title": info.get("title", "Unknown"),
            "duration_seconds": info.get("duration", 0),
            "youtube_id": youtube_id,
            "ext": download_result["ext"],
            "filesize_bytes": download_result["filesize"],
            "download_time": download_result["duration"],
            "proxy_session": actual_session_id,
        }

    except asyncio.TimeoutError:
        duration = time.time() - start_time
        # Mark session as potentially bad (timeout could be proxy issue)
        mark_session_bad(actual_session_id, "timeout")
        logger.warn(f"Attempt {attempt} timed out after {duration:.1f}s", "download", {
            "job_id": job_id, "attempt": attempt, "timeout": DOWNLOAD_TIMEOUT_SECONDS
        })
        raise DownloadTimeoutError(f"Download timed out after {DOWNLOAD_TIMEOUT_SECONDS}s")

    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        error_lower = error_msg.lower()

        # Determine the type of error for logging and session marking
        is_cdn_error = "aria2c exited with code 22" in error_lower or "403" in error_lower
        is_bot_error = is_bad_proxy_symptom(error_msg)

        # Mark session as bad for CDN errors or bot detection
        if is_cdn_error:
            mark_session_bad(actual_session_id, "cdn_403")
        elif is_bot_error:
            mark_session_bad(actual_session_id, "bot_detection" if "bot" in error_lower else "bad_exit")

        # Create descriptive error message for logging
        if is_cdn_error:
            log_msg = f"Attempt {attempt} failed: YouTube CDN rejected request (HTTP 403 - IP binding issue)"
        else:
            log_msg = f"Attempt {attempt} failed: {error_msg[:100]}"

        logger.warn(log_msg, "download", {
            "job_id": job_id,
            "attempt": attempt,
            "is_cdn_error": is_cdn_error,
            "is_bad_proxy": is_bot_error,
            "raw_error": error_msg[:200]
        })
        raise _classify_error(error_msg)


async def download_video(youtube_id: str, job_id: str) -> dict:
    """
    Download YouTube video using smart proxy rotation with fail-fast behavior.

    SMART RETRY STRATEGY
    ====================
    1. QUICK RETRY BATCH: First 3 attempts use minimal delay, just fresh proxy IPs
    2. PLAYER CLIENT ROTATION: Try different YouTube player clients
    3. COUNTRY ROTATION: Try different proxy countries after initial failures
    4. DOWNLOAD RESUME: Uses aria2c continue flag to resume partial downloads
    5. CIRCUIT BREAKER: Final attempt after all strategies exhausted

    The key insight: bot detection is usually IP-specific, so getting a fresh IP
    quickly is more valuable than waiting. We fail fast and rotate aggressively.

    Args:
        youtube_id: YouTube video ID
        job_id: Job ID for tracking

    Returns:
        dict with success status, file info, and download stats
    """
    # Initialize progress
    job_progress[job_id] = {"status": "pending", "progress": 0, "attempt": 1}

    logger.info(
        f"Starting proxy download for video: {youtube_id}",
        "download",
        {
            "job_id": job_id,
            "youtube_id": youtube_id,
            "strategy": "smart_rotation_failfast",
            "max_attempts": MAX_RETRY_ATTEMPTS,
            "quick_batch": QUICK_RETRY_BATCH,
        }
    )

    total_start = time.time()
    last_error = None
    bot_detection_count = 0
    bad_proxy_count = 0

    for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
        # Check for cancellation
        if is_job_cancelled(job_id):
            return {
                "success": False,
                "cancelled": True,
                "youtube_id": youtube_id,
                "error": "Download cancelled by user",
            }

        # Update progress with attempt number
        job_progress[job_id]["attempt"] = attempt
        job_progress[job_id]["status"] = "downloading"
        job_progress[job_id]["progress"] = 10

        # === SMART ROTATION STRATEGY ===

        # 1. Get fresh proxy session (always new IP on each attempt)
        proxy_session_id = get_fresh_session_id(f"{job_id}-a{attempt}")

        # 2. Rotate player client based on attempt
        player_client_idx = (attempt - 1) % len(PLAYER_CLIENTS)
        player_client = PLAYER_CLIENTS[player_client_idx]

        # 3. Always use US for fastest CDN connection
        country = "US"

        logger.info(
            f"[Attempt {attempt}/{MAX_RETRY_ATTEMPTS}] Smart rotation: "
            f"client={player_client}, "
            f"{'QUICK' if attempt <= QUICK_RETRY_BATCH else 'STANDARD'} retry",
            "download",
            {
                "job_id": job_id,
                "youtube_id": youtube_id,
                "attempt": attempt,
                "player_client": player_client,
                "is_quick_retry": attempt <= QUICK_RETRY_BATCH,
            }
        )

        try:
            # Single yt-dlp call with smart parameters
            download_result = await _download_single_attempt(
                youtube_id=youtube_id,
                job_id=job_id,
                attempt=attempt,
                proxy_session_id=proxy_session_id,
                player_client=player_client,
                country=country,
                resume_enabled=True,  # Enable resume for partial downloads
            )

            # Success!
            total_duration = time.time() - total_start
            job_progress[job_id]["progress"] = 75

            logger.success(
                f"[SUCCESS] Download complete: {download_result['title'][:50]} (attempt {attempt})",
                "download",
                {
                    "job_id": job_id,
                    "youtube_id": youtube_id,
                    "title": download_result["title"][:50],
                    "filesize_mb": round(download_result["filesize_bytes"] / (1024 * 1024), 2),
                    "download_time_seconds": round(download_result["download_time"], 2),
                    "total_time_seconds": round(total_duration, 2),
                    "attempts": attempt,
                    "bot_detections": bot_detection_count,
                    "bad_proxy_rotations": bad_proxy_count,
                    "final_client": player_client,
                }
            )

            return {
                "success": True,
                "file_path": download_result["file_path"],
                "title": download_result["title"],
                "duration_seconds": download_result["duration_seconds"],
                "youtube_id": youtube_id,
                "ext": download_result["ext"],
                "filesize_bytes": download_result["filesize_bytes"],
                "download_time": download_result["download_time"],
                "method": "proxy_smart_rotation",
                "cost": COST_PER_DOWNLOAD,
                "attempts": attempt,
            }

        except PERMANENT_ERRORS as e:
            # Don't retry permanent errors - fail immediately
            logger.error(f"Permanent error (no retry): {e.message}", "download", {
                "job_id": job_id, "error_code": e.error_code, "attempt": attempt
            })
            job_progress[job_id] = {"status": "failed", "progress": 0, "error": e.message}
            raise

        except DownloadCancelled:
            job_progress[job_id] = {"status": "cancelled", "progress": 0, "error": "Cancelled by user"}
            cancelled_jobs.discard(job_id)
            return {
                "success": False,
                "cancelled": True,
                "youtube_id": youtube_id,
                "error": "Download cancelled by user",
            }

        except BotDetectionError as e:
            # Bot detection - this is specifically a bad proxy symptom
            bot_detection_count += 1
            bad_proxy_count += 1
            last_error = e

            # === FAIL FAST STRATEGY ===
            if attempt <= QUICK_RETRY_BATCH:
                # Quick retry: minimal delay, just get a new IP immediately
                delay = QUICK_RETRY_DELAY
                logger.warn(
                    f"Bot detection #{bot_detection_count} on attempt {attempt}, "
                    f"QUICK RETRY in {delay}s with fresh IP...",
                    "download",
                    {"job_id": job_id, "attempt": attempt, "delay": delay, "strategy": "quick_retry"}
                )
            elif attempt < MAX_RETRY_ATTEMPTS:
                # Standard retry with backoff (after quick batch exhausted)
                backoff_idx = min(attempt - QUICK_RETRY_BATCH - 1, len(RETRY_BACKOFF_SECONDS) - 1)
                delay = RETRY_BACKOFF_SECONDS[backoff_idx]
                logger.warn(
                    f"Bot detection #{bot_detection_count} on attempt {attempt}, "
                    f"retrying in {delay}s with fresh proxy (country rotation)...",
                    "download",
                    {"job_id": job_id, "attempt": attempt, "delay": delay, "strategy": "standard_retry"}
                )
            else:
                # Last attempt - go to circuit breaker
                delay = 0
                logger.warn(
                    f"All {MAX_RETRY_ATTEMPTS} attempts exhausted with {bot_detection_count} bot detections",
                    "download",
                    {"job_id": job_id, "bot_detections": bot_detection_count}
                )

            if delay > 0:
                await asyncio.sleep(delay)

        except (DownloadError, DownloadTimeoutError) as e:
            last_error = e
            error_msg = e.message if hasattr(e, 'message') else str(e)

            # Check if this looks like a bad proxy symptom
            if is_bad_proxy_symptom(error_msg):
                bad_proxy_count += 1

            if attempt <= QUICK_RETRY_BATCH:
                # Quick retry for any error in the quick batch
                delay = QUICK_RETRY_DELAY
                logger.warn(
                    f"Download error on attempt {attempt}, QUICK RETRY in {delay}s...",
                    "download",
                    {"job_id": job_id, "attempt": attempt, "delay": delay, "error": error_msg[:80]}
                )
            elif attempt < MAX_RETRY_ATTEMPTS:
                backoff_idx = min(attempt - QUICK_RETRY_BATCH - 1, len(RETRY_BACKOFF_SECONDS) - 1)
                delay = RETRY_BACKOFF_SECONDS[backoff_idx]
                logger.warn(
                    f"Download failed on attempt {attempt}, retrying in {delay}s...",
                    "download",
                    {"job_id": job_id, "attempt": attempt, "delay": delay, "error": error_msg[:80]}
                )
            else:
                delay = 0
                logger.error(
                    f"All {MAX_RETRY_ATTEMPTS} attempts failed for {youtube_id}",
                    "download",
                    {"job_id": job_id, "youtube_id": youtube_id, "final_error": error_msg}
                )

            if delay > 0:
                await asyncio.sleep(delay)

        except Exception as e:
            last_error = e
            error_msg = str(e) if not hasattr(e, 'message') else e.message

            if attempt <= QUICK_RETRY_BATCH:
                delay = QUICK_RETRY_DELAY
            elif attempt < MAX_RETRY_ATTEMPTS:
                backoff_idx = min(attempt - QUICK_RETRY_BATCH - 1, len(RETRY_BACKOFF_SECONDS) - 1)
                delay = RETRY_BACKOFF_SECONDS[backoff_idx]
            else:
                delay = 0

            logger.warn(
                f"Attempt {attempt} failed, {'QUICK RETRY' if attempt <= QUICK_RETRY_BATCH else 'retrying'} "
                f"in {delay}s with fresh proxy...",
                "download",
                {"job_id": job_id, "attempt": attempt, "delay": delay, "error": error_msg[:80]}
            )

            if delay > 0:
                await asyncio.sleep(delay)

    # === CIRCUIT BREAKER ===
    # ALWAYS run a final attempt after all regular attempts fail
    # This attempt disables the speed limit to ensure the download completes
    # even if the proxy is slow - better to finish slowly than not at all
    logger.info(
        f"Circuit breaker activated: waiting {CIRCUIT_BREAKER_DELAY}s before final attempt "
        f"(bot_detections={bot_detection_count}, bad_proxies={bad_proxy_count})",
        "download",
        {"job_id": job_id, "youtube_id": youtube_id, "bot_detections": bot_detection_count}
    )

    await asyncio.sleep(CIRCUIT_BREAKER_DELAY)

    # Final attempt with completely fresh session and different client
    job_progress[job_id]["attempt"] = MAX_RETRY_ATTEMPTS + 1
    job_progress[job_id]["status"] = "downloading"

    # Circuit breaker uses ios client which provides progressive download URLs
    # and avoids both SABR streaming and bot detection issues
    circuit_breaker_client = ["ios"]
    # Keep US for speed
    circuit_breaker_country = "US"

    try:
        logger.info(
            f"[Circuit Breaker] Final attempt: client={circuit_breaker_client}",
            "download",
            {"job_id": job_id, "youtube_id": youtube_id}
        )

        download_result = await _download_single_attempt(
            youtube_id=youtube_id,
            job_id=job_id,
            attempt=MAX_RETRY_ATTEMPTS + 1,
            proxy_session_id=get_fresh_session_id("circuit"),
            player_client=circuit_breaker_client,
            country=circuit_breaker_country,
            resume_enabled=True,
            is_final_attempt=True,  # Disable speed limit - complete even if slow
        )

        total_duration = time.time() - total_start

        logger.success(
            f"[Circuit Breaker SUCCESS] {download_result['title'][:50]}",
            "download",
            {
                "job_id": job_id,
                "youtube_id": youtube_id,
                "title": download_result["title"][:50],
                "total_time_seconds": round(total_duration, 2),
                "attempts": MAX_RETRY_ATTEMPTS + 1,
                "bot_detections": bot_detection_count,
                "circuit_breaker": True,
            }
        )

        return {
            "success": True,
            "file_path": download_result["file_path"],
            "title": download_result["title"],
            "duration_seconds": download_result["duration_seconds"],
            "youtube_id": youtube_id,
            "ext": download_result["ext"],
            "filesize_bytes": download_result["filesize_bytes"],
            "download_time": download_result["download_time"],
            "method": "proxy_circuit_breaker",
            "cost": COST_PER_DOWNLOAD,
            "attempts": MAX_RETRY_ATTEMPTS + 1,
        }

    except Exception as e:
        last_error = e
        logger.error(
            f"Circuit breaker failed for {youtube_id}",
            "download",
            {"job_id": job_id, "error": str(e)[:100]}
        )

    # All retries exhausted
    total_duration = time.time() - total_start
    final_attempts = MAX_RETRY_ATTEMPTS + 1  # Always includes circuit breaker now

    job_progress[job_id] = {
        "status": "failed",
        "progress": 0,
        "error": str(last_error),
        "attempts": final_attempts,
        "bot_detections": bot_detection_count,
    }

    logger.error(
        f"Download failed for {youtube_id}",
        "download",
        {
            "job_id": job_id,
            "youtube_id": youtube_id,
            "total_attempts": final_attempts,
            "bot_detections": bot_detection_count,
            "bad_proxy_rotations": bad_proxy_count,
            "total_time_seconds": round(total_duration, 2),
        }
    )

    # Return failure with details
    return {
        "success": False,
        "youtube_id": youtube_id,
        "error": str(last_error),
        "attempts": final_attempts,
        "bot_detections": bot_detection_count,
        "bad_proxy_rotations": bad_proxy_count,
        "total_time": total_duration,
        "method": "all_failed",
        "cost": COST_PER_DOWNLOAD,
    }


def get_job_status(job_id: str) -> dict:
    """Get current job status."""
    return job_progress.get(job_id, {"status": "unknown", "progress": 0})


def update_job_progress(job_id: str, status: str, progress: int, error: Optional[str] = None) -> None:
    """Update job progress."""
    job_progress[job_id] = {
        "status": status,
        "progress": progress,
    }
    if error:
        job_progress[job_id]["error"] = error

    logger.debug(f"Job progress updated: {status} ({progress}%)", "download", {
        "job_id": job_id,
        "status": status,
        "progress": progress,
        "error": error,
    })


def cleanup_temp_files(job_id: str) -> None:
    """Clean up temporary files after upload."""
    temp_dir = Path(settings.TEMP_DIR) / job_id
    if temp_dir.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)
        logger.debug(f"Cleaned up temp files for job", "download", {"job_id": job_id})

    if job_id in job_progress:
        del job_progress[job_id]


def cleanup_old_temp_files(max_age_hours: int = 24) -> int:
    """Clean up old temporary files."""
    temp_base = Path(settings.TEMP_DIR)
    if not temp_base.exists():
        return 0

    cleaned = 0
    current_time = time.time()
    max_age_seconds = max_age_hours * 3600

    for item in temp_base.iterdir():
        if item.is_dir():
            age = current_time - item.stat().st_mtime
            if age > max_age_seconds:
                shutil.rmtree(item, ignore_errors=True)
                cleaned += 1

    if cleaned > 0:
        logger.info(f"Cleaned up {cleaned} old temp directories", "download")

    return cleaned


async def get_video_info(youtube_id: str) -> Optional[dict]:
    """Get video information without downloading."""
    url = f"https://www.youtube.com/watch?v={youtube_id}"

    logger.debug(f"Fetching video info: {youtube_id}", "ytdlp")

    ydl_opts = {
        **get_proxy_config(),
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            result = {
                "youtube_id": youtube_id,
                "title": info.get("title"),
                "duration_seconds": info.get("duration"),
                "description": info.get("description"),
                "uploader": info.get("uploader"),
                "view_count": info.get("view_count"),
            }
            logger.debug(f"Got video info: {info.get('title')}", "ytdlp", {"youtube_id": youtube_id})
            return result
    except Exception as e:
        logger.error(f"Failed to get video info: {e}", "ytdlp", {"youtube_id": youtube_id})
        return None
