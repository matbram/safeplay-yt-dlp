"""Supabase storage operations for video uploads."""

import asyncio
import aiofiles
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from supabase import create_client, Client

from app.config import settings
from app.services import logger

# === RELIABILITY CONFIGURATION ===
UPLOAD_TIMEOUT_SECONDS = 45  # Max time for upload (generous for large files)

# Initialize Supabase client
supabase: Client = create_client(
    settings.SUPABASE_URL,
    settings.SUPABASE_SERVICE_KEY
)

# Thread pool for uploads (separate from download pool for true parallelism)
_upload_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="upload")

logger.info("Supabase storage client initialized", "storage")


async def upload_to_supabase(
    local_file_path: str,
    youtube_id: str,
    job_id: str
) -> dict:
    """
    Upload a video file to Supabase Storage.

    Args:
        local_file_path: Path to the local file
        youtube_id: YouTube video ID
        job_id: Job ID for tracking

    Returns:
        dict: Upload result with storage_path and filesize_bytes
    """
    file_path = Path(local_file_path)
    file_ext = file_path.suffix  # .m4a, .webm, .mp3, etc.

    # Storage path structure: {youtube_id}/audio.m4a
    storage_path = f"{youtube_id}/audio{file_ext}"

    logger.info(
        f"Starting upload to Supabase: {storage_path}",
        "storage",
        {"job_id": job_id, "youtube_id": youtube_id, "local_path": local_file_path}
    )

    try:
        # Read file content
        start_time = time.time()
        logger.debug(f"Reading file: {local_file_path}", "storage", {"job_id": job_id})

        async with aiofiles.open(local_file_path, "rb") as f:
            file_content = await f.read()

        file_size = len(file_content)
        read_time = time.time() - start_time
        logger.info(
            f"File read complete: {file_size / (1024*1024):.2f} MB in {read_time:.2f}s",
            "storage",
            {"job_id": job_id, "filesize_bytes": file_size, "read_time_seconds": read_time}
        )

        # Determine content type (audio formats for transcription)
        content_types = {
            ".m4a": "audio/mp4",
            ".mp3": "audio/mpeg",
            ".webm": "audio/webm",
            ".ogg": "audio/ogg",
            ".opus": "audio/opus",
            ".wav": "audio/wav",
        }
        content_type = content_types.get(file_ext.lower(), "audio/mp4")

        # Upload to Supabase Storage (run in thread pool for parallel execution)
        logger.info(
            f"Uploading to Supabase bucket '{settings.STORAGE_BUCKET}'...",
            "storage",
            {"job_id": job_id, "bucket": settings.STORAGE_BUCKET, "path": storage_path, "content_type": content_type}
        )

        def _blocking_upload():
            """Run the blocking Supabase upload in a thread."""
            supabase.storage.from_(settings.STORAGE_BUCKET).upload(
                path=storage_path,
                file=file_content,
                file_options={"content-type": content_type, "upsert": "true"}
            )

        upload_start = time.time()
        try:
            loop = asyncio.get_event_loop()
            upload_task = loop.run_in_executor(_upload_executor, _blocking_upload)
            await asyncio.wait_for(upload_task, timeout=UPLOAD_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            upload_time = time.time() - upload_start
            logger.error(
                f"Upload timed out after {upload_time:.1f}s",
                "storage",
                {"job_id": job_id, "timeout": UPLOAD_TIMEOUT_SECONDS, "filesize_mb": file_size / (1024*1024)}
            )
            return {
                "success": False,
                "error": f"Upload timed out after {UPLOAD_TIMEOUT_SECONDS}s",
            }
        upload_time = time.time() - upload_start

        logger.success(
            f"Upload complete: {storage_path} ({file_size / (1024*1024):.2f} MB in {upload_time:.2f}s)",
            "storage",
            {
                "job_id": job_id,
                "storage_path": storage_path,
                "filesize_bytes": file_size,
                "upload_time_seconds": upload_time,
                "upload_speed_mbps": (file_size / (1024*1024)) / upload_time if upload_time > 0 else 0
            }
        )

        return {
            "success": True,
            "storage_path": storage_path,
            "bucket": settings.STORAGE_BUCKET,
            "filesize_bytes": file_size,
        }

    except Exception as e:
        error_msg = str(e)
        logger.error(
            f"Upload failed: {error_msg}",
            "storage",
            {
                "job_id": job_id,
                "youtube_id": youtube_id,
                "storage_path": storage_path,
                "error": error_msg,
                "error_type": type(e).__name__
            }
        )
        return {
            "success": False,
            "error": error_msg,
        }


async def get_signed_url(storage_path: str, expires_in: int = 3600) -> Optional[str]:
    """
    Get a signed URL for accessing a file in storage.

    Args:
        storage_path: Path to the file in storage
        expires_in: URL expiry time in seconds (default 1 hour)

    Returns:
        str: Signed URL or None if failed
    """
    try:
        result = supabase.storage.from_(settings.STORAGE_BUCKET).create_signed_url(
            path=storage_path,
            expires_in=expires_in
        )
        return result.get("signedURL")
    except Exception:
        return None


async def delete_file(storage_path: str) -> bool:
    """
    Delete a file from storage.

    Args:
        storage_path: Path to the file in storage

    Returns:
        bool: True if deletion was successful
    """
    try:
        supabase.storage.from_(settings.STORAGE_BUCKET).remove([storage_path])
        return True
    except Exception:
        return False


async def file_exists(storage_path: str) -> bool:
    """
    Check if a file exists in storage.

    Args:
        storage_path: Path to the file in storage

    Returns:
        bool: True if file exists
    """
    try:
        # Try to get file info
        result = supabase.storage.from_(settings.STORAGE_BUCKET).list(
            path=storage_path.rsplit("/", 1)[0] if "/" in storage_path else ""
        )
        filename = storage_path.rsplit("/", 1)[-1] if "/" in storage_path else storage_path
        return any(item.get("name") == filename for item in result)
    except Exception:
        return False


async def check_video_cache(youtube_id: str) -> dict:
    """
    Check if audio already exists in Supabase storage.

    Args:
        youtube_id: YouTube video ID

    Returns:
        dict: {exists: bool, storage_path: str, filesize_bytes: int} or {exists: False}
    """
    try:
        # Check for audio files (also check legacy video files for backwards compat)
        folder_path = youtube_id
        result = supabase.storage.from_(settings.STORAGE_BUCKET).list(path=folder_path)

        for item in result:
            name = item.get("name", "")
            # Check for audio files first, then video files for backwards compatibility
            if name.startswith("audio.") or name.startswith("video."):
                storage_path = f"{youtube_id}/{name}"
                filesize = item.get("metadata", {}).get("size", 0)

                logger.info(
                    f"Cache hit! Audio already exists: {storage_path}",
                    "storage",
                    {"youtube_id": youtube_id, "storage_path": storage_path, "filesize_bytes": filesize}
                )

                return {
                    "exists": True,
                    "storage_path": storage_path,
                    "filesize_bytes": filesize,
                }

        return {"exists": False}

    except Exception as e:
        # If folder doesn't exist, that's fine - video not cached
        logger.debug(f"Cache check for {youtube_id}: not found", "storage")
        return {"exists": False}


def test_supabase_connection() -> bool:
    """
    Test if Supabase connection is working.

    Returns:
        bool: True if connected
    """
    try:
        # Try to list buckets
        supabase.storage.list_buckets()
        return True
    except Exception:
        return False
