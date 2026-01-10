"""Job status endpoint."""

from fastapi import APIRouter, Depends, HTTPException

from app.models.schemas import StatusResponse
from app.services.youtube import get_job_status
from app.middleware.auth import verify_api_key


router = APIRouter(tags=["status"])


@router.get(
    "/api/download/{job_id}/status",
    response_model=StatusResponse,
)
async def get_download_status(
    job_id: str,
    api_key: str = Depends(verify_api_key),
) -> StatusResponse:
    """
    Check download progress for a job.

    Args:
        job_id: The job ID to check status for

    Returns:
        StatusResponse with current status and progress
    """
    status_data = get_job_status(job_id)

    if status_data.get("status") == "unknown":
        raise HTTPException(
            status_code=404,
            detail=f"Job {job_id} not found"
        )

    return StatusResponse(
        job_id=job_id,
        status=status_data.get("status", "unknown"),
        progress=status_data.get("progress", 0),
        error=status_data.get("error"),
    )
