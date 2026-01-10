from typing import Optional


class SafePlayError(Exception):
    """Base exception for SafePlay errors."""

    def __init__(self, message: str, error_code: str = "UNKNOWN_ERROR"):
        self.message = message
        self.error_code = error_code
        super().__init__(self.message)


class VideoUnavailableError(SafePlayError):
    """Raised when video is unavailable."""

    def __init__(self, message: str = "Video is unavailable"):
        super().__init__(message, "VIDEO_UNAVAILABLE")


class PrivateVideoError(SafePlayError):
    """Raised when video is private."""

    def __init__(self, message: str = "Video is private"):
        super().__init__(message, "PRIVATE_VIDEO")


class AgeRestrictedError(SafePlayError):
    """Raised when video is age-restricted."""

    def __init__(self, message: str = "Video is age-restricted"):
        super().__init__(message, "AGE_RESTRICTED")


class CopyrightBlockedError(SafePlayError):
    """Raised when video is blocked due to copyright."""

    def __init__(self, message: str = "Video is blocked due to copyright"):
        super().__init__(message, "COPYRIGHT_BLOCKED")


class DownloadError(SafePlayError):
    """Raised when download fails."""

    def __init__(self, message: str = "Download failed"):
        super().__init__(message, "DOWNLOAD_ERROR")


class UploadError(SafePlayError):
    """Raised when upload to storage fails."""

    def __init__(self, message: str = "Upload to storage failed"):
        super().__init__(message, "UPLOAD_ERROR")


class ProxyError(SafePlayError):
    """Raised when proxy connection fails."""

    def __init__(self, message: str = "Proxy connection failed"):
        super().__init__(message, "PROXY_ERROR")
