"""SafePlay custom exceptions with orchestration-friendly metadata."""

from typing import Optional


class SafePlayError(Exception):
    """Base exception for SafePlay errors with orchestration metadata."""

    def __init__(
        self,
        message: str,
        error_code: str = "UNKNOWN_ERROR",
        retryable: bool = False,
        user_message: Optional[str] = None,
    ):
        self.message = message
        self.error_code = error_code
        self.retryable = retryable
        # User-friendly message for the orchestration to show to end users
        self.user_message = user_message or message
        super().__init__(self.message)

    def to_dict(self) -> dict:
        """Convert exception to dictionary for API responses."""
        return {
            "error_code": self.error_code,
            "message": self.message,
            "retryable": self.retryable,
            "user_message": self.user_message,
        }


# =============================================================================
# PERMANENT ERRORS - Do not retry, inform user of the issue
# =============================================================================

class VideoUnavailableError(SafePlayError):
    """Raised when video is unavailable (deleted, region-blocked, etc.)."""

    def __init__(self, message: str = "Video is unavailable"):
        super().__init__(
            message=message,
            error_code="VIDEO_UNAVAILABLE",
            retryable=False,
            user_message="This video doesn't exist or has been removed.",
        )


class PrivateVideoError(SafePlayError):
    """Raised when video is private."""

    def __init__(self, message: str = "Video is private"):
        super().__init__(
            message=message,
            error_code="PRIVATE_VIDEO",
            retryable=False,
            user_message="This video is private and cannot be accessed.",
        )


class AgeRestrictedError(SafePlayError):
    """Raised when video is age-restricted."""

    def __init__(self, message: str = "Video is age-restricted"):
        super().__init__(
            message=message,
            error_code="AGE_RESTRICTED",
            retryable=False,
            user_message="This video requires age verification and cannot be processed.",
        )


class CopyrightBlockedError(SafePlayError):
    """Raised when video is blocked due to copyright."""

    def __init__(self, message: str = "Video is blocked due to copyright"):
        super().__init__(
            message=message,
            error_code="COPYRIGHT_BLOCKED",
            retryable=False,
            user_message="This video is blocked due to copyright restrictions.",
        )


class LiveStreamError(SafePlayError):
    """Raised when trying to download a live stream."""

    def __init__(self, message: str = "Cannot download live streams"):
        super().__init__(
            message=message,
            error_code="LIVE_STREAM",
            retryable=False,
            user_message="Live streams cannot be processed. Please try again after the stream ends.",
        )


class PremiumContentError(SafePlayError):
    """Raised when video requires YouTube Premium."""

    def __init__(self, message: str = "Video requires YouTube Premium"):
        super().__init__(
            message=message,
            error_code="PREMIUM_CONTENT",
            retryable=False,
            user_message="This video requires YouTube Premium and cannot be processed.",
        )


# =============================================================================
# RETRYABLE ERRORS - Temporary issues, orchestration may retry
# =============================================================================

class DownloadError(SafePlayError):
    """Raised when download fails for unknown reasons."""

    def __init__(self, message: str = "Download failed"):
        super().__init__(
            message=message,
            error_code="DOWNLOAD_ERROR",
            retryable=True,
            user_message="Download failed. Please try again.",
        )


class UploadError(SafePlayError):
    """Raised when upload to storage fails."""

    def __init__(self, message: str = "Upload to storage failed"):
        super().__init__(
            message=message,
            error_code="UPLOAD_ERROR",
            retryable=True,
            user_message="Failed to save the video. Please try again.",
        )


class ProxyError(SafePlayError):
    """Raised when proxy connection fails."""

    def __init__(self, message: str = "Proxy connection failed"):
        super().__init__(
            message=message,
            error_code="PROXY_ERROR",
            retryable=True,
            user_message="Connection issue. Please try again.",
        )


class DownloadTimeoutError(SafePlayError):
    """Raised when download times out."""

    def __init__(self, message: str = "Download timed out"):
        super().__init__(
            message=message,
            error_code="DOWNLOAD_TIMEOUT",
            retryable=True,
            user_message="Download took too long. Please try again.",
        )


class BotDetectionError(SafePlayError):
    """Raised when YouTube detects us as a bot - retryable with new IP."""

    def __init__(self, message: str = "Bot detection triggered"):
        super().__init__(
            message=message,
            error_code="BOT_DETECTED",
            retryable=True,
            user_message="Temporary issue with YouTube. Please try again in a moment.",
        )


class RateLimitError(SafePlayError):
    """Raised when we hit rate limits."""

    def __init__(self, message: str = "Rate limit exceeded"):
        super().__init__(
            message=message,
            error_code="RATE_LIMITED",
            retryable=True,
            user_message="Too many requests. Please wait a moment and try again.",
        )


# =============================================================================
# ERROR CLASSIFICATION HELPERS
# =============================================================================

# Permanent errors - orchestration should NOT retry, show error to user
PERMANENT_ERRORS = (
    VideoUnavailableError,
    PrivateVideoError,
    AgeRestrictedError,
    CopyrightBlockedError,
    LiveStreamError,
    PremiumContentError,
)

# Retryable errors - orchestration MAY retry or ask user to try again
RETRYABLE_ERRORS = (
    DownloadError,
    UploadError,
    ProxyError,
    DownloadTimeoutError,
    BotDetectionError,
    RateLimitError,
)


def is_retryable(error: Exception) -> bool:
    """Check if an error is retryable."""
    if isinstance(error, SafePlayError):
        return error.retryable
    # Unknown errors are assumed retryable (might be transient)
    return True


def get_error_response(error: Exception) -> dict:
    """Get a standardized error response dict from any exception."""
    if isinstance(error, SafePlayError):
        return error.to_dict()

    # For unknown exceptions, return a generic retryable error
    return {
        "error_code": "INTERNAL_ERROR",
        "message": str(error),
        "retryable": True,
        "user_message": "An unexpected error occurred. Please try again.",
    }
