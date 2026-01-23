"""
Benchmark Web UI Routes

Provides a web interface for testing YouTube download speeds
across different quality settings.
"""

import asyncio
import subprocess
import tempfile
import shutil
import time
import os
import re
import json
from pathlib import Path
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/benchmark", tags=["benchmark"])

# Quality presets
QUALITY_PRESETS = {
    "worst_audio": {
        "name": "Worst Audio",
        "format": "worstaudio",
        "description": "Lowest bitrate audio (production setting)",
        "icon": "volume-x",
    },
    "best_audio": {
        "name": "Best Audio",
        "format": "bestaudio",
        "description": "Highest bitrate audio",
        "icon": "volume-2",
    },
    "medium_audio": {
        "name": "Medium Audio",
        "format": "bestaudio[abr<=128]/worstaudio",
        "description": "Audio around 128kbps",
        "icon": "volume-1",
    },
    "opus_audio": {
        "name": "Opus Audio",
        "format": "bestaudio[acodec^=opus]/bestaudio",
        "description": "Best Opus codec (YouTube native)",
        "icon": "music",
    },
    "worst_video": {
        "name": "Worst Video",
        "format": "worstvideo+worstaudio/worst",
        "description": "Lowest quality video",
        "icon": "film",
    },
    "480p_video": {
        "name": "480p Video",
        "format": "bestvideo[height<=480]+bestaudio/best[height<=480]",
        "description": "SD quality video",
        "icon": "monitor",
    },
    "720p_video": {
        "name": "720p Video",
        "format": "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "description": "HD quality video",
        "icon": "tv",
    },
    "1080p_video": {
        "name": "1080p Video",
        "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        "description": "Full HD video",
        "icon": "tv-2",
    },
}

# Sample test videos
TEST_VIDEOS = {
    "dQw4w9WgXcQ": "Rick Astley - Never Gonna Give You Up (3:33)",
    "jNQXAC9IVRw": "Me at the zoo (0:19)",
    "9bZkp7q19f0": "PSY - Gangnam Style (4:13)",
    "kJQP7kiw5Fk": "Luis Fonsi - Despacito (4:42)",
}


def parse_speed(speed_str: str) -> float:
    """Parse yt-dlp speed string to bytes/sec"""
    speed_str = speed_str.strip()
    multipliers = {
        "B/s": 1,
        "KiB/s": 1024,
        "MiB/s": 1024 * 1024,
        "GiB/s": 1024 * 1024 * 1024,
        "KB/s": 1000,
        "MB/s": 1000 * 1000,
    }
    for unit, mult in multipliers.items():
        if unit in speed_str:
            try:
                num = float(speed_str.replace(unit, "").strip())
                return num * mult
            except ValueError:
                pass
    return 0


def format_speed(bytes_per_sec: float) -> str:
    """Format bytes/sec to human readable"""
    if bytes_per_sec >= 1024 * 1024:
        return f"{bytes_per_sec / (1024 * 1024):.2f} MiB/s"
    elif bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.2f} KiB/s"
    return f"{bytes_per_sec:.0f} B/s"


def format_size(bytes_val: int) -> str:
    """Format bytes to human readable"""
    if bytes_val >= 1024 * 1024:
        return f"{bytes_val / (1024 * 1024):.2f} MB"
    elif bytes_val >= 1024:
        return f"{bytes_val / 1024:.2f} KB"
    return f"{bytes_val} B"


@router.get("", response_class=HTMLResponse)
async def benchmark_ui():
    """Serve the benchmark web UI"""
    return HTML_TEMPLATE


@router.get("/presets")
async def get_presets():
    """Get available quality presets"""
    return {"presets": QUALITY_PRESETS, "videos": TEST_VIDEOS}


@router.websocket("/ws")
async def benchmark_websocket(websocket: WebSocket):
    """WebSocket endpoint for real-time benchmark updates"""
    await websocket.accept()

    try:
        while True:
            # Wait for benchmark request
            data = await websocket.receive_json()

            if data.get("action") == "run_benchmark":
                video_id = data.get("video_id", "").strip()
                format_string = data.get("format", "worstaudio")
                preset_name = data.get("preset_name", "Custom")

                # Validate video ID
                if not video_id:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Video ID is required"
                    })
                    continue

                # Extract video ID from URL if needed
                if "youtube.com" in video_id or "youtu.be" in video_id:
                    if "v=" in video_id:
                        video_id = video_id.split("v=")[1].split("&")[0]
                    elif "youtu.be/" in video_id:
                        video_id = video_id.split("youtu.be/")[1].split("?")[0]

                # Run benchmark
                await run_benchmark(websocket, video_id, format_string, preset_name)

            elif data.get("action") == "list_formats":
                video_id = data.get("video_id", "").strip()
                if video_id:
                    await list_formats(websocket, video_id)

    except WebSocketDisconnect:
        pass


async def run_benchmark(websocket: WebSocket, video_id: str, format_string: str, preset_name: str):
    """Run a benchmark and stream results via WebSocket"""

    temp_dir = tempfile.mkdtemp(prefix="yt_benchmark_")
    output_template = os.path.join(temp_dir, "%(id)s.%(ext)s")

    await websocket.send_json({
        "type": "start",
        "video_id": video_id,
        "format": format_string,
        "preset_name": preset_name,
        "timestamp": datetime.now().isoformat(),
    })

    cmd = [
        "yt-dlp",
        "--verbose",
        "--newline",
        "--no-warnings",
        "-f", format_string,
        "-o", output_template,
        "--no-playlist",
        "--print-to-file", "%(title)s", os.path.join(temp_dir, "title.txt"),
        f"https://www.youtube.com/watch?v={video_id}",
    ]

    start_time = time.time()
    speed_samples = []

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )

        async for line in process.stdout:
            line = line.decode().strip()
            if not line:
                continue

            # Parse progress
            if "[download]" in line:
                if "%" in line and "of" in line and "at" in line:
                    # Progress line
                    progress_match = re.search(r'(\d+\.?\d*)%', line)
                    speed_match = re.search(r'at\s+([\d.]+\s*\w+/s)', line)
                    eta_match = re.search(r'ETA\s+(\d+:\d+)', line)
                    size_match = re.search(r'of\s+~?([\d.]+\w+)', line)

                    progress = float(progress_match.group(1)) if progress_match else 0
                    speed_str = speed_match.group(1) if speed_match else ""
                    eta = eta_match.group(1) if eta_match else ""
                    size = size_match.group(1) if size_match else ""

                    speed_bps = parse_speed(speed_str)
                    if speed_bps > 0:
                        speed_samples.append(speed_bps)

                    await websocket.send_json({
                        "type": "progress",
                        "percent": progress,
                        "speed": speed_str,
                        "speed_bps": speed_bps,
                        "eta": eta,
                        "size": size,
                        "raw": line,
                    })

                elif "Destination" in line:
                    await websocket.send_json({
                        "type": "log",
                        "level": "info",
                        "message": line,
                    })

            elif "[info]" in line:
                await websocket.send_json({
                    "type": "log",
                    "level": "info",
                    "message": line,
                })

            elif "ERROR" in line or "error" in line.lower():
                await websocket.send_json({
                    "type": "log",
                    "level": "error",
                    "message": line,
                })

            elif "[ExtractAudio]" in line or "[Merger]" in line:
                await websocket.send_json({
                    "type": "log",
                    "level": "info",
                    "message": line,
                })

        return_code = await process.wait()
        end_time = time.time()
        duration = end_time - start_time

        if return_code == 0:
            # Find downloaded file
            files = list(Path(temp_dir).glob("*"))
            media_files = [f for f in files if f.suffix in ['.webm', '.m4a', '.mp4', '.mkv', '.opus', '.mp3', '.wav', '.ogg']]

            file_size = 0
            if media_files:
                file_size = media_files[0].stat().st_size

            # Get title
            title = video_id
            title_file = Path(temp_dir) / "title.txt"
            if title_file.exists():
                title = title_file.read_text().strip()

            # Calculate speeds
            avg_speed = sum(speed_samples) / len(speed_samples) if speed_samples else (file_size / duration if duration > 0 else 0)
            max_speed = max(speed_samples) if speed_samples else avg_speed
            min_speed = min(speed_samples) if speed_samples else avg_speed

            await websocket.send_json({
                "type": "complete",
                "success": True,
                "video_id": video_id,
                "title": title,
                "preset_name": preset_name,
                "format": format_string,
                "file_size": file_size,
                "file_size_formatted": format_size(file_size),
                "duration": round(duration, 2),
                "avg_speed": avg_speed,
                "avg_speed_formatted": format_speed(avg_speed),
                "avg_speed_mbps": round((avg_speed * 8) / (1024 * 1024), 2),
                "max_speed": max_speed,
                "max_speed_formatted": format_speed(max_speed),
                "min_speed": min_speed,
                "min_speed_formatted": format_speed(min_speed),
                "samples": len(speed_samples),
            })
        else:
            await websocket.send_json({
                "type": "complete",
                "success": False,
                "error": f"Download failed with exit code {return_code}",
            })

    except Exception as e:
        await websocket.send_json({
            "type": "complete",
            "success": False,
            "error": str(e),
        })
    finally:
        try:
            shutil.rmtree(temp_dir)
        except Exception:
            pass


async def list_formats(websocket: WebSocket, video_id: str):
    """List available formats for a video"""

    # Extract video ID from URL if needed
    if "youtube.com" in video_id or "youtu.be" in video_id:
        if "v=" in video_id:
            video_id = video_id.split("v=")[1].split("&")[0]
        elif "youtu.be/" in video_id:
            video_id = video_id.split("youtu.be/")[1].split("?")[0]

    await websocket.send_json({
        "type": "formats_start",
        "video_id": video_id,
    })

    cmd = [
        "yt-dlp",
        "-F",
        "--no-warnings",
        f"https://www.youtube.com/watch?v={video_id}",
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        lines = []
        async for line in process.stdout:
            line = line.decode().strip()
            if line:
                lines.append(line)
                await websocket.send_json({
                    "type": "format_line",
                    "line": line,
                })

        await process.wait()

        await websocket.send_json({
            "type": "formats_complete",
            "lines": lines,
        })

    except Exception as e:
        await websocket.send_json({
            "type": "formats_error",
            "error": str(e),
        })


# HTML Template with embedded CSS and JavaScript
HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>YouTube Download Benchmark</title>
    <style>
        :root {
            --bg-primary: #0f0f0f;
            --bg-secondary: #1a1a1a;
            --bg-tertiary: #252525;
            --text-primary: #ffffff;
            --text-secondary: #aaaaaa;
            --accent: #3b82f6;
            --accent-hover: #2563eb;
            --success: #22c55e;
            --warning: #f59e0b;
            --error: #ef4444;
            --border: #333333;
        }

        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            line-height: 1.5;
        }

        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 24px;
        }

        header {
            text-align: center;
            margin-bottom: 32px;
            padding-bottom: 24px;
            border-bottom: 1px solid var(--border);
        }

        header h1 {
            font-size: 2rem;
            margin-bottom: 8px;
            background: linear-gradient(135deg, var(--accent), #8b5cf6);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        header p {
            color: var(--text-secondary);
            font-size: 1rem;
        }

        .grid {
            display: grid;
            grid-template-columns: 350px 1fr;
            gap: 24px;
        }

        @media (max-width: 1024px) {
            .grid {
                grid-template-columns: 1fr;
            }
        }

        .panel {
            background: var(--bg-secondary);
            border-radius: 12px;
            padding: 20px;
            border: 1px solid var(--border);
        }

        .panel-title {
            font-size: 1.1rem;
            font-weight: 600;
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .form-group {
            margin-bottom: 16px;
        }

        label {
            display: block;
            font-size: 0.875rem;
            color: var(--text-secondary);
            margin-bottom: 6px;
        }

        input, select {
            width: 100%;
            padding: 10px 12px;
            background: var(--bg-tertiary);
            border: 1px solid var(--border);
            border-radius: 8px;
            color: var(--text-primary);
            font-size: 0.95rem;
            transition: border-color 0.2s;
        }

        input:focus, select:focus {
            outline: none;
            border-color: var(--accent);
        }

        .preset-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 8px;
            margin-bottom: 16px;
        }

        .preset-btn {
            padding: 12px 8px;
            background: var(--bg-tertiary);
            border: 1px solid var(--border);
            border-radius: 8px;
            color: var(--text-primary);
            cursor: pointer;
            transition: all 0.2s;
            text-align: center;
        }

        .preset-btn:hover {
            border-color: var(--accent);
            background: rgba(59, 130, 246, 0.1);
        }

        .preset-btn.active {
            border-color: var(--accent);
            background: rgba(59, 130, 246, 0.2);
        }

        .preset-btn .name {
            font-weight: 500;
            font-size: 0.85rem;
        }

        .preset-btn .desc {
            font-size: 0.7rem;
            color: var(--text-secondary);
            margin-top: 2px;
        }

        .btn {
            width: 100%;
            padding: 12px;
            background: var(--accent);
            border: none;
            border-radius: 8px;
            color: white;
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            transition: background 0.2s;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
        }

        .btn:hover:not(:disabled) {
            background: var(--accent-hover);
        }

        .btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }

        .btn-secondary {
            background: var(--bg-tertiary);
            border: 1px solid var(--border);
        }

        .btn-secondary:hover:not(:disabled) {
            background: var(--bg-primary);
        }

        .progress-section {
            margin-top: 20px;
            padding-top: 20px;
            border-top: 1px solid var(--border);
        }

        .progress-bar-container {
            background: var(--bg-tertiary);
            border-radius: 8px;
            height: 24px;
            overflow: hidden;
            margin-bottom: 12px;
        }

        .progress-bar {
            height: 100%;
            background: linear-gradient(90deg, var(--accent), #8b5cf6);
            transition: width 0.3s ease;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.75rem;
            font-weight: 600;
        }

        .progress-stats {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 12px;
            text-align: center;
        }

        .stat {
            background: var(--bg-tertiary);
            padding: 10px;
            border-radius: 8px;
        }

        .stat-value {
            font-size: 1.1rem;
            font-weight: 600;
            color: var(--accent);
        }

        .stat-label {
            font-size: 0.75rem;
            color: var(--text-secondary);
        }

        .log-container {
            background: var(--bg-primary);
            border-radius: 8px;
            padding: 12px;
            height: 150px;
            overflow-y: auto;
            font-family: 'Monaco', 'Consolas', monospace;
            font-size: 0.75rem;
            margin-top: 12px;
        }

        .log-line {
            padding: 2px 0;
            word-break: break-all;
        }

        .log-line.info { color: var(--text-secondary); }
        .log-line.error { color: var(--error); }
        .log-line.success { color: var(--success); }

        .results-table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 16px;
        }

        .results-table th,
        .results-table td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid var(--border);
        }

        .results-table th {
            background: var(--bg-tertiary);
            font-weight: 600;
            font-size: 0.85rem;
            color: var(--text-secondary);
        }

        .results-table tr:hover td {
            background: rgba(59, 130, 246, 0.05);
        }

        .speed-badge {
            display: inline-block;
            padding: 4px 8px;
            border-radius: 4px;
            font-weight: 600;
            font-size: 0.85rem;
        }

        .speed-fast { background: rgba(34, 197, 94, 0.2); color: var(--success); }
        .speed-medium { background: rgba(245, 158, 11, 0.2); color: var(--warning); }
        .speed-slow { background: rgba(239, 68, 68, 0.2); color: var(--error); }

        .status-success { color: var(--success); }
        .status-error { color: var(--error); }

        .empty-state {
            text-align: center;
            padding: 40px;
            color: var(--text-secondary);
        }

        .connection-status {
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 0.8rem;
            color: var(--text-secondary);
            margin-bottom: 16px;
        }

        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: var(--error);
        }

        .status-dot.connected {
            background: var(--success);
        }

        .format-modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.8);
            z-index: 1000;
            align-items: center;
            justify-content: center;
        }

        .format-modal.active {
            display: flex;
        }

        .format-modal-content {
            background: var(--bg-secondary);
            border-radius: 12px;
            padding: 24px;
            width: 90%;
            max-width: 800px;
            max-height: 80vh;
            overflow-y: auto;
        }

        .format-modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
        }

        .format-modal-close {
            background: none;
            border: none;
            color: var(--text-secondary);
            font-size: 1.5rem;
            cursor: pointer;
        }

        .format-output {
            background: var(--bg-primary);
            padding: 16px;
            border-radius: 8px;
            font-family: monospace;
            font-size: 0.8rem;
            white-space: pre-wrap;
            max-height: 400px;
            overflow-y: auto;
        }

        .format-line.audio { color: #22d3ee; }
        .format-line.video { color: #fbbf24; }
        .format-line.header { color: var(--text-primary); font-weight: bold; }

        .actions-row {
            display: flex;
            gap: 8px;
            margin-top: 12px;
        }

        .actions-row .btn {
            flex: 1;
        }

        .analysis-section {
            margin-top: 24px;
            padding: 20px;
            background: var(--bg-tertiary);
            border-radius: 12px;
        }

        .analysis-title {
            font-weight: 600;
            margin-bottom: 12px;
        }

        .analysis-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 16px;
        }

        .analysis-card {
            background: var(--bg-secondary);
            padding: 16px;
            border-radius: 8px;
            text-align: center;
        }

        .analysis-card .value {
            font-size: 1.5rem;
            font-weight: 700;
        }

        .analysis-card .label {
            font-size: 0.85rem;
            color: var(--text-secondary);
            margin-top: 4px;
        }

        .analysis-card.fastest .value { color: var(--success); }
        .analysis-card.slowest .value { color: var(--error); }
        .analysis-card.ratio .value { color: var(--warning); }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>YouTube Download Benchmark</h1>
            <p>Test the theory: Does YouTube serve different quality levels at different speeds?</p>
        </header>

        <div class="grid">
            <div class="sidebar">
                <div class="panel">
                    <div class="connection-status">
                        <div class="status-dot" id="statusDot"></div>
                        <span id="statusText">Connecting...</span>
                    </div>

                    <div class="panel-title">Test Configuration</div>

                    <div class="form-group">
                        <label>Video ID or URL</label>
                        <input type="text" id="videoInput" placeholder="Enter video ID or YouTube URL">
                    </div>

                    <div class="form-group">
                        <label>Quick Select</label>
                        <select id="videoSelect">
                            <option value="">-- Sample Videos --</option>
                            <option value="dQw4w9WgXcQ">Rick Astley - Never Gonna Give You Up (3:33)</option>
                            <option value="jNQXAC9IVRw">Me at the zoo (0:19)</option>
                            <option value="9bZkp7q19f0">PSY - Gangnam Style (4:13)</option>
                            <option value="kJQP7kiw5Fk">Luis Fonsi - Despacito (4:42)</option>
                        </select>
                    </div>

                    <div class="form-group">
                        <label>Quality Preset</label>
                        <div class="preset-grid" id="presetGrid">
                            <button class="preset-btn active" data-format="worstaudio" data-name="Worst Audio">
                                <div class="name">Worst Audio</div>
                                <div class="desc">Production setting</div>
                            </button>
                            <button class="preset-btn" data-format="bestaudio" data-name="Best Audio">
                                <div class="name">Best Audio</div>
                                <div class="desc">Highest bitrate</div>
                            </button>
                            <button class="preset-btn" data-format="bestaudio[abr<=128]/worstaudio" data-name="Medium Audio">
                                <div class="name">Medium Audio</div>
                                <div class="desc">~128kbps</div>
                            </button>
                            <button class="preset-btn" data-format="bestaudio[acodec^=opus]/bestaudio" data-name="Opus Audio">
                                <div class="name">Opus Audio</div>
                                <div class="desc">YouTube native</div>
                            </button>
                            <button class="preset-btn" data-format="worstvideo+worstaudio/worst" data-name="Worst Video">
                                <div class="name">Worst Video</div>
                                <div class="desc">Lowest quality</div>
                            </button>
                            <button class="preset-btn" data-format="bestvideo[height<=480]+bestaudio/best[height<=480]" data-name="480p Video">
                                <div class="name">480p Video</div>
                                <div class="desc">SD quality</div>
                            </button>
                            <button class="preset-btn" data-format="bestvideo[height<=720]+bestaudio/best[height<=720]" data-name="720p Video">
                                <div class="name">720p Video</div>
                                <div class="desc">HD quality</div>
                            </button>
                            <button class="preset-btn" data-format="bestvideo[height<=1080]+bestaudio/best[height<=1080]" data-name="1080p Video">
                                <div class="name">1080p Video</div>
                                <div class="desc">Full HD</div>
                            </button>
                        </div>
                    </div>

                    <div class="form-group">
                        <label>Custom Format (optional)</label>
                        <input type="text" id="customFormat" placeholder="e.g., bestaudio[ext=m4a]">
                    </div>

                    <button class="btn" id="runBtn">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <polygon points="5 3 19 12 5 21 5 3"></polygon>
                        </svg>
                        Run Benchmark
                    </button>

                    <div class="actions-row">
                        <button class="btn btn-secondary" id="listFormatsBtn">List Formats</button>
                        <button class="btn btn-secondary" id="clearResultsBtn">Clear Results</button>
                    </div>

                    <div class="progress-section" id="progressSection" style="display: none;">
                        <div class="progress-bar-container">
                            <div class="progress-bar" id="progressBar" style="width: 0%">0%</div>
                        </div>
                        <div class="progress-stats">
                            <div class="stat">
                                <div class="stat-value" id="currentSpeed">--</div>
                                <div class="stat-label">Speed</div>
                            </div>
                            <div class="stat">
                                <div class="stat-value" id="currentSize">--</div>
                                <div class="stat-label">Size</div>
                            </div>
                            <div class="stat">
                                <div class="stat-value" id="currentEta">--</div>
                                <div class="stat-label">ETA</div>
                            </div>
                        </div>
                        <div class="log-container" id="logContainer"></div>
                    </div>
                </div>
            </div>

            <div class="main-content">
                <div class="panel">
                    <div class="panel-title">Benchmark Results</div>

                    <div id="resultsEmpty" class="empty-state">
                        <p>No benchmark results yet.</p>
                        <p style="font-size: 0.85rem; margin-top: 8px;">Run a benchmark to compare download speeds across different quality settings.</p>
                    </div>

                    <table class="results-table" id="resultsTable" style="display: none;">
                        <thead>
                            <tr>
                                <th>Quality</th>
                                <th>Video</th>
                                <th>Size</th>
                                <th>Time</th>
                                <th>Avg Speed</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody id="resultsBody">
                        </tbody>
                    </table>

                    <div class="analysis-section" id="analysisSection" style="display: none;">
                        <div class="analysis-title">Speed Analysis</div>
                        <div class="analysis-grid">
                            <div class="analysis-card fastest">
                                <div class="value" id="fastestSpeed">--</div>
                                <div class="label" id="fastestLabel">Fastest</div>
                            </div>
                            <div class="analysis-card slowest">
                                <div class="value" id="slowestSpeed">--</div>
                                <div class="label" id="slowestLabel">Slowest</div>
                            </div>
                            <div class="analysis-card ratio">
                                <div class="value" id="speedRatio">--</div>
                                <div class="label">Speed Ratio</div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <div class="format-modal" id="formatModal">
        <div class="format-modal-content">
            <div class="format-modal-header">
                <h3>Available Formats</h3>
                <button class="format-modal-close" id="closeModal">&times;</button>
            </div>
            <div class="format-output" id="formatOutput">Loading...</div>
        </div>
    </div>

    <script>
        let ws = null;
        let selectedFormat = 'worstaudio';
        let selectedPresetName = 'Worst Audio';
        let results = [];
        let isRunning = false;

        function connect() {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            ws = new WebSocket(`${protocol}//${window.location.host}/benchmark/ws`);

            ws.onopen = () => {
                document.getElementById('statusDot').classList.add('connected');
                document.getElementById('statusText').textContent = 'Connected';
            };

            ws.onclose = () => {
                document.getElementById('statusDot').classList.remove('connected');
                document.getElementById('statusText').textContent = 'Disconnected - Reconnecting...';
                setTimeout(connect, 2000);
            };

            ws.onerror = () => {
                document.getElementById('statusDot').classList.remove('connected');
                document.getElementById('statusText').textContent = 'Connection error';
            };

            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                handleMessage(data);
            };
        }

        function handleMessage(data) {
            switch (data.type) {
                case 'start':
                    document.getElementById('progressSection').style.display = 'block';
                    document.getElementById('progressBar').style.width = '0%';
                    document.getElementById('progressBar').textContent = '0%';
                    document.getElementById('logContainer').innerHTML = '';
                    addLog('Starting download...', 'info');
                    break;

                case 'progress':
                    const pct = Math.round(data.percent);
                    document.getElementById('progressBar').style.width = pct + '%';
                    document.getElementById('progressBar').textContent = pct + '%';
                    document.getElementById('currentSpeed').textContent = data.speed || '--';
                    document.getElementById('currentSize').textContent = data.size || '--';
                    document.getElementById('currentEta').textContent = data.eta || '--';
                    break;

                case 'log':
                    addLog(data.message, data.level);
                    break;

                case 'complete':
                    isRunning = false;
                    document.getElementById('runBtn').disabled = false;

                    if (data.success) {
                        addLog(`Download complete! Avg speed: ${data.avg_speed_formatted}`, 'success');
                        addResult(data);
                    } else {
                        addLog(`Error: ${data.error}`, 'error');
                        addResult({
                            success: false,
                            preset_name: selectedPresetName,
                            error: data.error
                        });
                    }
                    updateAnalysis();
                    break;

                case 'formats_start':
                    document.getElementById('formatModal').classList.add('active');
                    document.getElementById('formatOutput').innerHTML = 'Loading formats...';
                    break;

                case 'format_line':
                    const output = document.getElementById('formatOutput');
                    if (output.textContent === 'Loading formats...') {
                        output.innerHTML = '';
                    }
                    const line = document.createElement('div');
                    line.className = 'format-line';
                    if (data.line.includes('audio only')) {
                        line.classList.add('audio');
                    } else if (data.line.includes('video only')) {
                        line.classList.add('video');
                    } else if (data.line.includes('ID') || data.line.includes('---')) {
                        line.classList.add('header');
                    }
                    line.textContent = data.line;
                    output.appendChild(line);
                    output.scrollTop = output.scrollHeight;
                    break;

                case 'formats_complete':
                    break;

                case 'formats_error':
                    document.getElementById('formatOutput').innerHTML = `<span style="color: var(--error)">Error: ${data.error}</span>`;
                    break;

                case 'error':
                    addLog(`Error: ${data.message}`, 'error');
                    isRunning = false;
                    document.getElementById('runBtn').disabled = false;
                    break;
            }
        }

        function addLog(message, level) {
            const container = document.getElementById('logContainer');
            const line = document.createElement('div');
            line.className = `log-line ${level}`;
            line.textContent = message;
            container.appendChild(line);
            container.scrollTop = container.scrollHeight;
        }

        function addResult(data) {
            results.push(data);
            updateResultsTable();
        }

        function updateResultsTable() {
            const table = document.getElementById('resultsTable');
            const empty = document.getElementById('resultsEmpty');
            const tbody = document.getElementById('resultsBody');

            if (results.length === 0) {
                table.style.display = 'none';
                empty.style.display = 'block';
                return;
            }

            table.style.display = 'table';
            empty.style.display = 'none';

            tbody.innerHTML = results.map((r, i) => {
                if (!r.success) {
                    return `<tr>
                        <td>${r.preset_name || 'Unknown'}</td>
                        <td colspan="4">--</td>
                        <td class="status-error">Failed</td>
                    </tr>`;
                }

                const speedClass = r.avg_speed_mbps > 10 ? 'speed-fast' :
                                   r.avg_speed_mbps > 3 ? 'speed-medium' : 'speed-slow';

                return `<tr>
                    <td><strong>${r.preset_name}</strong></td>
                    <td title="${r.title}">${(r.title || r.video_id).substring(0, 30)}${(r.title || '').length > 30 ? '...' : ''}</td>
                    <td>${r.file_size_formatted}</td>
                    <td>${r.duration}s</td>
                    <td><span class="speed-badge ${speedClass}">${r.avg_speed_mbps} Mbps</span></td>
                    <td class="status-success">OK</td>
                </tr>`;
            }).join('');
        }

        function updateAnalysis() {
            const section = document.getElementById('analysisSection');
            const successful = results.filter(r => r.success);

            if (successful.length < 2) {
                section.style.display = 'none';
                return;
            }

            section.style.display = 'block';

            const fastest = successful.reduce((a, b) => a.avg_speed_mbps > b.avg_speed_mbps ? a : b);
            const slowest = successful.reduce((a, b) => a.avg_speed_mbps < b.avg_speed_mbps ? a : b);
            const ratio = (fastest.avg_speed_mbps / slowest.avg_speed_mbps).toFixed(2);

            document.getElementById('fastestSpeed').textContent = fastest.avg_speed_mbps + ' Mbps';
            document.getElementById('fastestLabel').textContent = fastest.preset_name;
            document.getElementById('slowestSpeed').textContent = slowest.avg_speed_mbps + ' Mbps';
            document.getElementById('slowestLabel').textContent = slowest.preset_name;
            document.getElementById('speedRatio').textContent = ratio + 'x';
        }

        function getVideoId() {
            const input = document.getElementById('videoInput').value.trim();
            const select = document.getElementById('videoSelect').value;
            return input || select;
        }

        // Event listeners
        document.getElementById('runBtn').addEventListener('click', () => {
            const videoId = getVideoId();
            if (!videoId) {
                alert('Please enter a video ID or select a sample video');
                return;
            }

            const customFormat = document.getElementById('customFormat').value.trim();
            const format = customFormat || selectedFormat;
            const presetName = customFormat ? 'Custom' : selectedPresetName;

            isRunning = true;
            document.getElementById('runBtn').disabled = true;

            ws.send(JSON.stringify({
                action: 'run_benchmark',
                video_id: videoId,
                format: format,
                preset_name: presetName
            }));
        });

        document.getElementById('listFormatsBtn').addEventListener('click', () => {
            const videoId = getVideoId();
            if (!videoId) {
                alert('Please enter a video ID first');
                return;
            }

            ws.send(JSON.stringify({
                action: 'list_formats',
                video_id: videoId
            }));
        });

        document.getElementById('clearResultsBtn').addEventListener('click', () => {
            results = [];
            updateResultsTable();
            document.getElementById('analysisSection').style.display = 'none';
        });

        document.getElementById('closeModal').addEventListener('click', () => {
            document.getElementById('formatModal').classList.remove('active');
        });

        document.getElementById('formatModal').addEventListener('click', (e) => {
            if (e.target.id === 'formatModal') {
                document.getElementById('formatModal').classList.remove('active');
            }
        });

        document.getElementById('videoSelect').addEventListener('change', (e) => {
            if (e.target.value) {
                document.getElementById('videoInput').value = '';
            }
        });

        document.getElementById('videoInput').addEventListener('input', () => {
            document.getElementById('videoSelect').value = '';
        });

        document.querySelectorAll('.preset-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.preset-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                selectedFormat = btn.dataset.format;
                selectedPresetName = btn.dataset.name;
                document.getElementById('customFormat').value = '';
            });
        });

        // Connect on load
        connect();
    </script>
</body>
</html>
'''
