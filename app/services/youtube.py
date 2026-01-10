"""YouTube video download service using yt-dlp."""

import shutil
from pathlib import Path
from typing import Optional

import yt_dlp

from app.config import settings
from app.services.proxy import get_proxy_config
from app.utils.exceptions import (
    VideoUnavailableError,
    PrivateVideoError,
    AgeRestrictedError,
    CopyrightBlockedError,
    DownloadError,
)


# In-memory job progress tracking
# For production scale, consider using Redis
job_progress: dict[str, dict] = {}


def _progress_hook(d: dict, job_id: str) -> None:
    """
    Track download progress.

    Args:
        d: Progress dictionary from yt-dlp
        job_id: Job ID for tracking
    """
    if d["status"] == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
        downloaded = d.get("downloaded_bytes", 0)
        if total > 0:
            # 0-75% for download phase
            progress = int((downloaded / total) * 75)
            job_progress[job_id] = {
                "status": "downloading",
                "progress": progress,
            }
    elif d["status"] == "finished":
        job_progress[job_id] = {
            "status": "uploading",
            "progress": 75,
        }


async def download_video(youtube_id: str, job_id: str) -> dict:
    """
    Download YouTube video using yt-dlp through OxyLabs proxy.
    No audio extraction needed - ElevenLabs accepts video directly.

    Args:
        youtube_id: YouTube video ID
        job_id: Job ID for progress tracking

    Returns:
        dict: Download result with file path, title, duration, etc.
    """
    url = f"https://www.youtube.com/watch?v={youtube_id}"
    temp_dir = Path(settings.TEMP_DIR) / job_id
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Initialize progress
    job_progress[job_id] = {"status": "pending", "progress": 0}

    # yt-dlp options
    ydl_opts = {
        # Proxy configuration
        **get_proxy_config(),
        # Output template
        "outtmpl": str(temp_dir / f"{youtube_id}.%(ext)s"),
        # Video format - 720p max to save bandwidth, prefer mp4
        "format": "best[height<=720][ext=mp4]/best[height<=720]/best",
        # Progress tracking
        "progress_hooks": [lambda d: _progress_hook(d, job_id)],
        # Quiet mode for production
        "quiet": True,
        "no_warnings": True,
        # Retry settings
        "retries": 3,
        "fragment_retries": 3,
        # Don't download playlists
        "noplaylist": True,
        # Avoid geo-restrictions
        "geo_bypass": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Extract info and download
            info = ydl.extract_info(url, download=True)

            # Find the downloaded file
            ext = info.get("ext", "mp4")
            output_file = temp_dir / f"{youtube_id}.{ext}"

            # Get file size
            filesize = output_file.stat().st_size if output_file.exists() else 0

            return {
                "success": True,
                "file_path": str(output_file),
                "title": info.get("title", "Unknown"),
                "duration_seconds": info.get("duration", 0),
                "youtube_id": youtube_id,
                "ext": ext,
                "filesize_bytes": filesize,
            }

    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        job_progress[job_id] = {
            "status": "failed",
            "progress": 0,
            "error": error_msg,
        }

        # Classify the error
        if "Video unavailable" in error_msg or "removed" in error_msg.lower():
            raise VideoUnavailableError(error_msg)
        elif "Private video" in error_msg:
            raise PrivateVideoError(error_msg)
        elif "age" in error_msg.lower() or "sign in" in error_msg.lower():
            raise AgeRestrictedError(error_msg)
        elif "copyright" in error_msg.lower():
            raise CopyrightBlockedError(error_msg)
        else:
            raise DownloadError(error_msg)

    except Exception as e:
        error_msg = str(e)
        job_progress[job_id] = {
            "status": "failed",
            "progress": 0,
            "error": error_msg,
        }
        raise DownloadError(error_msg)


def get_job_status(job_id: str) -> dict:
    """
    Get current job status.

    Args:
        job_id: Job ID to check

    Returns:
        dict: Job status with progress
    """
    return job_progress.get(job_id, {"status": "unknown", "progress": 0})


def update_job_progress(job_id: str, status: str, progress: int, error: Optional[str] = None) -> None:
    """
    Update job progress.

    Args:
        job_id: Job ID to update
        status: New status
        progress: Progress percentage (0-100)
        error: Optional error message
    """
    job_progress[job_id] = {
        "status": status,
        "progress": progress,
    }
    if error:
        job_progress[job_id]["error"] = error


def cleanup_temp_files(job_id: str) -> None:
    """
    Clean up temporary files after upload.

    Args:
        job_id: Job ID whose temp files should be cleaned
    """
    temp_dir = Path(settings.TEMP_DIR) / job_id
    if temp_dir.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)

    # Also clean up from progress tracking
    if job_id in job_progress:
        del job_progress[job_id]


def cleanup_old_temp_files(max_age_hours: int = 24) -> int:
    """
    Clean up old temporary files.

    Args:
        max_age_hours: Maximum age in hours before cleanup

    Returns:
        int: Number of directories cleaned
    """
    import time

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

    return cleaned


async def get_video_info(youtube_id: str) -> Optional[dict]:
    """
    Get video information without downloading.

    Args:
        youtube_id: YouTube video ID

    Returns:
        dict: Video information or None if failed
    """
    url = f"https://www.youtube.com/watch?v={youtube_id}"

    ydl_opts = {
        **get_proxy_config(),
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "youtube_id": youtube_id,
                "title": info.get("title"),
                "duration_seconds": info.get("duration"),
                "description": info.get("description"),
                "uploader": info.get("uploader"),
                "view_count": info.get("view_count"),
            }
    except Exception:
        return None
