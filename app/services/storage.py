"""Supabase storage operations for video uploads."""

import aiofiles
from pathlib import Path
from typing import Optional

from supabase import create_client, Client

from app.config import settings


# Initialize Supabase client
supabase: Client = create_client(
    settings.SUPABASE_URL,
    settings.SUPABASE_SERVICE_KEY
)


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

    try:
        # Read file content
        async with aiofiles.open(local_file_path, "rb") as f:
            file_content = await f.read()

        # Determine content type
        content_types = {
            ".mp4": "video/mp4",
            ".webm": "video/webm",
            ".mkv": "video/x-matroska",
        }
        content_type = content_types.get(file_ext.lower(), "video/mp4")

        # Upload to Supabase Storage
        supabase.storage.from_(settings.STORAGE_BUCKET).upload(
            path=storage_path,
            file=file_content,
            file_options={"content-type": content_type, "upsert": "true"}
        )

        return {
            "success": True,
            "storage_path": storage_path,
            "bucket": settings.STORAGE_BUCKET,
            "filesize_bytes": len(file_content),
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
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
