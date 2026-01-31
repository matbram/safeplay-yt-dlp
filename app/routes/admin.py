"""Admin dashboard routes with real-time updates and configuration."""

import asyncio
import os
import subprocess
from datetime import datetime
from typing import Optional
from collections import deque

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.config import settings
from app.middleware.auth import verify_api_key
from app.services.youtube import (
    job_progress, cancel_job, cancel_all_jobs, clear_completed_jobs,
    COST_PER_DOWNLOAD,
)
from app.services import logger
from app.services.download_config import (
    DownloadConfig,
    get_config as get_download_config,
    update_config as update_download_config,
)


router = APIRouter(tags=["admin"])

# Queue for real-time log broadcasting
_pending_logs: deque = deque(maxlen=500)
_pending_logs_lock = asyncio.Lock()


def _queue_log_for_broadcast(entry: dict):
    """Queue a log entry for broadcast to WebSocket clients."""
    # This is called synchronously from the logger, so we just append
    _pending_logs.append(entry)


# Register the broadcast callback with the logger
logger.set_broadcast_callback(_queue_log_for_broadcast)


@router.get("/api/admin/config", response_model=DownloadConfig)
async def get_config(api_key: str = Depends(verify_api_key)):
    """Get current download configuration."""
    return get_download_config()


@router.post("/api/admin/config", response_model=DownloadConfig)
async def update_config(config: DownloadConfig, api_key: str = Depends(verify_api_key)):
    """Update download configuration.

    These settings are used by the youtube.py downloader in real-time.
    """
    updated = update_download_config(config.model_dump())
    logger.info(f"Configuration updated: retries={updated.retries}, fragment_retries={updated.fragment_retries}, geo_bypass={updated.geo_bypass}", "admin")
    return updated


@router.get("/api/admin/logs")
async def get_logs(
    limit: int = 100,
    category: Optional[str] = None,
    level: Optional[str] = None,
    api_key: str = Depends(verify_api_key)
):
    """Get recent logs from server-side storage."""
    logs = logger.get_logs(limit=limit, category=category, level=level)
    return {"logs": logs}


@router.get("/api/admin/logs/file")
async def get_logs_from_file(
    limit: int = 500,
    category: Optional[str] = None,
    api_key: str = Depends(verify_api_key)
):
    """Get logs from persistent file storage."""
    logs = logger.get_logs_from_file(limit=limit, category=category)
    return {"logs": logs}


@router.delete("/api/admin/logs")
async def clear_logs(api_key: str = Depends(verify_api_key)):
    """Clear all logs."""
    logger.clear_logs()
    return {"status": "cleared"}


@router.get("/api/admin/logs/stats")
async def get_logs_stats(api_key: str = Depends(verify_api_key)):
    """Get log statistics."""
    return logger.get_log_stats()


@router.get("/api/admin/jobs")
async def get_jobs(api_key: str = Depends(verify_api_key)):
    """Get all current job statuses."""
    return {"jobs": dict(job_progress)}


@router.post("/api/admin/jobs/{job_id}/cancel")
async def cancel_job_endpoint(job_id: str, api_key: str = Depends(verify_api_key)):
    """Cancel a specific job."""
    success = cancel_job(job_id)
    if success:
        return {"status": "cancelled", "job_id": job_id}
    raise HTTPException(status_code=404, detail=f"Job {job_id} not found")


@router.post("/api/admin/jobs/cancel-all")
async def cancel_all_jobs_endpoint(api_key: str = Depends(verify_api_key)):
    """Cancel all active jobs."""
    count = cancel_all_jobs()
    return {"status": "cancelled", "count": count}


@router.delete("/api/admin/jobs")
async def clear_jobs_endpoint(api_key: str = Depends(verify_api_key)):
    """Clear all completed/failed/cancelled jobs from tracking."""
    count = clear_completed_jobs()
    return {"status": "cleared", "count": count}


@router.get("/api/admin/system")
async def get_system_info(api_key: str = Depends(verify_api_key)):
    """Get system information."""
    import shutil

    # Disk usage
    total, used, free = shutil.disk_usage("/")

    # Memory (if available)
    try:
        with open("/proc/meminfo") as f:
            meminfo = f.read()
        mem_total = int([x for x in meminfo.split('\n') if 'MemTotal' in x][0].split()[1]) // 1024
        mem_available = int([x for x in meminfo.split('\n') if 'MemAvailable' in x][0].split()[1]) // 1024
    except:
        mem_total = 0
        mem_available = 0

    # Temp directory size
    temp_size = 0
    try:
        for dirpath, dirnames, filenames in os.walk(settings.TEMP_DIR):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                temp_size += os.path.getsize(fp)
    except:
        pass

    # yt-dlp version
    try:
        result = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True)
        ytdlp_version = result.stdout.strip()
    except:
        ytdlp_version = "unknown"

    return {
        "disk": {
            "total_gb": round(total / (1024**3), 2),
            "used_gb": round(used / (1024**3), 2),
            "free_gb": round(free / (1024**3), 2),
            "percent_used": round((used / total) * 100, 1)
        },
        "memory": {
            "total_mb": mem_total,
            "available_mb": mem_available,
            "percent_used": round(((mem_total - mem_available) / mem_total) * 100, 1) if mem_total > 0 else 0
        },
        "temp_dir": {
            "path": settings.TEMP_DIR,
            "size_mb": round(temp_size / (1024**2), 2)
        },
        "ytdlp_version": ytdlp_version,
        "environment": settings.ENVIRONMENT
    }


@router.get("/api/admin/download-stats")
async def get_download_stats(api_key: str = Depends(verify_api_key)):
    """
    Get download statistics and cost tracking.

    Returns:
        - Active job count
        - Cost per download
    """
    active_jobs = len([j for j in job_progress.values() if j.get("status") in ("pending", "downloading", "uploading")])
    completed_jobs = len([j for j in job_progress.values() if j.get("status") == "completed"])
    failed_jobs = len([j for j in job_progress.values() if j.get("status") == "failed"])

    return {
        "jobs": {
            "active": active_jobs,
            "completed": completed_jobs,
            "failed": failed_jobs,
            "total": len(job_progress),
        },
        "costs": {
            "cost_per_download_usd": COST_PER_DOWNLOAD,
            "estimated_total_usd": round(completed_jobs * COST_PER_DOWNLOAD, 4),
        },
    }




# WebSocket connections for real-time updates
connected_clients: list[WebSocket] = []


@router.websocket("/ws/admin")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time updates."""
    await websocket.accept()
    connected_clients.append(websocket)
    logger.info(f"WebSocket client connected. Total clients: {len(connected_clients)}", "admin")

    # Track last sequence sent to this client
    last_seq = 0

    try:
        while True:
            # Check for pending logs more frequently (100ms)
            await asyncio.sleep(0.1)

            # Get new logs since last sequence
            new_logs = []
            while _pending_logs:
                try:
                    log_entry = _pending_logs.popleft()
                    if log_entry.get("seq", 0) > last_seq:
                        new_logs.append(log_entry)
                        last_seq = log_entry.get("seq", last_seq)
                except IndexError:
                    break

            # Also check buffer for any missed logs
            if not new_logs:
                buffer_logs = logger.get_logs(limit=100, since_seq=last_seq)
                if buffer_logs:
                    new_logs = buffer_logs
                    if buffer_logs:
                        last_seq = max(l.get("seq", 0) for l in buffer_logs)

            # Send update if we have new logs or periodically for job status
            if new_logs:
                data = {
                    "type": "logs",
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                    "logs": new_logs,
                    "jobs": dict(job_progress),
                }
                await websocket.send_json(data)
            else:
                # Send job status every second even without new logs
                # Use a counter to only send every 10 iterations (1 second)
                pass

    except WebSocketDisconnect:
        connected_clients.remove(websocket)
        logger.info(f"WebSocket client disconnected. Total clients: {len(connected_clients)}", "admin")
    except Exception as e:
        if websocket in connected_clients:
            connected_clients.remove(websocket)


async def broadcast_log(level: str, message: str, category: str = "admin"):
    """Broadcast a log message to all connected clients."""
    # Log to server-side storage
    logger.log(level, message, category)

    for client in connected_clients:
        try:
            await client.send_json({
                "type": "log",
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "level": level,
                "category": category,
                "message": message
            })
        except:
            pass


ADMIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SafePlay YT-DLP Admin</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/lucide@latest"></script>
    <style>
        .status-ok { color: #22c55e; }
        .status-error { color: #ef4444; }
        .status-warn { color: #f59e0b; }
        .log-entry { font-family: 'Monaco', 'Menlo', monospace; font-size: 11px; }
        .log-INFO { color: #60a5fa; }
        .log-WARN { color: #fbbf24; }
        .log-ERROR { color: #f87171; }
        .log-SUCCESS { color: #4ade80; }
        .log-DEBUG { color: #a78bfa; }
        .cat-ytdlp { background: #1e40af22; border-left: 2px solid #3b82f6; }
        .cat-proxy { background: #065f4622; border-left: 2px solid #10b981; }
        .cat-storage { background: #7c2d1222; border-left: 2px solid #ef4444; }
        .cat-download { background: #78350f22; border-left: 2px solid #f59e0b; }
        .cat-admin { background: #3f3f4622; border-left: 2px solid #9ca3af; }
        .pulse { animation: pulse 2s infinite; }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        .live-indicator {
            animation: blink 1s infinite;
        }
        @keyframes blink {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.3; }
        }
        #logs-container {
            height: 300px;
            overflow-y: auto;
            scroll-behavior: smooth;
        }
        #logs-container::-webkit-scrollbar {
            width: 8px;
        }
        #logs-container::-webkit-scrollbar-track {
            background: #1f2937;
        }
        #logs-container::-webkit-scrollbar-thumb {
            background: #4b5563;
            border-radius: 4px;
        }
    </style>
</head>
<body class="bg-gray-900 text-gray-100 min-h-screen">
    <div class="container mx-auto px-4 py-6 max-w-7xl">
        <!-- Header -->
        <div class="flex items-center justify-between mb-6">
            <div>
                <h1 class="text-2xl font-bold text-white flex items-center gap-2">
                    <i data-lucide="play-circle" class="w-8 h-8 text-blue-500"></i>
                    SafePlay YT-DLP Admin
                </h1>
                <p class="text-gray-400 text-sm">Real-time monitoring and control</p>
            </div>
            <div class="flex items-center gap-4">
                <div id="ws-status" class="flex items-center gap-2">
                    <span class="w-2 h-2 bg-gray-500 rounded-full"></span>
                    <span class="text-sm text-gray-400">Connecting...</span>
                </div>
                <div class="text-sm text-gray-500" id="server-time">--</div>
            </div>
        </div>

        <!-- API Key Input (collapsible) -->
        <div class="mb-4">
            <button onclick="toggleApiKey()" class="text-sm text-gray-400 hover:text-white flex items-center gap-1">
                <i data-lucide="key" class="w-4 h-4"></i>
                <span id="api-key-toggle-text">Show API Key</span>
            </button>
            <div id="api-key-section" class="hidden mt-2">
                <input type="password" id="api-key"
                       class="w-full md:w-96 bg-gray-800 border border-gray-600 rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-blue-500"
                       placeholder="Enter your API key">
            </div>
        </div>

        <!-- Health Status Cards -->
        <div class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3 mb-6">
            <div class="bg-gray-800 rounded-lg p-3 border border-gray-700">
                <div class="text-gray-400 text-xs mb-1">Status</div>
                <div id="service-status" class="text-lg font-bold status-ok">--</div>
            </div>
            <div class="bg-gray-800 rounded-lg p-3 border border-gray-700">
                <div class="text-gray-400 text-xs mb-1">YT-DLP</div>
                <div id="ytdlp-status" class="text-lg font-bold">--</div>
            </div>
            <div class="bg-gray-800 rounded-lg p-3 border border-gray-700">
                <div class="text-gray-400 text-xs mb-1">Supabase</div>
                <div id="supabase-status" class="text-lg font-bold">--</div>
            </div>
            <div class="bg-gray-800 rounded-lg p-3 border border-gray-700">
                <div class="text-gray-400 text-xs mb-1">Proxy</div>
                <div id="proxy-status" class="text-lg font-bold">--</div>
            </div>
            <div class="bg-gray-800 rounded-lg p-3 border border-gray-700">
                <div class="text-gray-400 text-xs mb-1">Disk Free</div>
                <div id="disk-free" class="text-lg font-bold">--</div>
            </div>
            <div class="bg-gray-800 rounded-lg p-3 border border-gray-700">
                <div class="text-gray-400 text-xs mb-1">Memory</div>
                <div id="memory-used" class="text-lg font-bold">--</div>
            </div>
        </div>

        <!-- Main Grid -->
        <div class="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-6">
            <!-- Download Test Panel -->
            <div class="bg-gray-800 rounded-lg border border-gray-700">
                <div class="p-3 border-b border-gray-700">
                    <h2 class="font-semibold flex items-center gap-2">
                        <i data-lucide="download" class="w-4 h-4"></i>
                        Test Download
                    </h2>
                </div>
                <div class="p-3">
                    <div class="space-y-3">
                        <div>
                            <label class="block text-xs text-gray-400 mb-1">YouTube URL or Video ID</label>
                            <input type="text" id="youtube-input"
                                   class="w-full bg-gray-700 border border-gray-600 rounded px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                                   placeholder="e.g., dQw4w9WgXcQ">
                        </div>
                        <button onclick="startDownload()"
                                id="download-btn"
                                class="w-full bg-blue-600 hover:bg-blue-700 disabled:bg-gray-600 text-white font-medium py-2 px-4 rounded text-sm flex items-center justify-center gap-2 transition">
                            <i data-lucide="download" class="w-4 h-4"></i>
                            Start Download
                        </button>
                    </div>

                    <!-- Progress -->
                    <div id="progress-section" class="mt-4 hidden">
                        <div class="flex items-center justify-between mb-1">
                            <span class="text-xs text-gray-400" id="progress-status">Starting...</span>
                            <span id="progress-percent" class="text-xs font-mono">0%</span>
                        </div>
                        <div class="w-full bg-gray-700 rounded-full h-1.5">
                            <div id="progress-bar" class="bg-blue-500 h-1.5 rounded-full transition-all duration-300" style="width: 0%"></div>
                        </div>
                    </div>

                    <!-- Result -->
                    <div id="result-section" class="mt-4 hidden">
                        <div class="bg-green-900/30 border border-green-700 rounded p-3">
                            <div id="result-details" class="text-xs space-y-1 text-green-300"></div>
                        </div>
                    </div>

                    <!-- Error -->
                    <div id="error-section" class="mt-4 hidden">
                        <div class="bg-red-900/30 border border-red-700 rounded p-3">
                            <div id="error-details" class="text-xs text-red-300"></div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Active Jobs -->
            <div class="bg-gray-800 rounded-lg border border-gray-700">
                <div class="p-3 border-b border-gray-700 flex items-center justify-between">
                    <h2 class="font-semibold flex items-center gap-2">
                        <i data-lucide="activity" class="w-4 h-4"></i>
                        Active Jobs
                        <span id="active-jobs-count" class="text-xs bg-blue-600 px-2 py-0.5 rounded-full">0</span>
                    </h2>
                    <div class="flex items-center gap-2">
                        <button onclick="killAllJobs()" class="text-xs bg-red-600 hover:bg-red-700 text-white px-2 py-1 rounded flex items-center gap-1">
                            <i data-lucide="x-circle" class="w-3 h-3"></i>
                            Kill All
                        </button>
                        <span class="w-2 h-2 bg-green-500 rounded-full live-indicator"></span>
                    </div>
                </div>
                <div class="p-3">
                    <div id="active-jobs" class="space-y-2 max-h-64 overflow-y-auto">
                        <p class="text-gray-500 text-sm text-center py-4">No active jobs</p>
                    </div>
                </div>
            </div>

            <!-- Configuration -->
            <div class="bg-gray-800 rounded-lg border border-gray-700">
                <div class="p-3 border-b border-gray-700">
                    <h2 class="font-semibold flex items-center gap-2">
                        <i data-lucide="settings" class="w-4 h-4"></i>
                        YT-DLP Config
                    </h2>
                </div>
                <div class="p-3">
                    <div class="space-y-3">
                        <div class="grid grid-cols-2 gap-2">
                            <div>
                                <label class="block text-xs text-gray-400 mb-1">Max Height</label>
                                <select id="config-height" class="w-full bg-gray-700 border border-gray-600 rounded px-2 py-1.5 text-sm text-white">
                                    <option value="360">360p</option>
                                    <option value="480">480p</option>
                                    <option value="720" selected>720p</option>
                                    <option value="1080">1080p</option>
                                </select>
                            </div>
                            <div>
                                <label class="block text-xs text-gray-400 mb-1">Format</label>
                                <select id="config-format" class="w-full bg-gray-700 border border-gray-600 rounded px-2 py-1.5 text-sm text-white">
                                    <option value="mp4" selected>MP4</option>
                                    <option value="webm">WebM</option>
                                    <option value="any">Any</option>
                                </select>
                            </div>
                        </div>
                        <div class="grid grid-cols-2 gap-2">
                            <div>
                                <label class="block text-xs text-gray-400 mb-1">Retries</label>
                                <input type="number" id="config-retries" value="3" min="1" max="10"
                                       class="w-full bg-gray-700 border border-gray-600 rounded px-2 py-1.5 text-sm text-white">
                            </div>
                            <div>
                                <label class="block text-xs text-gray-400 mb-1">Concurrent Frags</label>
                                <input type="number" id="config-fragments" value="1" min="1" max="10"
                                       class="w-full bg-gray-700 border border-gray-600 rounded px-2 py-1.5 text-sm text-white">
                            </div>
                        </div>
                        <div>
                            <label class="block text-xs text-gray-400 mb-1">Rate Limit (e.g., 50M)</label>
                            <input type="text" id="config-rate" placeholder="No limit"
                                   class="w-full bg-gray-700 border border-gray-600 rounded px-2 py-1.5 text-sm text-white">
                        </div>
                        <div class="flex items-center gap-4">
                            <label class="flex items-center gap-2 text-sm">
                                <input type="checkbox" id="config-geo" checked class="rounded">
                                <span class="text-gray-300">Geo Bypass</span>
                            </label>
                        </div>
                        <button onclick="saveConfig()"
                                class="w-full bg-gray-700 hover:bg-gray-600 text-white font-medium py-2 px-4 rounded text-sm flex items-center justify-center gap-2 transition">
                            <i data-lucide="save" class="w-4 h-4"></i>
                            Save Configuration
                        </button>
                    </div>
                </div>
            </div>
        </div>

        <!-- Logs Section -->
        <div class="bg-gray-800 rounded-lg border border-gray-700 mb-6">
            <div class="p-3 border-b border-gray-700 flex items-center justify-between flex-wrap gap-2">
                <h2 class="font-semibold flex items-center gap-2">
                    <i data-lucide="terminal" class="w-4 h-4"></i>
                    System Logs
                    <span class="w-2 h-2 bg-green-500 rounded-full live-indicator"></span>
                    <span class="text-xs text-gray-400 font-normal">(server-side persistent)</span>
                </h2>
                <div class="flex items-center gap-3">
                    <select id="log-category-filter" class="bg-gray-700 border border-gray-600 rounded px-2 py-1 text-xs text-white">
                        <option value="">All Categories</option>
                        <option value="ytdlp">yt-dlp</option>
                        <option value="proxy">proxy</option>
                        <option value="storage">storage</option>
                        <option value="download">download</option>
                        <option value="admin">admin</option>
                        <option value="general">general</option>
                    </select>
                    <select id="log-level-filter" class="bg-gray-700 border border-gray-600 rounded px-2 py-1 text-xs text-white">
                        <option value="">All Levels</option>
                        <option value="DEBUG">DEBUG</option>
                        <option value="INFO">INFO</option>
                        <option value="WARN">WARN</option>
                        <option value="ERROR">ERROR</option>
                        <option value="SUCCESS">SUCCESS</option>
                    </select>
                    <label class="flex items-center gap-2 text-sm text-gray-400">
                        <input type="checkbox" id="auto-scroll" checked class="rounded">
                        Auto-scroll
                    </label>
                    <button onclick="refreshLogs()" class="text-xs text-gray-400 hover:text-white px-2 py-1 bg-gray-700 rounded">
                        <i data-lucide="refresh-cw" class="w-3 h-3 inline"></i> Refresh
                    </button>
                    <button onclick="clearServerLogs()" class="text-xs text-red-400 hover:text-red-300 px-2 py-1 bg-gray-700 rounded">Clear All</button>
                </div>
            </div>
            <div id="logs-container" class="p-3 bg-gray-900">
                <div id="logs-content" class="space-y-0.5">
                    <p class="text-gray-500 text-sm text-center py-4">Loading logs from server...</p>
                </div>
            </div>
            <div class="p-2 border-t border-gray-700 flex items-center justify-between text-xs text-gray-500">
                <span id="log-count">0 logs</span>
                <span id="log-storage-info">Logs stored at: /var/log/safeplay/service.jsonl</span>
            </div>
        </div>

        <!-- Recent Jobs History -->
        <div class="bg-gray-800 rounded-lg border border-gray-700">
            <div class="p-3 border-b border-gray-700 flex items-center justify-between">
                <h2 class="font-semibold flex items-center gap-2">
                    <i data-lucide="history" class="w-4 h-4"></i>
                    Job History
                </h2>
                <button onclick="clearHistory()" class="text-xs text-gray-400 hover:text-white">Clear</button>
            </div>
            <div class="p-3">
                <div id="jobs-history" class="space-y-2 max-h-64 overflow-y-auto">
                    <p class="text-gray-500 text-sm text-center py-4">No completed jobs</p>
                </div>
            </div>
        </div>

        <!-- Stats Footer -->
        <div class="mt-4 grid grid-cols-4 gap-3 text-center">
            <div class="bg-gray-800 rounded p-3">
                <div id="stat-total" class="text-2xl font-bold text-blue-400">0</div>
                <div class="text-xs text-gray-400">Total</div>
            </div>
            <div class="bg-gray-800 rounded p-3">
                <div id="stat-success" class="text-2xl font-bold text-green-400">0</div>
                <div class="text-xs text-gray-400">Success</div>
            </div>
            <div class="bg-gray-800 rounded p-3">
                <div id="stat-failed" class="text-2xl font-bold text-red-400">0</div>
                <div class="text-xs text-gray-400">Failed</div>
            </div>
            <div class="bg-gray-800 rounded p-3">
                <div id="stat-bytes" class="text-2xl font-bold text-purple-400">0 MB</div>
                <div class="text-xs text-gray-400">Downloaded</div>
            </div>
        </div>
    </div>

    <script>
        // Initialize Lucide icons
        lucide.createIcons();

        // State
        let ws = null;
        let reconnectAttempts = 0;
        let serverLogs = [];  // Logs from server
        let lastSeq = 0;  // Track last sequence number for incremental updates
        let seenSeqs = new Set();  // Track seen sequence numbers
        let jobHistory = JSON.parse(localStorage.getItem('safeplay_history') || '[]');
        let stats = JSON.parse(localStorage.getItem('safeplay_stats') || '{"total":0,"success":0,"failed":0,"bytes":0}');
        let currentJobId = null;

        // Load saved API key
        document.getElementById('api-key').value = localStorage.getItem('safeplay_api_key') || '';

        // Fetch logs from server
        async function fetchServerLogs() {
            const apiKey = document.getElementById('api-key').value;
            if (!apiKey) return;

            const category = document.getElementById('log-category-filter').value;
            const level = document.getElementById('log-level-filter').value;

            let url = `/api/admin/logs?limit=200`;
            if (category) url += `&category=${category}`;
            if (level) url += `&level=${level}`;

            try {
                const response = await fetch(url, {
                    headers: { 'X-API-Key': apiKey }
                });
                if (response.ok) {
                    const data = await response.json();
                    renderServerLogs(data.logs);
                }
            } catch (e) {
                console.error('Failed to fetch logs:', e);
            }
        }

        function renderServerLogs(logs) {
            const container = document.getElementById('logs-content');
            serverLogs = logs;
            seenSeqs.clear();
            lastSeq = 0;

            if (logs.length === 0) {
                container.innerHTML = '<p class="text-gray-500 text-sm text-center py-4">No logs found. Start a download to see activity.</p>';
                document.getElementById('log-count').textContent = '0 logs';
                return;
            }

            container.innerHTML = '';
            logs.forEach(log => {
                const seq = log.seq || 0;
                seenSeqs.add(seq);
                if (seq > lastSeq) lastSeq = seq;
                appendLogEntry(log);
            });

            document.getElementById('log-count').textContent = `${logs.length} logs`;

            // Scroll to bottom
            if (document.getElementById('auto-scroll').checked) {
                const logsContainer = document.getElementById('logs-container');
                logsContainer.scrollTop = logsContainer.scrollHeight;
            }
        }

        async function refreshLogs() {
            await fetchServerLogs();
        }

        async function clearServerLogs() {
            const apiKey = document.getElementById('api-key').value;
            if (!apiKey) { alert('Please enter your API key'); return; }

            if (!confirm('Clear all server logs? This cannot be undone.')) return;

            try {
                const response = await fetch('/api/admin/logs', {
                    method: 'DELETE',
                    headers: { 'X-API-Key': apiKey }
                });
                if (response.ok) {
                    serverLogs = [];
                    seenSeqs.clear();
                    lastSeq = 0;
                    document.getElementById('logs-content').innerHTML = '<p class="text-gray-500 text-sm text-center py-4">Logs cleared</p>';
                    document.getElementById('log-count').textContent = '0 logs';
                }
            } catch (e) {
                alert('Failed to clear logs: ' + e.message);
            }
        }

        // Filter change handlers
        document.getElementById('log-category-filter').addEventListener('change', fetchServerLogs);
        document.getElementById('log-level-filter').addEventListener('change', fetchServerLogs);
        document.getElementById('api-key').addEventListener('change', (e) => {
            localStorage.setItem('safeplay_api_key', e.target.value);
        });

        function toggleApiKey() {
            const section = document.getElementById('api-key-section');
            const text = document.getElementById('api-key-toggle-text');
            section.classList.toggle('hidden');
            text.textContent = section.classList.contains('hidden') ? 'Show API Key' : 'Hide API Key';
        }

        // WebSocket connection
        function connectWebSocket() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            ws = new WebSocket(`${protocol}//${window.location.host}/ws/admin`);

            ws.onopen = () => {
                updateWsStatus(true);
                reconnectAttempts = 0;
                addLocalLog('INFO', 'Connected to real-time updates');
            };

            ws.onclose = () => {
                updateWsStatus(false);
                // Reconnect with exponential backoff
                const delay = Math.min(1000 * Math.pow(2, reconnectAttempts), 30000);
                reconnectAttempts++;
                setTimeout(connectWebSocket, delay);
            };

            ws.onerror = () => {
                updateWsStatus(false);
            };

            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                handleWsMessage(data);
            };
        }

        function updateWsStatus(connected) {
            const el = document.getElementById('ws-status');
            if (connected) {
                el.innerHTML = '<span class="w-2 h-2 bg-green-500 rounded-full live-indicator"></span><span class="text-sm text-green-400">Live</span>';
            } else {
                el.innerHTML = '<span class="w-2 h-2 bg-red-500 rounded-full"></span><span class="text-sm text-red-400">Disconnected</span>';
            }
        }

        function handleWsMessage(data) {
            if (data.type === 'update' || data.type === 'logs') {
                // Update active jobs
                if (data.jobs) {
                    updateActiveJobs(data.jobs);
                }

                // Update logs from server (only show new ones based on sequence)
                if (data.logs && data.logs.length > 0) {
                    const category = document.getElementById('log-category-filter').value;
                    const level = document.getElementById('log-level-filter').value;

                    data.logs.forEach(log => {
                        const seq = log.seq || 0;

                        // Skip if already seen
                        if (seenSeqs.has(seq)) return;

                        // Apply filters
                        if (category && log.category !== category) return;
                        if (level && log.level !== level) return;

                        seenSeqs.add(seq);
                        if (seq > lastSeq) lastSeq = seq;
                        serverLogs.push(log);
                        appendLogEntry(log);
                    });

                    document.getElementById('log-count').textContent = `${serverLogs.length} logs`;
                }

                if (data.timestamp) {
                    document.getElementById('server-time').textContent = new Date(data.timestamp).toLocaleTimeString();
                }
            } else if (data.type === 'log') {
                const seq = data.seq || 0;
                if (!seenSeqs.has(seq)) {
                    seenSeqs.add(seq);
                    if (seq > lastSeq) lastSeq = seq;
                    serverLogs.push(data);
                    appendLogEntry(data);
                    document.getElementById('log-count').textContent = `${serverLogs.length} logs`;
                }
            }
        }

        function updateActiveJobs(jobs) {
            const container = document.getElementById('active-jobs');
            const count = Object.keys(jobs).length;
            document.getElementById('active-jobs-count').textContent = count;

            if (count === 0) {
                container.innerHTML = '<p class="text-gray-500 text-sm text-center py-4">No active jobs</p>';
                return;
            }

            const activeStatuses = ['pending', 'downloading', 'uploading', 'cancelling'];
            container.innerHTML = Object.entries(jobs).map(([jobId, job]) => `
                <div class="bg-gray-700 rounded p-2">
                    <div class="flex items-center justify-between">
                        <span class="font-mono text-xs truncate max-w-24">${jobId}</span>
                        <div class="flex items-center gap-2">
                            <span class="text-xs px-2 py-0.5 rounded ${getStatusClass(job.status)}">${job.status}</span>
                            ${activeStatuses.includes(job.status) ? `<button onclick="killJob('${jobId}')" class="text-xs text-red-400 hover:text-red-300 px-1" title="Kill this job"><i data-lucide="x" class="w-3 h-3"></i></button>` : ''}
                        </div>
                    </div>
                    <div class="mt-1">
                        <div class="w-full bg-gray-600 rounded-full h-1">
                            <div class="bg-blue-500 h-1 rounded-full transition-all" style="width: ${job.progress}%"></div>
                        </div>
                        <span class="text-xs text-gray-400">${job.progress}%</span>
                    </div>
                </div>
            `).join('');

            // Re-init icons for dynamically added elements
            lucide.createIcons();

            // Update current job progress if matching
            if (currentJobId && jobs[currentJobId]) {
                const job = jobs[currentJobId];
                updateProgress(job.progress, job.status);
            }
        }

        async function killJob(jobId) {
            const apiKey = document.getElementById('api-key').value;
            if (!apiKey) { alert('Please enter your API key'); return; }

            try {
                const response = await fetch(`/api/admin/jobs/${jobId}/cancel`, {
                    method: 'POST',
                    headers: { 'X-API-Key': apiKey }
                });
                if (response.ok) {
                    addLocalLog('WARN', `Cancelled job: ${jobId}`);
                } else {
                    const data = await response.json();
                    addLocalLog('ERROR', `Failed to cancel job: ${data.detail || 'Unknown error'}`);
                }
            } catch (e) {
                addLocalLog('ERROR', `Failed to cancel job: ${e.message}`);
            }
        }

        async function killAllJobs() {
            const apiKey = document.getElementById('api-key').value;
            if (!apiKey) { alert('Please enter your API key'); return; }

            if (!confirm('Cancel all active downloads?')) return;

            try {
                const response = await fetch('/api/admin/jobs/cancel-all', {
                    method: 'POST',
                    headers: { 'X-API-Key': apiKey }
                });
                if (response.ok) {
                    const data = await response.json();
                    addLocalLog('WARN', `Cancelled ${data.count} active jobs`);
                } else {
                    addLocalLog('ERROR', 'Failed to cancel jobs');
                }
            } catch (e) {
                addLocalLog('ERROR', `Failed to cancel jobs: ${e.message}`);
            }
        }

        function appendLogEntry(log) {
            const container = document.getElementById('logs-content');

            // Remove placeholder if present
            if (container.querySelector('p.text-gray-500')) {
                container.innerHTML = '';
            }

            const entry = document.createElement('div');
            const category = log.category || 'general';
            entry.className = `log-entry log-${log.level} cat-${category} pl-2 py-0.5`;
            const time = new Date(log.timestamp).toLocaleTimeString();
            const catBadge = `<span class="text-gray-600 text-[10px]">[${category}]</span>`;
            entry.innerHTML = `<span class="text-gray-500">${time}</span> ${catBadge} <span class="font-semibold">[${log.level}]</span> ${escapeHtml(log.message)}`;
            container.appendChild(entry);

            // Auto-scroll
            if (document.getElementById('auto-scroll').checked) {
                const logsContainer = document.getElementById('logs-container');
                logsContainer.scrollTop = logsContainer.scrollHeight;
            }

            // Limit displayed logs
            while (container.children.length > 300) {
                container.removeChild(container.firstChild);
            }
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        function addLocalLog(level, message, category = 'admin') {
            const log = {
                timestamp: new Date().toISOString(),
                level: level,
                category: category,
                message: message,
                seq: Date.now()  // Use timestamp as pseudo-sequence for local logs
            };
            if (!seenSeqs.has(log.seq)) {
                seenSeqs.add(log.seq);
                serverLogs.push(log);
                appendLogEntry(log);
            }
        }

        // Health check
        async function fetchHealth() {
            try {
                const response = await fetch('/health');
                const data = await response.json();

                document.getElementById('service-status').textContent = data.status.toUpperCase();
                document.getElementById('service-status').className = 'text-lg font-bold ' +
                    (data.status === 'ok' ? 'status-ok' : 'status-error');

                updateCheckStatus('ytdlp-status', data.checks.ytdlp, 'available');
                updateCheckStatus('supabase-status', data.checks.supabase, 'connected');
                updateCheckStatus('proxy-status', data.checks.proxy, ['configured', 'reachable']);
            } catch (error) {
                document.getElementById('service-status').textContent = 'OFFLINE';
                document.getElementById('service-status').className = 'text-lg font-bold status-error';
            }
        }

        async function fetchSystemInfo() {
            const apiKey = document.getElementById('api-key').value;
            if (!apiKey) return;

            try {
                const response = await fetch('/api/admin/system', {
                    headers: { 'X-API-Key': apiKey }
                });
                if (response.ok) {
                    const data = await response.json();
                    document.getElementById('disk-free').textContent = data.disk.free_gb + ' GB';
                    document.getElementById('disk-free').className = 'text-lg font-bold ' +
                        (data.disk.percent_used > 90 ? 'status-error' : 'status-ok');
                    document.getElementById('memory-used').textContent = data.memory.percent_used + '%';
                    document.getElementById('memory-used').className = 'text-lg font-bold ' +
                        (data.memory.percent_used > 90 ? 'status-error' : 'status-ok');
                }
            } catch (e) {}
        }

        function updateCheckStatus(elementId, value, goodValues) {
            const el = document.getElementById(elementId);
            const isGood = Array.isArray(goodValues) ? goodValues.includes(value) : value === goodValues;
            el.textContent = value ? value.toUpperCase() : 'UNKNOWN';
            el.className = 'text-lg font-bold ' + (isGood ? 'status-ok' : 'status-error');
        }

        // Download
        function extractVideoId(input) {
            input = input.trim();
            if (/^[a-zA-Z0-9_-]{11}$/.test(input)) return input;
            const patterns = [
                /(?:youtube\\.com\\/watch\\?v=|youtu\\.be\\/|youtube\\.com\\/embed\\/)([a-zA-Z0-9_-]{11})/,
                /youtube\\.com\\/shorts\\/([a-zA-Z0-9_-]{11})/
            ];
            for (const pattern of patterns) {
                const match = input.match(pattern);
                if (match) return match[1];
            }
            return input;
        }

        async function startDownload() {
            const apiKey = document.getElementById('api-key').value;
            const input = document.getElementById('youtube-input').value;

            if (!apiKey) { showError('Please enter your API key'); return; }
            if (!input) { showError('Please enter a YouTube URL or video ID'); return; }

            const videoId = extractVideoId(input);
            currentJobId = 'job-' + Date.now();

            hideResults();
            showProgress();
            updateProgress(0, 'Starting download...');
            document.getElementById('download-btn').disabled = true;

            addLocalLog('INFO', `Starting download: ${videoId} (${currentJobId})`);

            try {
                const response = await fetch('/api/download', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-API-Key': apiKey
                    },
                    body: JSON.stringify({
                        youtube_id: videoId,
                        job_id: currentJobId
                    })
                });

                const data = await response.json();

                if (response.ok && data.status === 'success') {
                    updateProgress(100, 'Complete!');
                    showSuccess(data);
                    addToHistory(currentJobId, videoId, 'success', data);
                    addLocalLog('SUCCESS', `Download complete: ${data.title} (${formatBytes(data.filesize_bytes)})`);
                    stats.success++;
                    stats.bytes += data.filesize_bytes || 0;
                } else {
                    throw new Error(data.detail?.message || data.message || 'Download failed');
                }
            } catch (error) {
                showError(error.message);
                addToHistory(currentJobId, videoId, 'failed', { error: error.message });
                addLocalLog('ERROR', `Download failed: ${error.message}`);
                stats.failed++;
            }

            stats.total++;
            saveStats();
            updateStatsDisplay();
            document.getElementById('download-btn').disabled = false;
            currentJobId = null;
        }

        function showProgress() {
            document.getElementById('progress-section').classList.remove('hidden');
        }

        function updateProgress(percent, status) {
            document.getElementById('progress-bar').style.width = percent + '%';
            document.getElementById('progress-percent').textContent = percent + '%';
            document.getElementById('progress-status').textContent = status;
        }

        function hideResults() {
            document.getElementById('result-section').classList.add('hidden');
            document.getElementById('error-section').classList.add('hidden');
        }

        function showSuccess(data) {
            document.getElementById('result-section').classList.remove('hidden');
            document.getElementById('result-details').innerHTML = `
                <p><strong>Title:</strong> ${data.title || 'Unknown'}</p>
                <p><strong>Duration:</strong> ${formatDuration(data.duration_seconds)}</p>
                <p><strong>Size:</strong> ${formatBytes(data.filesize_bytes)}</p>
                <p><strong>Path:</strong> ${data.storage_path}</p>
            `;
        }

        function showError(message) {
            document.getElementById('error-section').classList.remove('hidden');
            document.getElementById('error-details').textContent = message;
        }

        // Config
        async function saveConfig() {
            const apiKey = document.getElementById('api-key').value;
            if (!apiKey) { alert('Please enter your API key'); return; }

            const config = {
                max_height: parseInt(document.getElementById('config-height').value),
                format_preference: document.getElementById('config-format').value,
                retries: parseInt(document.getElementById('config-retries').value),
                concurrent_fragments: parseInt(document.getElementById('config-fragments').value),
                rate_limit: document.getElementById('config-rate').value || null,
                geo_bypass: document.getElementById('config-geo').checked
            };

            try {
                const response = await fetch('/api/admin/config', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-API-Key': apiKey
                    },
                    body: JSON.stringify(config)
                });

                if (response.ok) {
                    addLocalLog('SUCCESS', 'Configuration saved');
                } else {
                    throw new Error('Failed to save config');
                }
            } catch (error) {
                addLocalLog('ERROR', 'Failed to save configuration: ' + error.message);
            }
        }

        // History
        function addToHistory(jobId, videoId, status, data) {
            jobHistory.unshift({
                id: jobId,
                videoId: videoId,
                status: status,
                data: data,
                timestamp: new Date().toISOString()
            });
            if (jobHistory.length > 50) jobHistory = jobHistory.slice(0, 50);
            localStorage.setItem('safeplay_history', JSON.stringify(jobHistory));
            renderHistory();
        }

        function renderHistory() {
            const container = document.getElementById('jobs-history');
            if (jobHistory.length === 0) {
                container.innerHTML = '<p class="text-gray-500 text-sm text-center py-4">No completed jobs</p>';
                return;
            }
            container.innerHTML = jobHistory.map(job => `
                <div class="bg-gray-700 rounded p-2">
                    <div class="flex items-center justify-between">
                        <span class="font-mono text-xs">${job.videoId}</span>
                        <span class="text-xs px-2 py-0.5 rounded ${getStatusClass(job.status)}">${job.status}</span>
                    </div>
                    <div class="text-xs text-gray-400 mt-1">
                        ${new Date(job.timestamp).toLocaleString()}
                        ${job.data?.title ? ' • ' + job.data.title : ''}
                    </div>
                    ${job.data?.filesize_bytes ? '<div class="text-xs text-gray-500">' + formatBytes(job.data.filesize_bytes) + '</div>' : ''}
                    ${job.data?.error ? '<div class="text-xs text-red-400">' + job.data.error + '</div>' : ''}
                </div>
            `).join('');
        }

        function clearHistory() {
            jobHistory = [];
            stats = { total: 0, success: 0, failed: 0, bytes: 0 };
            localStorage.setItem('safeplay_history', '[]');
            localStorage.setItem('safeplay_stats', JSON.stringify(stats));
            renderHistory();
            updateStatsDisplay();
        }

        function getStatusClass(status) {
            switch(status) {
                case 'success': case 'completed': return 'bg-green-600';
                case 'failed': return 'bg-red-600';
                case 'downloading': case 'uploading': return 'bg-blue-600';
                case 'pending': return 'bg-yellow-600';
                default: return 'bg-gray-600';
            }
        }

        function saveStats() {
            localStorage.setItem('safeplay_stats', JSON.stringify(stats));
        }

        function updateStatsDisplay() {
            document.getElementById('stat-total').textContent = stats.total;
            document.getElementById('stat-success').textContent = stats.success;
            document.getElementById('stat-failed').textContent = stats.failed;
            document.getElementById('stat-bytes').textContent = formatBytes(stats.bytes);
        }

        function formatBytes(bytes) {
            if (!bytes) return '0 B';
            const k = 1024;
            const sizes = ['B', 'KB', 'MB', 'GB'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
        }

        function formatDuration(seconds) {
            if (!seconds) return '--';
            const mins = Math.floor(seconds / 60);
            const secs = seconds % 60;
            return `${mins}:${secs.toString().padStart(2, '0')}`;
        }

        // Initialize
        connectWebSocket();
        fetchHealth();
        fetchSystemInfo();
        fetchServerLogs();  // Load server-side logs on page load
        setInterval(fetchHealth, 10000);
        setInterval(fetchSystemInfo, 30000);
        renderHistory();
        updateStatsDisplay();

        // Re-initialize lucide icons for dynamically added elements
        setInterval(() => lucide.createIcons(), 2000);
    </script>
</body>
</html>
"""


@router.get("/admin", response_class=HTMLResponse)
async def admin_dashboard():
    """Serve the admin dashboard."""
    return HTMLResponse(content=ADMIN_HTML)
