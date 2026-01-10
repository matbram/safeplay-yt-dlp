"""Download endpoint for YouTube videos."""

from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks

from app.models.schemas import DownloadRequest, DownloadResponse
from app.services import youtube, storage
from app.middleware.auth import verify_api_key
from app.utils.exceptions import SafePlayError


router = APIRouter(tags=["download"])


@router.post(
    "/api/download",
    response_model=DownloadResponse,
    responses={
        400: {"description": "Download failed"},
        401: {"description": "Invalid API key"},
        500: {"description": "Internal server error"},
    },
)
async def download_video_endpoint(
    request: DownloadRequest,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_api_key),
) -> DownloadResponse:
    """
    Download a YouTube video and upload to Supabase Storage.

    This endpoint:
    1. Downloads the video via OxyLabs proxy
    2. Uploads to Supabase Storage
    3. Cleans up temp files
    4. Returns the storage path

    No audio extraction - ElevenLabs accepts video files directly.

    Args:
        request: Download request with youtube_id and job_id

    Returns:
        DownloadResponse with storage path and video metadata
    """
    try:
        # Download the video
        result = await youtube.download_video(
            youtube_id=request.youtube_id,
            job_id=request.job_id,
        )

        if not result.get("success"):
            raise HTTPException(
                status_code=400,
                detail={
                    "error_code": "DOWNLOAD_FAILED",
                    "message": result.get("error", "Download failed"),
                }
            )

        # Update progress to uploading
        youtube.update_job_progress(request.job_id, "uploading", 80)

        # Upload to Supabase Storage
        upload_result = await storage.upload_to_supabase(
            local_file_path=result["file_path"],
            youtube_id=request.youtube_id,
            job_id=request.job_id,
        )

        if not upload_result.get("success"):
            raise HTTPException(
                status_code=500,
                detail={
                    "error_code": "UPLOAD_FAILED",
                    "message": upload_result.get("error", "Upload failed"),
                }
            )

        # Update progress to completed
        youtube.update_job_progress(request.job_id, "completed", 100)

        # Schedule cleanup of temp files
        background_tasks.add_task(youtube.cleanup_temp_files, request.job_id)

        return DownloadResponse(
            status="success",
            youtube_id=request.youtube_id,
            job_id=request.job_id,
            storage_path=upload_result["storage_path"],
            duration_seconds=result.get("duration_seconds", 0),
            title=result.get("title", "Unknown"),
            filesize_bytes=upload_result.get("filesize_bytes", 0),
        )

    except SafePlayError as e:
        # Handle known errors
        youtube.update_job_progress(request.job_id, "failed", 0, e.message)
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": e.error_code,
                "message": e.message,
            }
        )

    except HTTPException:
        # Re-raise HTTP exceptions
        raise

    except Exception as e:
        # Handle unexpected errors
        error_msg = str(e)
        youtube.update_job_progress(request.job_id, "failed", 0, error_msg)
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "INTERNAL_ERROR",
                "message": error_msg,
            }
        )
