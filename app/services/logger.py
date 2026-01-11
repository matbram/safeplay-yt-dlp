"""Server-side logging service with persistence and real-time streaming."""

import json
import os
from datetime import datetime
from pathlib import Path
from collections import deque
from typing import Optional, Callable
import threading

from app.config import settings


# Thread-safe log storage
_log_lock = threading.Lock()
_log_buffer: deque = deque(maxlen=2000)  # Keep last 2000 entries in memory
_log_file: Optional[Path] = None
_log_sequence: int = 0  # Global sequence number for ordering

# Callback for real-time broadcasting (set by admin routes)
_broadcast_callback: Optional[Callable] = None


def set_broadcast_callback(callback: Callable):
    """Set the callback function for broadcasting new logs."""
    global _broadcast_callback
    _broadcast_callback = callback


def _get_log_file() -> Path:
    """Get the log file path, creating directory if needed."""
    global _log_file
    if _log_file is None:
        log_dir = Path("/var/log/safeplay")
        if not log_dir.exists():
            # Fallback to app directory
            log_dir = Path(settings.TEMP_DIR).parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        _log_file = log_dir / "service.jsonl"
    return _log_file


def log(level: str, message: str, category: str = "general", details: Optional[dict] = None):
    """
    Log a message with optional details.

    Args:
        level: Log level (INFO, WARN, ERROR, DEBUG, SUCCESS)
        message: Log message
        category: Category (general, ytdlp, proxy, storage, download)
        details: Optional additional details dict
    """
    global _log_sequence

    with _log_lock:
        _log_sequence += 1
        seq = _log_sequence

    entry = {
        "seq": seq,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "level": level,
        "category": category,
        "message": message,
    }
    if details:
        entry["details"] = details

    with _log_lock:
        _log_buffer.append(entry)

        # Write to file
        try:
            log_file = _get_log_file()
            with open(log_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
                f.flush()  # Ensure immediate write
        except Exception as e:
            # Don't fail if logging fails
            pass

    # Also print to stdout for systemd journal
    print(f"[{entry['timestamp']}] [{level}] [{category}] {message}", flush=True)

    # Trigger real-time broadcast if callback is set
    if _broadcast_callback:
        try:
            _broadcast_callback(entry)
        except Exception:
            pass


def get_logs(limit: int = 100, category: Optional[str] = None, level: Optional[str] = None, since_seq: int = 0) -> list:
    """
    Get recent logs from memory buffer.

    Args:
        limit: Maximum number of logs to return
        category: Filter by category
        level: Filter by level
        since_seq: Only return logs with sequence > since_seq
    """
    with _log_lock:
        logs = list(_log_buffer)

    # Filter by sequence number first (for incremental updates)
    if since_seq > 0:
        logs = [l for l in logs if l.get("seq", 0) > since_seq]

    # Apply filters
    if category:
        logs = [l for l in logs if l.get("category") == category]
    if level:
        logs = [l for l in logs if l.get("level") == level]

    return logs[-limit:]


def get_latest_sequence() -> int:
    """Get the current log sequence number."""
    return _log_sequence


def get_logs_from_file(limit: int = 500, category: Optional[str] = None) -> list:
    """
    Get logs from persistent file storage.
    """
    logs = []
    try:
        log_file = _get_log_file()
        if log_file.exists():
            with open(log_file, "r") as f:
                # Read last N lines efficiently
                lines = f.readlines()[-limit:]
                for line in lines:
                    try:
                        entry = json.loads(line.strip())
                        if category is None or entry.get("category") == category:
                            logs.append(entry)
                    except:
                        pass
    except Exception:
        pass
    return logs


def clear_logs():
    """Clear all logs from memory and file."""
    global _log_buffer
    with _log_lock:
        _log_buffer.clear()
        try:
            log_file = _get_log_file()
            if log_file.exists():
                # Archive old logs
                archive_path = log_file.with_suffix(f".{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl")
                log_file.rename(archive_path)
        except:
            pass
    log("INFO", "Logs cleared", "general")


def get_log_stats() -> dict:
    """Get log statistics."""
    with _log_lock:
        logs = list(_log_buffer)

    stats = {
        "total": len(logs),
        "by_level": {},
        "by_category": {},
    }

    for entry in logs:
        level = entry.get("level", "UNKNOWN")
        category = entry.get("category", "general")
        stats["by_level"][level] = stats["by_level"].get(level, 0) + 1
        stats["by_category"][category] = stats["by_category"].get(category, 0) + 1

    return stats


# Convenience functions
def info(message: str, category: str = "general", details: Optional[dict] = None):
    log("INFO", message, category, details)

def warn(message: str, category: str = "general", details: Optional[dict] = None):
    log("WARN", message, category, details)

def error(message: str, category: str = "general", details: Optional[dict] = None):
    log("ERROR", message, category, details)

def debug(message: str, category: str = "general", details: Optional[dict] = None):
    log("DEBUG", message, category, details)

def success(message: str, category: str = "general", details: Optional[dict] = None):
    log("SUCCESS", message, category, details)


# YT-DLP specific logging
class YtdlpLogger:
    """Custom logger for yt-dlp that captures all output."""

    def __init__(self, job_id: str):
        self.job_id = job_id

    def debug(self, msg):
        if msg.startswith('[debug]'):
            log("DEBUG", msg, "ytdlp", {"job_id": self.job_id})
        else:
            # yt-dlp uses debug for informational messages too
            log("INFO", msg, "ytdlp", {"job_id": self.job_id})

    def info(self, msg):
        log("INFO", msg, "ytdlp", {"job_id": self.job_id})

    def warning(self, msg):
        log("WARN", msg, "ytdlp", {"job_id": self.job_id})

    def error(self, msg):
        log("ERROR", msg, "ytdlp", {"job_id": self.job_id})


# Initialize with startup message
log("INFO", "Logging service initialized", "general")
