"""YouTube video download service with hybrid Tier 1/Tier 2 strategy.

HYBRID DOWNLOAD STRATEGY
========================
This service implements a cost-optimized hybrid download strategy:

Tier 1 (Primary - PO Token): ~$0.008/video, ~60% success rate
    - Uses Proof-of-Origin (PO) tokens via bgutil-ytdlp-pot-provider
    - PO tokens prove we're a legitimate browser, bypassing IP-binding
    - Extracts URLs that work from ANY IP (no proxy needed for download)
    - Downloads directly via aria2c (16 connections, no proxy)
    - Best for: Most regular videos

Tier 2 (Fallback - Full Proxy): ~$0.12/video, ~100% success rate
    - Uses Oxylabs residential proxy for extraction AND download
    - Required when Tier 1 fails (age-restricted, region-locked, etc.)
    - Uses aria2c through proxy (still 16 connections, but via proxy)
    - Best for: Age-restricted, region-locked, premium content

SMART CACHING:
- Tracks which videos need Tier 2 (proxy)
- Skips Tier 1 for videos known to require proxy
- Reduces wasted time and API calls

EXPECTED COSTS:
- ~60% of videos use Tier 1: $0.008/video
- ~40% of videos need Tier 2: $0.12/video
- Blended average: ~$0.05/video (56% cheaper than always using proxy)
"""

import asyncio
import random
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple
from concurrent.futures import ThreadPoolExecutor

import yt_dlp

from app.config import settings
from app.services.proxy import get_proxy_config
from app.services import logger
from app.services.po_token import get_token_manager
from app.services.method_cache import (
    get_method_cache,
    DownloadMethod,
    FailureReason,
)
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
    PERMANENT_ERRORS,
)

# === VIDEO RESTRICTION DETECTION ===
# Age limit threshold - YouTube uses 18 for age-restricted content
AGE_RESTRICTION_THRESHOLD = 18

# === RELIABILITY CONFIGURATION ===
DOWNLOAD_TIMEOUT_SECONDS = 180  # Max time for a single download attempt (3 min for large files + ffmpeg)
MAX_RETRY_ATTEMPTS = 5  # Number of retry attempts (increased for bot detection)
RETRY_BACKOFF_SECONDS = [1, 2, 5, 10]  # Escalating backoff - longer delays help get fresh IPs
CIRCUIT_BREAKER_DELAY = 30  # Final attempt after this delay if all retries fail with bot detection

# === ARIA2C CONNECTION SETTINGS ===
# YouTube sweet spot is 6-8 connections through residential proxies.
# 16+ triggers bot walls and 403s. Start moderate and back off on failures.
ARIA2C_START_CONNECTIONS = 8  # Initial connection count
ARIA2C_MIN_CONNECTIONS = 2  # Minimum after backoff

# === DOWNLOAD FORMAT STRATEGY ===
# Key insight: Single mp4 streams (video+audio in one container) are:
# 1. Faster - single stream, no merging needed
# 2. More reliable - video formats are cached better on YouTube CDN
# 3. Less likely to trigger bot detection than audio-only streams
#
# Format priority:
# 1. worst[ext=mp4][acodec!=none] - smallest combined mp4 (e.g., 360p)
# 2. worst - fallback to any worst format
DOWNLOAD_FORMAT = "worst[ext=mp4][acodec!=none]/worst[ext=mp4]/worst"

# Thread pool for running blocking downloads (8 workers for parallel capacity)
_download_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ytdlp")


def _get_backoff_with_jitter(attempt: int) -> float:
    """
    Get backoff delay with jitter for the given attempt number.

    Jitter helps prevent thundering herd when multiple downloads retry simultaneously.
    Formula: base_backoff + random(0, 3) seconds

    Args:
        attempt: Current attempt number (1-based)

    Returns:
        float: Backoff delay in seconds with jitter added
    """
    base_backoff = RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
    jitter = random.uniform(0, 3)  # Add 0-3 seconds of jitter
    return base_backoff + jitter


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
# Estimated costs per download method
COST_TIER1_PO_TOKEN = 0.008  # ~$0.008 per video (minimal overhead)
COST_TIER2_PROXY = 0.12  # ~$0.12 per video (full proxy bandwidth)

# Tier 1 configuration
TIER1_TIMEOUT_SECONDS = 30  # Shorter timeout for Tier 1 (fail fast)
TIER1_MAX_RETRIES = 2  # Fewer retries for Tier 1 (fallback to Tier 2 quickly)


@dataclass
class DownloadResult:
    """Result of a video download operation."""
    success: bool
    file_path: Optional[str] = None
    title: str = "Unknown"
    duration_seconds: int = 0
    youtube_id: str = ""
    ext: str = "m4a"
    filesize_bytes: int = 0
    download_time: float = 0.0
    method: DownloadMethod = DownloadMethod.UNKNOWN
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
# TIER 1: PO TOKEN DOWNLOAD (NO PROXY)
# =============================================================================

async def download_tier1_po_token(
    youtube_id: str,
    job_id: str,
) -> DownloadResult:
    """
    TIER 1: Download video using PO token (no proxy needed).

    Uses the bgutil-ytdlp-pot-provider plugin which automatically generates
    PO tokens via the bgutil HTTP server. This bypasses YouTube's IP-binding,
    allowing direct downloads without residential proxies.

    Requires: bgutil server running at http://127.0.0.1:4416
    Setup: sudo bash deployment/setup-bgutil.sh

    Cost: ~$0.008 per video
    Expected success rate: ~60%

    Args:
        youtube_id: YouTube video ID
        job_id: Job tracking ID

    Returns:
        DownloadResult with success status and file info
    """
    url = f"https://www.youtube.com/watch?v={youtube_id}"
    method_cache = get_method_cache()

    logger.info(
        f"[TIER 1] Starting PO token download for {youtube_id}",
        "download",
        {"job_id": job_id, "youtube_id": youtube_id, "method": "tier1_po_token"}
    )

    # Check if bgutil server is available (PO token generation)
    token_manager = get_token_manager()
    if not token_manager.is_available():
        logger.warn(
            f"[TIER 1] bgutil server not available, skipping Tier 1. "
            f"Run: sudo bash deployment/setup-bgutil.sh",
            "download",
            {"job_id": job_id, "youtube_id": youtube_id}
        )
        return DownloadResult(
            success=False,
            youtube_id=youtube_id,
            method=DownloadMethod.TIER1_PO_TOKEN,
            error="bgutil PO token server not available"
        )

    method_cache.record_attempt(DownloadMethod.TIER1_PO_TOKEN)
    start_time = time.time()

    # Create temp directory for download
    temp_dir = Path(settings.TEMP_DIR) / job_id
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Create custom logger for yt-dlp
    ytdlp_logger = logger.YtdlpLogger(job_id)

    # yt-dlp options WITHOUT proxy - the bgutil plugin handles PO tokens automatically
    ydl_opts = {
        "quiet": False,
        "verbose": True,
        "logger": ytdlp_logger,
        "outtmpl": str(temp_dir / "%(id)s.%(ext)s"),
        # Single mp4 stream - faster and more reliable than separate video+audio
        "format": DOWNLOAD_FORMAT,
        "geo_bypass": True,
        "socket_timeout": 30,
        # NO PROXY - direct download with PO token from bgutil plugin
        # Prefer English audio track
        "extractor_args": {
            "youtube": {
                "lang": ["en", "en-US", "en-GB"],
            }
        },
    }

    # Enable aria2c for multi-connection download if available
    # Use moderate concurrency (8) - YouTube sweet spot for residential proxies
    if ARIA2C_AVAILABLE:
        conns = str(ARIA2C_START_CONNECTIONS)
        ydl_opts["external_downloader"] = "aria2c"
        ydl_opts["external_downloader_args"] = {
            "aria2c": [
                "-x", conns, "-s", conns, "-k", "1M",
                "--file-allocation=none",
                f"--max-connection-per-server={ARIA2C_START_CONNECTIONS}",
                "--min-split-size=1M", f"--split={ARIA2C_START_CONNECTIONS}",
                "--timeout=20", "--connect-timeout=10",
            ]
        }

    # Enable Node.js for YouTube bot challenge solving
    if NODEJS_AVAILABLE:
        ydl_opts["js_runtimes"] = {"node": {}}

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
        download_result = await asyncio.wait_for(download_task, timeout=TIER1_TIMEOUT_SECONDS + DOWNLOAD_TIMEOUT_SECONDS)

        info = download_result["info"]
        download_time = download_result["duration"]

        # Success!
        method_cache.record_success(youtube_id, DownloadMethod.TIER1_PO_TOKEN)

        logger.success(
            f"[TIER 1] Download complete: {info.get('title', 'Unknown')[:50]}",
            "download",
            {
                "job_id": job_id,
                "youtube_id": youtube_id,
                "method": "tier1_po_token",
                "title": info.get("title", "Unknown")[:50],
                "filesize_mb": round(download_result["filesize"] / (1024 * 1024), 2),
                "download_time_seconds": round(download_time, 2),
                "cost": COST_TIER1_PO_TOKEN,
            }
        )

        return DownloadResult(
            success=True,
            file_path=str(download_result["output_file"]),
            title=info.get("title", "Unknown"),
            duration_seconds=info.get("duration", 0),
            youtube_id=youtube_id,
            ext=download_result["ext"],
            filesize_bytes=download_result["filesize"],
            download_time=download_time,
            method=DownloadMethod.TIER1_PO_TOKEN,
            cost=COST_TIER1_PO_TOKEN,
        )

    except asyncio.TimeoutError:
        duration = time.time() - start_time
        error_msg = f"Tier 1 download timed out after {duration:.1f}s"
        logger.warn(f"[TIER 1] {error_msg}", "download", {
            "job_id": job_id, "youtube_id": youtube_id
        })
        method_cache.record_failure(youtube_id, DownloadMethod.TIER1_PO_TOKEN, FailureReason.UNKNOWN)
        return DownloadResult(
            success=False,
            youtube_id=youtube_id,
            method=DownloadMethod.TIER1_PO_TOKEN,
            error=error_msg
        )

    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        duration = time.time() - start_time

        # Classify the failure reason
        failure_reason = _classify_tier1_failure(error_msg)

        logger.warn(
            f"[TIER 1] Download failed ({failure_reason.value}): {error_msg[:100]}",
            "download",
            {"job_id": job_id, "youtube_id": youtube_id, "failure_reason": failure_reason.value}
        )

        method_cache.record_failure(youtube_id, DownloadMethod.TIER1_PO_TOKEN, failure_reason)

        # Check if this is a permanent error that won't be fixed by Tier 2
        classified = _classify_error(error_msg)
        if isinstance(classified, PERMANENT_ERRORS):
            raise classified

        return DownloadResult(
            success=False,
            youtube_id=youtube_id,
            method=DownloadMethod.TIER1_PO_TOKEN,
            error=error_msg
        )

    except Exception as e:
        error_msg = str(e)
        duration = time.time() - start_time
        logger.warn(
            f"[TIER 1] Unexpected error: {error_msg[:100]}",
            "download",
            {"job_id": job_id, "youtube_id": youtube_id}
        )
        method_cache.record_failure(youtube_id, DownloadMethod.TIER1_PO_TOKEN, FailureReason.UNKNOWN)
        return DownloadResult(
            success=False,
            youtube_id=youtube_id,
            method=DownloadMethod.TIER1_PO_TOKEN,
            error=error_msg
        )


def _classify_tier1_failure(error_msg: str) -> FailureReason:
    """Classify why Tier 1 failed for caching purposes."""
    error_lower = error_msg.lower()

    if "403" in error_msg or "forbidden" in error_lower:
        return FailureReason.IP_BINDING
    elif "age" in error_lower and "restrict" in error_lower:
        return FailureReason.AGE_RESTRICTED
    elif "not available" in error_lower and ("country" in error_lower or "region" in error_lower):
        return FailureReason.REGION_LOCKED
    elif "sign in" in error_lower or "bot" in error_lower or "confirm" in error_lower:
        return FailureReason.BOT_DETECTION
    elif "token" in error_lower and ("expired" in error_lower or "invalid" in error_lower):
        return FailureReason.TOKEN_EXPIRED
    elif "format" in error_lower and "not available" in error_lower:
        return FailureReason.FORMAT_UNAVAILABLE
    else:
        return FailureReason.UNKNOWN


# =============================================================================
# TIER 2: PROXY DOWNLOAD (PHASE 1 + PHASE 2)
# =============================================================================
# The existing extract_audio_url and download_from_cdn functions form Tier 2.
# They use the Oxylabs residential proxy for both extraction and download.

# =============================================================================
# TIER 2 - PHASE 1: METADATA EXTRACTION (WITH PROXY)
# =============================================================================

async def extract_audio_url(
    youtube_id: str,
    job_id: str,
    proxy_session_id: str,
) -> dict:
    """
    PHASE 1: Extract video metadata and signed CDN URL using proxy.

    This function uses yt-dlp with the Oxylabs proxy to:
    1. Fetch video metadata (title, duration, etc.)
    2. Extract the direct download URL for the best audio format
    3. Return all info WITHOUT downloading the actual file

    Bandwidth usage: ~700KB-1MB per video (metadata only)

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
        # Single mp4 stream - faster and more reliable than separate video+audio
        "format": DOWNLOAD_FORMAT,
        "geo_bypass": True,
        "socket_timeout": 30,
        # Prefer English audio track
        "extractor_args": {
            "youtube": {
                "lang": ["en", "en-US", "en-GB"],
            }
        },
    }

    # Enable Node.js for YouTube bot challenge solving
    if NODEJS_AVAILABLE:
        ydl_opts["js_runtimes"] = {"node": {}}

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
            # Get the first format with a URL (combined mp4 should be single stream)
            for fmt in info["requested_formats"]:
                if fmt.get("url"):
                    cdn_url = fmt.get("url")
                    break

        # Fallback: look in formats list for combined mp4 (video+audio in one stream)
        if not cdn_url and info.get("formats"):
            # Find combined formats (have both video and audio codecs)
            # These are single-stream mp4s like itag 18 (360p)
            combined_formats = [
                f for f in info["formats"]
                if f.get("acodec") != "none"
                and f.get("vcodec") != "none"
                and f.get("ext") == "mp4"
                and f.get("url")
            ]
            if combined_formats:
                # Sort by quality (filesize or height) - prefer smallest
                combined_formats.sort(key=lambda f: f.get("filesize") or f.get("height") or 0)
                cdn_url = combined_formats[0].get("url")

            # Final fallback: any format with audio
            if not cdn_url:
                audio_formats = [f for f in info["formats"] if f.get("acodec") != "none" and f.get("url")]
                if audio_formats:
                    audio_formats.sort(key=lambda f: f.get("filesize") or f.get("tbr") or 0)
                    cdn_url = audio_formats[0].get("url")

        if not cdn_url:
            raise DownloadError(
                f"Could not extract CDN URL for {youtube_id}. "
                "No suitable format URLs found in metadata."
            )

        # Get file extension and size (default to mp4 for combined formats)
        ext = info.get("ext", "mp4")
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
    PHASE 2: Download audio file from CDN using aria2c multi-connection.

    This function downloads the file from Google's CDN using aria2c for
    fast multi-connection downloads. Due to YouTube's IP-binding, the
    proxy_url should be the SAME proxy session used in Phase 1.

    Benefits of this approach:
    - 16 parallel connections for faster downloads
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
    connections: int = None,
) -> dict:
    """
    Download file using aria2c with multi-connection support.

    YouTube through residential proxies has a sweet spot of 6-8 connections.
    Higher values (16+) trigger bot walls and 403s.

    Args:
        cdn_url: The CDN URL to download from
        output_file: Path to save the downloaded file
        job_id: Job ID for logging
        proxy_url: Optional proxy URL (required for IP-bound YouTube URLs)
        connections: Number of connections (defaults to ARIA2C_START_CONNECTIONS)
    """
    # Use provided connections or default
    conns = connections if connections is not None else ARIA2C_START_CONNECTIONS
    conns_str = str(conns)

    # aria2c command optimized for YouTube through residential proxies
    cmd = [
        "aria2c",
        "-x", conns_str,  # Max connections per server
        "-s", conns_str,  # Split file into segments
        "-k", "1M",  # Min split size
        f"--max-connection-per-server={conns}",
        "--min-split-size=1M",
        f"--split={conns}",
        "--timeout=20",  # Reduced from 30 for faster failure detection
        "--connect-timeout=10",
        "--file-allocation=none",
        "--auto-file-renaming=false",
        "--allow-overwrite=true",
        "--console-log-level=warn",
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
        f"[PHASE 2] Executing aria2c ({conns} connections, {using_proxy})",
        "download",
        {"job_id": job_id, "output": str(output_file), "using_proxy": bool(proxy_url), "connections": conns}
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
) -> dict:
    """
    Execute a single download attempt with timeout.

    Args:
        youtube_id: YouTube video ID
        job_id: Job tracking ID
        attempt: Current attempt number (1-based)
        proxy_session_id: Unique session ID for proxy sticky session

    Returns:
        dict with download result or raises exception
    """
    url = f"https://www.youtube.com/watch?v={youtube_id}"
    temp_dir = Path(settings.TEMP_DIR) / job_id
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Get proxy config with unique session for this attempt
    proxy_config = get_proxy_config(proxy_session_id)
    if proxy_config.get("proxy"):
        proxy_url = proxy_config["proxy"]
        masked_proxy = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
        logger.info(f"Attempt {attempt}: Using proxy {masked_proxy}", "proxy", {
            "job_id": job_id, "attempt": attempt, "session_id": proxy_session_id
        })

    # Create custom logger for yt-dlp
    ytdlp_logger = logger.YtdlpLogger(job_id)

    # yt-dlp options optimized for SPEED
    ydl_opts = {
        **proxy_config,
        "outtmpl": str(temp_dir / f"{youtube_id}.%(ext)s"),
        # Single mp4 stream - faster and more reliable than separate video+audio
        "format": DOWNLOAD_FORMAT,
        "progress_hooks": [lambda d: _progress_hook(d, job_id)],
        "verbose": True,
        "logger": ytdlp_logger,
        # Reduced retries for speed - we handle retries at higher level
        "retries": 2,
        "fragment_retries": 3,
        "noplaylist": True,
        "geo_bypass": True,
        "socket_timeout": 30,  # Socket timeout for speed
        # Speed optimizations
        "concurrent_fragment_downloads": 8,
        "buffersize": 1024 * 64,
        "http_chunk_size": 10485760,
        # Prefer original/English audio track
        "extractor_args": {
            "youtube": {
                "lang": ["en", "en-US", "en-GB"],  # Prefer English
            }
        },
    }

    # Enable Node.js for YouTube bot challenge solving
    if NODEJS_AVAILABLE:
        ydl_opts["js_runtimes"] = {"node": {}}

    # Use aria2c if available with moderate concurrency
    if ARIA2C_AVAILABLE:
        conns = str(ARIA2C_START_CONNECTIONS)
        ydl_opts["external_downloader"] = "aria2c"
        ydl_opts["external_downloader_args"] = {
            "aria2c": [
                "-x", conns, "-s", conns, "-k", "1M",
                "--file-allocation=none",
                f"--max-connection-per-server={ARIA2C_START_CONNECTIONS}",
                "--min-split-size=1M", f"--split={ARIA2C_START_CONNECTIONS}",
                "--timeout=20", "--connect-timeout=10",
            ]
        }

    start_time = time.time()

    def _blocking_download():
        """Run blocking yt-dlp download."""
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            ext = info.get("ext", "mp4")
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
        }

    except asyncio.TimeoutError:
        duration = time.time() - start_time
        logger.warn(f"Attempt {attempt} timed out after {duration:.1f}s", "download", {
            "job_id": job_id, "attempt": attempt, "timeout": DOWNLOAD_TIMEOUT_SECONDS
        })
        raise DownloadTimeoutError(f"Download timed out after {DOWNLOAD_TIMEOUT_SECONDS}s")

    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        logger.warn(f"Attempt {attempt} failed: {error_msg[:100]}", "download", {
            "job_id": job_id, "attempt": attempt
        })
        raise _classify_error(error_msg)


async def download_video(youtube_id: str, job_id: str) -> dict:
    """
    Download YouTube video using HYBRID Tier 1/Tier 2 strategy.

    COST-OPTIMIZED HYBRID APPROACH
    ==============================
    Tier 1 (Primary - PO Token): ~$0.008/video, ~60% success rate
        - Uses Proof-of-Origin tokens to bypass IP-binding
        - Direct download via aria2c (no proxy needed)
        - Fastest and cheapest option

    Tier 2 (Fallback - Full Proxy): ~$0.12/video, ~100% success rate
        - Uses Oxylabs residential proxy for extraction AND download
        - Required for age-restricted, region-locked, etc.
        - Same proxy session for both phases (IP-bound URLs)

    Features:
    - Smart caching: Skips Tier 1 for videos known to require Tier 2
    - Early restriction detection (age-restricted, private, premium, live)
    - Automatic fallback from Tier 1 to Tier 2 on failure
    - Cost tracking per download

    Expected blended cost: ~$0.05/video (56% cheaper than always using proxy)

    Args:
        youtube_id: YouTube video ID
        job_id: Job ID for tracking

    Returns:
        dict with success status, file info, method used, and cost
    """
    # Initialize progress
    job_progress[job_id] = {"status": "pending", "progress": 0, "attempt": 1}
    method_cache = get_method_cache()

    logger.info(
        f"Starting hybrid Tier 1/Tier 2 download for video: {youtube_id}",
        "download",
        {
            "job_id": job_id,
            "youtube_id": youtube_id,
            "strategy": "hybrid_tier1_tier2",
        }
    )

    # === EARLY RESTRICTION CHECK ===
    # Check for age-restricted, private, premium, or live content BEFORE downloading.
    # This fails fast with a clear error instead of wasting time on doomed downloads.
    try:
        job_progress[job_id]["status"] = "checking"
        await check_video_restrictions(youtube_id, job_id)
    except PERMANENT_ERRORS:
        # Re-raise permanent errors - these won't be fixed by retrying
        raise

    total_start = time.time()

    # =================================================================
    # TIER 1: TRY PO TOKEN DOWNLOAD (NO PROXY - CHEAPEST)
    # =================================================================
    # Check if we should skip Tier 1 based on previous failures
    skip_tier1 = method_cache.should_skip_tier1(youtube_id)

    if not skip_tier1:
        # Check for cancellation
        if is_job_cancelled(job_id):
            return {
                "success": False,
                "cancelled": True,
                "youtube_id": youtube_id,
                "error": "Download cancelled by user",
            }

        job_progress[job_id]["status"] = "downloading"
        job_progress[job_id]["progress"] = 10

        logger.info(
            f"[TIER 1] Attempting PO token download (no proxy)",
            "download",
            {"job_id": job_id, "youtube_id": youtube_id}
        )

        try:
            tier1_result = await download_tier1_po_token(youtube_id, job_id)

            if tier1_result.success:
                # Tier 1 succeeded!
                total_duration = time.time() - total_start
                job_progress[job_id]["progress"] = 75

                logger.success(
                    f"[TIER 1 SUCCESS] Download complete: {tier1_result.title[:50]}",
                    "download",
                    {
                        "job_id": job_id,
                        "youtube_id": youtube_id,
                        "title": tier1_result.title[:50],
                        "filesize_mb": round(tier1_result.filesize_bytes / (1024 * 1024), 2),
                        "download_time_seconds": round(tier1_result.download_time, 2),
                        "total_time_seconds": round(total_duration, 2),
                        "method": "tier1_po_token",
                        "cost": COST_TIER1_PO_TOKEN,
                    }
                )

                return {
                    "success": True,
                    "file_path": tier1_result.file_path,
                    "title": tier1_result.title,
                    "duration_seconds": tier1_result.duration_seconds,
                    "youtube_id": youtube_id,
                    "ext": tier1_result.ext,
                    "filesize_bytes": tier1_result.filesize_bytes,
                    "download_time": tier1_result.download_time,
                    "method": "tier1_po_token",
                    "cost": COST_TIER1_PO_TOKEN,
                }

            # Tier 1 failed (non-permanent error) - fall through to Tier 2
            logger.info(
                f"[TIER 1 FAILED] {tier1_result.error[:100] if tier1_result.error else 'Unknown error'}, "
                f"falling back to Tier 2 (proxy)",
                "download",
                {"job_id": job_id, "youtube_id": youtube_id}
            )

        except PERMANENT_ERRORS:
            # Re-raise permanent errors - Tier 2 won't help
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

        except Exception as e:
            # Log but continue to Tier 2
            logger.warn(
                f"[TIER 1] Unexpected error: {str(e)[:100]}, falling back to Tier 2",
                "download",
                {"job_id": job_id, "youtube_id": youtube_id}
            )

    else:
        logger.info(
            f"[TIER 1 SKIPPED] Video {youtube_id} known to require proxy (from cache)",
            "download",
            {"job_id": job_id, "youtube_id": youtube_id}
        )

    # =================================================================
    # TIER 2: PROXY DOWNLOAD (FALLBACK)
    # =================================================================
    # Either Tier 1 failed or was skipped due to cache
    logger.info(
        f"[TIER 2] Starting proxy download for {youtube_id}",
        "download",
        {"job_id": job_id, "youtube_id": youtube_id}
    )

    method_cache.record_attempt(DownloadMethod.TIER2_PROXY)

    last_error = None
    bot_detection_count = 0
    metadata_result = None

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
        job_progress[job_id]["status"] = "extracting"

        # Use unique proxy session for each attempt (new IP on retry)
        # IMPORTANT: Same session ID used for BOTH phases (IP-bound URLs require this)
        proxy_session_id = f"{job_id}-t2-a{attempt}-{uuid.uuid4().hex[:8]}"

        # Get proxy config once - use same proxy URL for both phases
        proxy_config = get_proxy_config(proxy_session_id)
        proxy_url = proxy_config.get("proxy")

        try:
            # =================================================================
            # TIER 2 - PHASE 1: Extract metadata + CDN URL (WITH PROXY)
            # =================================================================
            logger.info(
                f"[TIER 2 - Attempt {attempt}] PHASE 1: Extracting metadata via proxy",
                "download",
                {"job_id": job_id, "youtube_id": youtube_id, "attempt": attempt}
            )

            metadata_result = await extract_audio_url(youtube_id, job_id, proxy_session_id)

            # Update progress - metadata extracted
            job_progress[job_id]["status"] = "downloading"
            job_progress[job_id]["progress"] = 40

            # =================================================================
            # TIER 2 - PHASE 2: Download from CDN (SAME PROXY SESSION)
            # =================================================================
            logger.info(
                f"[TIER 2 - Attempt {attempt}] PHASE 2: Downloading via aria2c (same proxy session)",
                "download",
                {"job_id": job_id, "youtube_id": youtube_id, "attempt": attempt}
            )

            download_result = await download_from_cdn(
                cdn_url=metadata_result["cdn_url"],
                youtube_id=youtube_id,
                job_id=job_id,
                ext=metadata_result["ext"],
                title=metadata_result["title"],
                proxy_url=proxy_url,  # Use same proxy session for IP-bound URLs
            )

            # Success! Record in cache and return
            total_duration = time.time() - total_start
            job_progress[job_id]["progress"] = 75

            method_cache.record_success(youtube_id, DownloadMethod.TIER2_PROXY)

            logger.success(
                f"[TIER 2 SUCCESS] Download complete: {metadata_result['title'][:50]} (attempt {attempt})",
                "download",
                {
                    "job_id": job_id,
                    "youtube_id": youtube_id,
                    "title": metadata_result["title"][:50],
                    "filesize_mb": round(download_result["filesize_bytes"] / (1024 * 1024), 2),
                    "download_time_seconds": round(download_result["download_time"], 2),
                    "total_time_seconds": round(total_duration, 2),
                    "attempts": attempt,
                    "bot_detections": bot_detection_count,
                    "method": "tier2_proxy",
                    "cost": COST_TIER2_PROXY,
                }
            )

            return {
                "success": True,
                "file_path": download_result["file_path"],
                "title": metadata_result["title"],
                "duration_seconds": metadata_result["duration_seconds"],
                "youtube_id": youtube_id,
                "ext": metadata_result["ext"],
                "filesize_bytes": download_result["filesize_bytes"],
                "download_time": download_result["download_time"],
                "method": "tier2_proxy",
                "cost": COST_TIER2_PROXY,
            }

        except PERMANENT_ERRORS as e:
            # Don't retry permanent errors - fail immediately
            logger.error(f"[TIER 2] Permanent error (no retry): {e.message}", "download", {
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
            # Special handling for bot detection - track and use longer delays
            bot_detection_count += 1
            last_error = e

            if attempt < MAX_RETRY_ATTEMPTS:
                # Use longer delays for bot detection to let proxy pool rotate
                # Jitter prevents thundering herd when multiple downloads retry
                backoff = _get_backoff_with_jitter(attempt)
                logger.warn(
                    f"[TIER 2] Bot detection #{bot_detection_count} on attempt {attempt}, "
                    f"retrying in {backoff:.1f}s with fresh proxy IP...",
                    "download",
                    {"job_id": job_id, "attempt": attempt, "backoff": round(backoff, 1), "bot_detections": bot_detection_count}
                )
                await asyncio.sleep(backoff)
            else:
                logger.warn(
                    f"[TIER 2] All {MAX_RETRY_ATTEMPTS} attempts hit bot detection, "
                    f"trying circuit breaker ({CIRCUIT_BREAKER_DELAY}s delay)...",
                    "download",
                    {"job_id": job_id, "bot_detections": bot_detection_count}
                )

        except DownloadError as e:
            # Phase 2 CDN download failed - need fresh URL from Phase 1
            last_error = e
            error_msg = e.message if hasattr(e, 'message') else str(e)

            if attempt < MAX_RETRY_ATTEMPTS:
                backoff = _get_backoff_with_jitter(attempt)
                logger.warn(
                    f"[TIER 2] CDN download failed on attempt {attempt}, "
                    f"will refresh URL in {backoff:.1f}s...",
                    "download",
                    {"job_id": job_id, "attempt": attempt, "backoff": round(backoff, 1), "error": error_msg[:100]}
                )
                await asyncio.sleep(backoff)
            else:
                logger.error(
                    f"[TIER 2] All {MAX_RETRY_ATTEMPTS} attempts failed for {youtube_id}",
                    "download",
                    {"job_id": job_id, "youtube_id": youtube_id, "final_error": error_msg}
                )

        except Exception as e:
            last_error = e
            error_msg = str(e) if not hasattr(e, 'message') else e.message

            # Check if we should retry
            if attempt < MAX_RETRY_ATTEMPTS:
                backoff = _get_backoff_with_jitter(attempt)
                logger.warn(
                    f"[TIER 2] Attempt {attempt} failed, retrying in {backoff:.1f}s with fresh proxy...",
                    "download",
                    {"job_id": job_id, "attempt": attempt, "backoff": round(backoff, 1), "error": error_msg[:100]}
                )
                await asyncio.sleep(backoff)
            else:
                logger.error(
                    f"[TIER 2] All {MAX_RETRY_ATTEMPTS} attempts failed for {youtube_id}",
                    "download",
                    {"job_id": job_id, "youtube_id": youtube_id, "final_error": error_msg}
                )

    # Circuit breaker: If all attempts failed with bot detection, wait longer and try one more time
    if bot_detection_count >= MAX_RETRY_ATTEMPTS - 1:
        logger.info(
            f"[TIER 2] Circuit breaker activated: waiting {CIRCUIT_BREAKER_DELAY}s before final attempt",
            "download",
            {"job_id": job_id, "youtube_id": youtube_id, "bot_detections": bot_detection_count}
        )

        await asyncio.sleep(CIRCUIT_BREAKER_DELAY)

        # Final attempt with completely fresh session
        job_progress[job_id]["attempt"] = MAX_RETRY_ATTEMPTS + 1
        job_progress[job_id]["status"] = "extracting"
        proxy_session_id = f"circuit-t2-{uuid.uuid4().hex}"

        # Get proxy config for circuit breaker attempt - use same proxy for both phases
        proxy_config = get_proxy_config(proxy_session_id)
        proxy_url = proxy_config.get("proxy")

        try:
            # Phase 1 with circuit breaker
            logger.info(
                f"[TIER 2 - Circuit Breaker] PHASE 1: Extracting metadata via fresh proxy",
                "download",
                {"job_id": job_id, "youtube_id": youtube_id}
            )
            metadata_result = await extract_audio_url(youtube_id, job_id, proxy_session_id)

            job_progress[job_id]["status"] = "downloading"

            # Phase 2 with same proxy session
            logger.info(
                f"[TIER 2 - Circuit Breaker] PHASE 2: Downloading via aria2c (same proxy session)",
                "download",
                {"job_id": job_id, "youtube_id": youtube_id}
            )
            download_result = await download_from_cdn(
                cdn_url=metadata_result["cdn_url"],
                youtube_id=youtube_id,
                job_id=job_id,
                ext=metadata_result["ext"],
                title=metadata_result["title"],
                proxy_url=proxy_url,
            )

            total_duration = time.time() - total_start
            method_cache.record_success(youtube_id, DownloadMethod.TIER2_PROXY)

            logger.success(
                f"[TIER 2 - Circuit Breaker SUCCESS] {metadata_result['title'][:50]}",
                "download",
                {
                    "job_id": job_id,
                    "youtube_id": youtube_id,
                    "title": metadata_result["title"][:50],
                    "total_time_seconds": round(total_duration, 2),
                    "attempts": MAX_RETRY_ATTEMPTS + 1,
                    "bot_detections": bot_detection_count,
                    "circuit_breaker": True,
                    "method": "tier2_proxy",
                    "cost": COST_TIER2_PROXY,
                }
            )

            return {
                "success": True,
                "file_path": download_result["file_path"],
                "title": metadata_result["title"],
                "duration_seconds": metadata_result["duration_seconds"],
                "youtube_id": youtube_id,
                "ext": metadata_result["ext"],
                "filesize_bytes": download_result["filesize_bytes"],
                "download_time": download_result["download_time"],
                "method": "tier2_proxy",
                "cost": COST_TIER2_PROXY,
            }

        except Exception as e:
            last_error = e
            logger.error(
                f"[TIER 2] Circuit breaker failed for {youtube_id}",
                "download",
                {"job_id": job_id, "error": str(e)[:100]}
            )

    # =================================================================
    # FINAL FALLBACK: Legacy single-phase proxy download
    # =================================================================
    # If Tier 2 two-phase also failed, try the legacy approach as a last resort
    logger.warn(
        f"[TIER 2] Two-phase proxy failed, trying legacy single-phase download for {youtube_id}",
        "download",
        {"job_id": job_id, "youtube_id": youtube_id, "reason": "two_phase_failed"}
    )

    # Try legacy download with fresh proxy session
    for fallback_attempt in range(1, 3):  # 2 fallback attempts
        proxy_session_id = f"legacy-{job_id}-{uuid.uuid4().hex[:8]}"

        try:
            job_progress[job_id]["status"] = "downloading"
            job_progress[job_id]["attempt"] = MAX_RETRY_ATTEMPTS + 1 + fallback_attempt

            logger.info(
                f"[LEGACY FALLBACK {fallback_attempt}] Using single-phase proxy download",
                "download",
                {"job_id": job_id, "youtube_id": youtube_id}
            )

            result = await _download_single_attempt(youtube_id, job_id, fallback_attempt, proxy_session_id)

            total_duration = time.time() - total_start
            method_cache.record_success(youtube_id, DownloadMethod.TIER2_PROXY)

            logger.success(
                f"[LEGACY FALLBACK SUCCESS] {result.get('title', 'Unknown')[:50]}",
                "download",
                {
                    "job_id": job_id,
                    "youtube_id": youtube_id,
                    "title": result.get("title", "Unknown")[:50],
                    "filesize_mb": round(result.get("filesize_bytes", 0) / (1024 * 1024), 2),
                    "total_time_seconds": round(total_duration, 2),
                    "legacy_fallback": True,
                    "method": "tier2_proxy",
                    "cost": COST_TIER2_PROXY,
                }
            )

            result["method"] = "tier2_proxy"
            result["cost"] = COST_TIER2_PROXY
            return result

        except PERMANENT_ERRORS as e:
            logger.error(f"[LEGACY FALLBACK] Permanent error: {e.message}", "download", {
                "job_id": job_id, "error_code": e.error_code
            })
            raise

        except Exception as e:
            last_error = e
            if fallback_attempt < 2:
                logger.warn(
                    f"[LEGACY FALLBACK {fallback_attempt}] failed, retrying...",
                    "download",
                    {"job_id": job_id, "error": str(e)[:100]}
                )
                await asyncio.sleep(2)
            else:
                logger.error(
                    f"[LEGACY FALLBACK] All attempts failed for {youtube_id}",
                    "download",
                    {"job_id": job_id, "error": str(e)[:100]}
                )

    # All retries exhausted (Tier 1 + Tier 2 + Legacy fallback)
    total_duration = time.time() - total_start
    job_progress[job_id] = {
        "status": "failed",
        "progress": 0,
        "error": str(last_error),
        "attempts": MAX_RETRY_ATTEMPTS + 3,
        "bot_detections": bot_detection_count,
    }

    # Return failure with details
    return {
        "success": False,
        "youtube_id": youtube_id,
        "error": str(last_error),
        "attempts": MAX_RETRY_ATTEMPTS + 3,
        "bot_detections": bot_detection_count,
        "total_time": total_duration,
        "method": "all_failed",
        "cost": COST_TIER1_PO_TOKEN + COST_TIER2_PROXY,  # Attempted both
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
