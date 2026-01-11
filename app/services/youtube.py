"""YouTube video download service using yt-dlp with verbose logging and speed optimizations."""

import asyncio
import shutil
import subprocess
import time
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
)

# Thread pool for running blocking downloads
_download_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ytdlp")


def _check_aria2c_available() -> bool:
    """Check if aria2c is available for faster downloads."""
    try:
        result = subprocess.run(["aria2c", "--version"], capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


# Check aria2c availability at startup
ARIA2C_AVAILABLE = _check_aria2c_available()
if ARIA2C_AVAILABLE:
    logger.info("aria2c detected - will use for faster multi-connection downloads", "ytdlp")
else:
    logger.warn("aria2c not found - downloads will be slower. Install with: apt install aria2", "ytdlp")


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


async def download_video(youtube_id: str, job_id: str) -> dict:
    """
    Download YouTube video using yt-dlp through OxyLabs proxy.
    Includes verbose logging for debugging.
    """
    url = f"https://www.youtube.com/watch?v={youtube_id}"
    temp_dir = Path(settings.TEMP_DIR) / job_id
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Initialize progress
    job_progress[job_id] = {"status": "pending", "progress": 0}

    logger.info(
        f"Starting download for video: {youtube_id}",
        "download",
        {"job_id": job_id, "youtube_id": youtube_id, "url": url}
    )

    # Get proxy config with sticky session for this job
    proxy_config = get_proxy_config(job_id)
    if proxy_config.get("proxy"):
        proxy_url = proxy_config["proxy"]
        # Mask password in log
        masked_proxy = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
        logger.info(f"Using proxy: {masked_proxy}", "proxy", {"job_id": job_id})
    else:
        logger.warn("No proxy configured - downloading directly", "proxy", {"job_id": job_id})

    # Create custom logger for yt-dlp
    ytdlp_logger = logger.YtdlpLogger(job_id)

    # yt-dlp options with verbose logging and SPEED OPTIMIZATIONS
    ydl_opts = {
        # Proxy configuration
        **proxy_config,
        # Output template
        "outtmpl": str(temp_dir / f"{youtube_id}.%(ext)s"),
        # Video format - 720p max to save bandwidth, prefer mp4
        # Prefer non-DASH formats when possible (less throttling)
        "format": "best[height<=720][ext=mp4]/best[height<=720]/best",
        # Progress tracking
        "progress_hooks": [lambda d: _progress_hook(d, job_id)],
        # Verbose logging (captured by our logger)
        "verbose": True,
        "logger": ytdlp_logger,
        # Retry settings
        "retries": 5,
        "fragment_retries": 10,
        # Don't download playlists
        "noplaylist": True,
        # Avoid geo-restrictions
        "geo_bypass": True,
        # Additional debug info
        "print_traffic": False,  # Set True for extreme debugging

        # === SPEED OPTIMIZATIONS ===
        # Download multiple fragments concurrently (for HLS/DASH streams)
        "concurrent_fragment_downloads": 8,

        # Buffer settings for faster throughput
        "buffersize": 1024 * 64,  # 64KB buffer

        # HTTP chunk size for non-fragmented downloads
        "http_chunk_size": 10485760,  # 10MB chunks
    }

    # Use aria2c if available for MUCH faster downloads (multi-connection)
    if ARIA2C_AVAILABLE:
        logger.info("Using aria2c external downloader for faster speeds", "ytdlp", {"job_id": job_id})
        ydl_opts["external_downloader"] = "aria2c"
        # aria2c args: -x=connections per server, -s=servers, -k=min split size
        ydl_opts["external_downloader_args"] = {
            "aria2c": [
                "-x", "16",      # 16 connections per server
                "-s", "16",      # 16 servers
                "-k", "1M",      # 1MB minimum split size
                "--file-allocation=none",  # Faster file creation
                "--max-connection-per-server=16",
                "--min-split-size=1M",
                "--split=16",
            ]
        }

    start_time = time.time()

    # Define blocking download function to run in thread
    def _blocking_download():
        """Run the blocking yt-dlp download in a separate thread."""
        logger.info("Initializing yt-dlp...", "ytdlp", {"job_id": job_id, "options": {
            "format": ydl_opts["format"],
            "retries": ydl_opts["retries"],
            "geo_bypass": ydl_opts["geo_bypass"],
        }})

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Extract info and download
            logger.info("Extracting video info...", "ytdlp", {"job_id": job_id})
            info = ydl.extract_info(url, download=True)

            # Find the downloaded file
            ext = info.get("ext", "mp4")
            output_file = temp_dir / f"{youtube_id}.{ext}"

            # Get file size
            filesize = output_file.stat().st_size if output_file.exists() else 0
            duration = time.time() - start_time

            return {
                "info": info,
                "ext": ext,
                "output_file": output_file,
                "filesize": filesize,
                "duration": duration,
            }

    try:
        # Run blocking download in thread pool to not block event loop
        # This allows WebSocket to send real-time log updates
        loop = asyncio.get_event_loop()
        download_result = await loop.run_in_executor(_download_executor, _blocking_download)

        info = download_result["info"]
        ext = download_result["ext"]
        filesize = download_result["filesize"]
        duration = download_result["duration"]
        output_file = download_result["output_file"]

        result = {
            "success": True,
            "file_path": str(output_file),
            "title": info.get("title", "Unknown"),
            "duration_seconds": info.get("duration", 0),
            "youtube_id": youtube_id,
            "ext": ext,
            "filesize_bytes": filesize,
        }

        logger.success(
            f"Download complete: {info.get('title', 'Unknown')}",
            "download",
            {
                "job_id": job_id,
                "youtube_id": youtube_id,
                "title": info.get("title"),
                "duration_seconds": info.get("duration"),
                "filesize_bytes": filesize,
                "filesize_mb": round(filesize / (1024 * 1024), 2),
                "download_time_seconds": round(duration, 2),
                "format": info.get("format"),
                "resolution": info.get("resolution"),
                "ext": ext,
            }
        )

        return result

    except DownloadCancelled:
        # Clean up cancelled job
        job_progress[job_id] = {
            "status": "cancelled",
            "progress": 0,
            "error": "Cancelled by user",
        }
        cancelled_jobs.discard(job_id)

        # Clean up temp files
        try:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
        except Exception:
            pass

        return {
            "success": False,
            "cancelled": True,
            "youtube_id": youtube_id,
            "error": "Download cancelled by user",
        }

    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        duration = time.time() - start_time

        logger.error(
            f"yt-dlp download error: {error_msg}",
            "ytdlp",
            {
                "job_id": job_id,
                "youtube_id": youtube_id,
                "error_type": "DownloadError",
                "error_message": error_msg,
                "duration_seconds": round(duration, 2),
            }
        )

        job_progress[job_id] = {
            "status": "failed",
            "progress": 0,
            "error": error_msg,
        }

        # Classify the error
        if "Video unavailable" in error_msg or "removed" in error_msg.lower():
            logger.error("Video is unavailable or removed", "download", {"job_id": job_id})
            raise VideoUnavailableError(error_msg)
        elif "Private video" in error_msg:
            logger.error("Video is private", "download", {"job_id": job_id})
            raise PrivateVideoError(error_msg)
        elif "age" in error_msg.lower() or "sign in" in error_msg.lower():
            logger.error("Video is age-restricted", "download", {"job_id": job_id})
            raise AgeRestrictedError(error_msg)
        elif "copyright" in error_msg.lower():
            logger.error("Video blocked due to copyright", "download", {"job_id": job_id})
            raise CopyrightBlockedError(error_msg)
        else:
            raise DownloadError(error_msg)

    except Exception as e:
        error_msg = str(e)
        duration = time.time() - start_time

        logger.error(
            f"Unexpected error during download: {error_msg}",
            "download",
            {
                "job_id": job_id,
                "youtube_id": youtube_id,
                "error_type": type(e).__name__,
                "error_message": error_msg,
                "duration_seconds": round(duration, 2),
            }
        )

        job_progress[job_id] = {
            "status": "failed",
            "progress": 0,
            "error": error_msg,
        }
        raise DownloadError(error_msg)


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
