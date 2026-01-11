"""Supabase storage operations for video uploads."""

import aiofiles
import time
from pathlib import Path
from typing import Optional

from supabase import create_client, Client

from app.config import settings
from app.services import logger


# Initialize Supabase client
supabase: Client = create_client(
    settings.SUPABASE_URL,
    settings.SUPABASE_SERVICE_KEY
)

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
    file_ext = file_path.suffix  # .mp4, .webm, etc.

    # Storage path structure: {youtube_id}/video.mp4
    storage_path = f"{youtube_id}/video{file_ext}"

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

        # Determine content type
        content_types = {
            ".mp4": "video/mp4",
            ".webm": "video/webm",
            ".mkv": "video/x-matroska",
        }
        content_type = content_types.get(file_ext.lower(), "video/mp4")

        # Upload to Supabase Storage
        logger.info(
            f"Uploading to Supabase bucket '{settings.STORAGE_BUCKET}'...",
            "storage",
            {"job_id": job_id, "bucket": settings.STORAGE_BUCKET, "path": storage_path, "content_type": content_type}
        )

        upload_start = time.time()
        supabase.storage.from_(settings.STORAGE_BUCKET).upload(
            path=storage_path,
            file=file_content,
            file_options={"content-type": content_type, "upsert": "true"}
        )
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
    Check if a video already exists in Supabase storage.

    Args:
        youtube_id: YouTube video ID

    Returns:
        dict: {exists: bool, storage_path: str, filesize_bytes: int} or {exists: False}
    """
    try:
        # Check for common video extensions
        folder_path = youtube_id
        result = supabase.storage.from_(settings.STORAGE_BUCKET).list(path=folder_path)

        for item in result:
            name = item.get("name", "")
            if name.startswith("video."):
                storage_path = f"{youtube_id}/{name}"
                filesize = item.get("metadata", {}).get("size", 0)

                logger.info(
                    f"Cache hit! Video already exists: {storage_path}",
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
