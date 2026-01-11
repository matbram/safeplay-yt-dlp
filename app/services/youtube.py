"""YouTube video download service using yt-dlp with verbose logging and speed optimizations."""

import asyncio
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

import yt_dlp

from app.config import settings
from app.services.proxy import get_proxy_config
from app.services import logger
from app.utils.exceptions import (
    VideoUnavailableError,
    PrivateVideoError,
    AgeRestrictedError,
    CopyrightBlockedError,
    DownloadError,
    DownloadTimeoutError,
    PERMANENT_ERRORS,
)

# === RELIABILITY CONFIGURATION ===
DOWNLOAD_TIMEOUT_SECONDS = 60  # Max time for a single download attempt
MAX_RETRY_ATTEMPTS = 3  # Number of retry attempts
RETRY_BACKOFF_SECONDS = [1, 2]  # Backoff between retries (fast for speed)

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


def _classify_error(error_msg: str) -> Exception:
    """Classify an error message into the appropriate exception type."""
    error_lower = error_msg.lower()

    # Check for bot detection FIRST - this is retryable with new IP
    if "confirm you're not a bot" in error_lower or "confirm your not a bot" in error_lower:
        return DownloadError(error_msg)  # Retryable - try new proxy IP

    if "video unavailable" in error_lower or "removed" in error_lower:
        return VideoUnavailableError(error_msg)
    elif "private video" in error_lower:
        return PrivateVideoError(error_msg)
    elif "age" in error_lower and "restrict" in error_lower:
        # Only age-restricted, not bot detection
        return AgeRestrictedError(error_msg)
    elif "copyright" in error_lower:
        return CopyrightBlockedError(error_msg)
    else:
        return DownloadError(error_msg)


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
        "format": "best[height<=720][ext=mp4]/best[height<=720]/best",
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
    }

    # Enable Node.js for YouTube bot challenge solving
    if NODEJS_AVAILABLE:
        ydl_opts["js_runtimes"] = {"node": {}}

    # Use aria2c if available
    if ARIA2C_AVAILABLE:
        ydl_opts["external_downloader"] = "aria2c"
        ydl_opts["external_downloader_args"] = {
            "aria2c": [
                "-x", "16", "-s", "16", "-k", "1M",
                "--file-allocation=none",
                "--max-connection-per-server=16",
                "--min-split-size=1M", "--split=16",
                "--timeout=30",  # aria2c timeout
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
    Download YouTube video with automatic retries and timeouts.

    Features:
    - 60 second timeout per attempt (fail fast)
    - Up to 3 retry attempts with new proxy sessions
    - Fast backoff (1s, 2s) between retries
    - Immediate failure for permanent errors (video unavailable, private, etc.)

    Args:
        youtube_id: YouTube video ID
        job_id: Job ID for tracking

    Returns:
        dict with success status and file info
    """
    # Initialize progress
    job_progress[job_id] = {"status": "pending", "progress": 0, "attempt": 1}

    logger.info(
        f"Starting download for video: {youtube_id}",
        "download",
        {"job_id": job_id, "youtube_id": youtube_id, "max_attempts": MAX_RETRY_ATTEMPTS}
    )

    last_error = None
    total_start = time.time()

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

        # Use unique proxy session for each attempt (new IP on retry)
        proxy_session_id = f"{job_id}-attempt{attempt}-{uuid.uuid4().hex[:8]}"

        try:
            result = await _download_single_attempt(youtube_id, job_id, attempt, proxy_session_id)

            # Success! Log and return
            total_duration = time.time() - total_start
            logger.success(
                f"Download complete: {result.get('title', 'Unknown')} (attempt {attempt})",
                "download",
                {
                    "job_id": job_id,
                    "youtube_id": youtube_id,
                    "title": result.get("title"),
                    "filesize_mb": round(result.get("filesize_bytes", 0) / (1024 * 1024), 2),
                    "download_time_seconds": round(result.get("download_time", 0), 2),
                    "total_time_seconds": round(total_duration, 2),
                    "attempts": attempt,
                }
            )
            return result

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

        except Exception as e:
            last_error = e
            error_msg = str(e) if not hasattr(e, 'message') else e.message

            # Check if we should retry
            if attempt < MAX_RETRY_ATTEMPTS:
                backoff = RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
                logger.warn(
                    f"Attempt {attempt} failed, retrying in {backoff}s with new proxy...",
                    "download",
                    {"job_id": job_id, "attempt": attempt, "backoff": backoff, "error": error_msg[:100]}
                )
                await asyncio.sleep(backoff)
            else:
                logger.error(
                    f"All {MAX_RETRY_ATTEMPTS} attempts failed for {youtube_id}",
                    "download",
                    {"job_id": job_id, "youtube_id": youtube_id, "final_error": error_msg}
                )

    # All retries exhausted
    total_duration = time.time() - total_start
    job_progress[job_id] = {
        "status": "failed",
        "progress": 0,
        "error": str(last_error),
        "attempts": MAX_RETRY_ATTEMPTS,
    }

    # Return failure with details
    return {
        "success": False,
        "youtube_id": youtube_id,
        "error": str(last_error),
        "attempts": MAX_RETRY_ATTEMPTS,
        "total_time": total_duration,
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
