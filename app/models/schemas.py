from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


class DownloadRequest(BaseModel):
    """Request model for video download."""

    youtube_id: str = Field(
        ...,
        min_length=11,
        max_length=11,
        description="YouTube video ID (11 characters)",
        examples=["dQw4w9WgXcQ"],
    )
    job_id: str = Field(..., description="Job ID for tracking progress")


class DownloadResponse(BaseModel):
    """Response model for successful download."""

    status: str = "success"
    youtube_id: str
    job_id: str
    storage_path: str
    duration_seconds: int
    title: str
    filesize_bytes: int


class ErrorResponse(BaseModel):
    """Response model for errors."""

    status: str = "error"
    error_code: str
    message: str


class StatusResponse(BaseModel):
    """Response model for job status check."""

    job_id: str
    status: str  # pending, downloading, uploading, completed, failed
    progress: int = Field(ge=0, le=100)
    error: Optional[str] = None


class HealthCheck(BaseModel):
    """Response model for health check."""

    status: str
    timestamp: str
    checks: dict


class VideoInfo(BaseModel):
    """Video information extracted from YouTube."""

    youtube_id: str
    title: str
    duration_seconds: int
    filesize_bytes: Optional[int] = None
    ext: str = "mp4"


class BatchDownloadItem(BaseModel):
    """Single item in a batch download request."""

    youtube_id: str = Field(
        ...,
        min_length=11,
        max_length=11,
        description="YouTube video ID (11 characters)",
    )
    job_id: str = Field(..., description="Job ID for tracking progress")


class BatchDownloadRequest(BaseModel):
    """Request model for batch video downloads."""

    videos: List[BatchDownloadItem] = Field(
        ...,
        min_length=1,
        max_length=20,
        description="List of videos to download (max 20)",
    )


class BatchDownloadResultItem(BaseModel):
    """Result for a single video in batch download."""

    youtube_id: str
    job_id: str
    status: str  # success, failed, cached
    storage_path: Optional[str] = None
    error: Optional[str] = None
    cached: bool = False


class BatchDownloadResponse(BaseModel):
    """Response model for batch download."""

    status: str = "completed"
    total: int
    succeeded: int
    failed: int
    cached: int
    results: List[BatchDownloadResultItem]
