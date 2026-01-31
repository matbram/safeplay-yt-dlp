"""Download endpoint for YouTube videos."""

import asyncio
import time
from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks

from app.models.schemas import (
    DownloadRequest,
    DownloadResponse,
    BatchDownloadRequest,
    BatchDownloadResponse,
    BatchDownloadResultItem,
)
from app.services import youtube, storage, logger
from app.services.agent_hook import (
    emit_download_success,
    emit_download_failure,
    emit_cache_hit,
    flush_telemetry,
)
from app.middleware.auth import verify_api_key
from app.utils.exceptions import SafePlayError, get_error_response, is_retryable


router = APIRouter(tags=["download"])


@router.get("/api/download/status/{job_id}")
async def get_job_status(
    job_id: str,
    api_key: str = Depends(verify_api_key),
):
    """Get the status of a download job."""
    status = youtube.get_job_status(job_id)
    return {
        "job_id": job_id,
        **status
    }


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
    start_time = time.time()

    try:
        logger.info(
            f"Download request received: {request.youtube_id}",
            "download",
            {"job_id": request.job_id, "youtube_id": request.youtube_id}
        )

        # Check cache first - if video already exists, return immediately
        cache_result = await storage.check_video_cache(request.youtube_id)
        if cache_result.get("exists"):
            logger.success(
                f"Cache hit! Returning existing video: {request.youtube_id}",
                "download",
                {
                    "job_id": request.job_id,
                    "youtube_id": request.youtube_id,
                    "storage_path": cache_result["storage_path"],
                    "cached": True
                }
            )
            youtube.update_job_progress(request.job_id, "completed", 100)

            # Emit cache hit telemetry
            await emit_cache_hit(request.job_id, request.youtube_id)

            return DownloadResponse(
                status="success",
                youtube_id=request.youtube_id,
                job_id=request.job_id,
                storage_path=cache_result["storage_path"],
                duration_seconds=0,  # Unknown for cached
                title="(cached)",
                filesize_bytes=cache_result.get("filesize_bytes", 0),
            )

        # Download the video
        result = await youtube.download_video(
            youtube_id=request.youtube_id,
            job_id=request.job_id,
        )

        if not result.get("success"):
            logger.error(
                f"Download failed for {request.youtube_id}",
                "download",
                {"job_id": request.job_id, "error": result.get("error")}
            )
            raise HTTPException(
                status_code=400,
                detail={
                    "error_code": "DOWNLOAD_FAILED",
                    "message": result.get("error", "Download failed"),
                    "retryable": True,  # Generic download failures may be transient
                    "user_message": "Download failed. Please try again.",
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
            logger.error(
                f"Upload failed for {request.youtube_id}",
                "download",
                {"job_id": request.job_id, "error": upload_result.get("error")}
            )
            raise HTTPException(
                status_code=500,
                detail={
                    "error_code": "UPLOAD_FAILED",
                    "message": upload_result.get("error", "Upload failed"),
                    "retryable": True,  # Storage failures are usually transient
                    "user_message": "Failed to save the video. Please try again.",
                }
            )

        # Update progress to completed
        youtube.update_job_progress(request.job_id, "completed", 100)

        logger.success(
            f"Download + upload complete for {request.youtube_id}",
            "download",
            {
                "job_id": request.job_id,
                "youtube_id": request.youtube_id,
                "storage_path": upload_result["storage_path"],
                "filesize_bytes": upload_result.get("filesize_bytes", 0)
            }
        )

        # Schedule cleanup of temp files
        background_tasks.add_task(youtube.cleanup_temp_files, request.job_id)

        # Emit success telemetry
        duration_ms = int((time.time() - start_time) * 1000)
        await emit_download_success(
            job_id=request.job_id,
            youtube_id=request.youtube_id,
            duration_ms=duration_ms,
            result=result,
        )

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
        # Handle known errors with full metadata for orchestration
        youtube.update_job_progress(request.job_id, "failed", 0, e.message)

        # Emit failure telemetry
        duration_ms = int((time.time() - start_time) * 1000)
        await emit_download_failure(
            job_id=request.job_id,
            youtube_id=request.youtube_id,
            duration_ms=duration_ms,
            error=e,
            result={"attempts": 1},
        )

        raise HTTPException(
            status_code=400,
            detail=e.to_dict(),  # Includes error_code, message, retryable, user_message
        )

    except HTTPException:
        # Re-raise HTTP exceptions
        raise

    except Exception as e:
        # Handle unexpected errors - assume retryable (might be transient)
        youtube.update_job_progress(request.job_id, "failed", 0, str(e))

        # Emit failure telemetry
        duration_ms = int((time.time() - start_time) * 1000)
        await emit_download_failure(
            job_id=request.job_id,
            youtube_id=request.youtube_id,
            duration_ms=duration_ms,
            error=e,
            result={"attempts": 1},
        )

        raise HTTPException(
            status_code=500,
            detail=get_error_response(e),  # Standardized error response
        )


async def _process_single_video(
    youtube_id: str,
    job_id: str,
) -> BatchDownloadResultItem:
    """
    Process a single video download for batch operations.
    Returns a result item instead of raising exceptions.
    Includes error metadata for orchestration decision-making.
    """
    try:
        # Check cache first
        cache_result = await storage.check_video_cache(youtube_id)
        if cache_result.get("exists"):
            youtube.update_job_progress(job_id, "completed", 100)
            return BatchDownloadResultItem(
                youtube_id=youtube_id,
                job_id=job_id,
                status="success",
                storage_path=cache_result["storage_path"],
                cached=True,
            )

        # Download the video
        result = await youtube.download_video(
            youtube_id=youtube_id,
            job_id=job_id,
        )

        if not result.get("success"):
            return BatchDownloadResultItem(
                youtube_id=youtube_id,
                job_id=job_id,
                status="failed",
                error=result.get("error", "Download failed"),
                error_code="DOWNLOAD_FAILED",
                retryable=True,  # Generic failures may be transient
                user_message="Download failed. Please try again.",
            )

        # Upload to Supabase Storage
        youtube.update_job_progress(job_id, "uploading", 80)
        upload_result = await storage.upload_to_supabase(
            local_file_path=result["file_path"],
            youtube_id=youtube_id,
            job_id=job_id,
        )

        if not upload_result.get("success"):
            return BatchDownloadResultItem(
                youtube_id=youtube_id,
                job_id=job_id,
                status="failed",
                error=upload_result.get("error", "Upload failed"),
                error_code="UPLOAD_FAILED",
                retryable=True,  # Storage failures usually transient
                user_message="Failed to save the video. Please try again.",
            )

        youtube.update_job_progress(job_id, "completed", 100)

        # Schedule cleanup
        youtube.cleanup_temp_files(job_id)

        return BatchDownloadResultItem(
            youtube_id=youtube_id,
            job_id=job_id,
            status="success",
            storage_path=upload_result["storage_path"],
        )

    except SafePlayError as e:
        # Known error with full metadata
        youtube.update_job_progress(job_id, "failed", 0, e.message)
        return BatchDownloadResultItem(
            youtube_id=youtube_id,
            job_id=job_id,
            status="failed",
            error=e.message,
            error_code=e.error_code,
            retryable=e.retryable,
            user_message=e.user_message,
        )

    except Exception as e:
        # Unknown error - assume retryable
        error_response = get_error_response(e)
        youtube.update_job_progress(job_id, "failed", 0, str(e))
        return BatchDownloadResultItem(
            youtube_id=youtube_id,
            job_id=job_id,
            status="failed",
            error=str(e),
            error_code=error_response["error_code"],
            retryable=error_response["retryable"],
            user_message=error_response["user_message"],
        )


@router.post(
    "/api/download/batch",
    response_model=BatchDownloadResponse,
    responses={
        400: {"description": "Invalid request"},
        401: {"description": "Invalid API key"},
    },
)
async def batch_download_endpoint(
    request: BatchDownloadRequest,
    api_key: str = Depends(verify_api_key),
) -> BatchDownloadResponse:
    """
    Download multiple YouTube videos in parallel.

    All videos are processed concurrently using separate thread pools
    for downloads (8 workers) and uploads (4 workers).

    Args:
        request: Batch download request with list of videos (max 20)

    Returns:
        BatchDownloadResponse with results for each video
    """
    logger.info(
        f"Batch download request received: {len(request.videos)} videos",
        "download",
        {"video_count": len(request.videos)}
    )

    # Create tasks for all videos
    tasks = [
        _process_single_video(video.youtube_id, video.job_id)
        for video in request.videos
    ]

    # Run all downloads in parallel
    results = await asyncio.gather(*tasks)

    # Count results
    succeeded = sum(1 for r in results if r.status == "success" and not r.cached)
    failed = sum(1 for r in results if r.status == "failed")
    cached = sum(1 for r in results if r.cached)

    logger.success(
        f"Batch download complete: {succeeded} succeeded, {cached} cached, {failed} failed",
        "download",
        {"succeeded": succeeded, "cached": cached, "failed": failed}
    )

    return BatchDownloadResponse(
        status="completed",
        total=len(results),
        succeeded=succeeded,
        failed=failed,
        cached=cached,
        results=results,
    )
