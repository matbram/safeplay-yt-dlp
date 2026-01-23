#!/usr/bin/env python3
"""
YouTube Download Quality Benchmark Tool

Tests the theory that YouTube serves different quality levels at different speeds.
Provides real-time download metrics with detailed logging.

Usage:
    python benchmark_quality.py

Interactive menu allows testing:
    - Worst audio (current production setting)
    - Best audio
    - Worst video
    - Medium video (480p)
    - Best video (up to 1080p)
    - Custom format string
"""

import subprocess
import sys
import time
import os
import tempfile
import shutil
import json
from datetime import datetime
from typing import Optional
from pathlib import Path

# ANSI color codes for terminal output
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    RESET = '\033[0m'

# Quality presets for testing
QUALITY_PRESETS = {
    "1": {
        "name": "Worst Audio (Production)",
        "format": "worstaudio",
        "description": "Lowest bitrate audio - current production setting",
    },
    "2": {
        "name": "Best Audio",
        "format": "bestaudio",
        "description": "Highest bitrate audio available",
    },
    "3": {
        "name": "Medium Audio (128k)",
        "format": "bestaudio[abr<=128]/worstaudio",
        "description": "Audio around 128kbps or lower",
    },
    "4": {
        "name": "Worst Video",
        "format": "worstvideo+worstaudio/worst",
        "description": "Lowest quality video with audio",
    },
    "5": {
        "name": "Medium Video (480p)",
        "format": "bestvideo[height<=480]+bestaudio/best[height<=480]",
        "description": "Video up to 480p with best audio",
    },
    "6": {
        "name": "Good Video (720p)",
        "format": "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "description": "Video up to 720p with best audio",
    },
    "7": {
        "name": "Best Video (1080p)",
        "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        "description": "Video up to 1080p with best audio",
    },
    "8": {
        "name": "Audio Only - Opus (Best)",
        "format": "bestaudio[acodec^=opus]/bestaudio",
        "description": "Best Opus audio codec (YouTube's native format)",
    },
    "9": {
        "name": "Audio Only - M4A (Best)",
        "format": "bestaudio[ext=m4a]/bestaudio",
        "description": "Best M4A/AAC audio",
    },
    "c": {
        "name": "Custom Format",
        "format": None,
        "description": "Enter your own yt-dlp format string",
    },
}

# Test video options
TEST_VIDEOS = {
    "1": {
        "id": "dQw4w9WgXcQ",
        "name": "Rick Astley - Never Gonna Give You Up (3:33)",
        "duration": "3:33",
    },
    "2": {
        "id": "jNQXAC9IVRw",
        "name": "Me at the zoo (first YouTube video, 0:19)",
        "duration": "0:19",
    },
    "3": {
        "id": "9bZkp7q19f0",
        "name": "PSY - Gangnam Style (4:13)",
        "duration": "4:13",
    },
    "4": {
        "id": "kJQP7kiw5Fk",
        "name": "Luis Fonsi - Despacito (4:42)",
        "duration": "4:42",
    },
    "c": {
        "id": None,
        "name": "Custom video ID/URL",
        "duration": "varies",
    },
}


class BenchmarkResult:
    """Stores results from a single benchmark run"""
    def __init__(self):
        self.start_time: float = 0
        self.end_time: float = 0
        self.format_string: str = ""
        self.format_name: str = ""
        self.video_id: str = ""
        self.video_title: str = ""
        self.file_size_bytes: int = 0
        self.download_speed_avg: float = 0  # bytes/sec
        self.download_speed_samples: list = []
        self.selected_format_id: str = ""
        self.selected_format_note: str = ""
        self.resolution: str = ""
        self.audio_bitrate: str = ""
        self.success: bool = False
        self.error_message: str = ""
        self.raw_output: str = ""

    @property
    def duration_seconds(self) -> float:
        return self.end_time - self.start_time

    @property
    def file_size_mb(self) -> float:
        return self.file_size_bytes / (1024 * 1024)

    @property
    def avg_speed_mbps(self) -> float:
        if self.download_speed_avg > 0:
            return (self.download_speed_avg * 8) / (1024 * 1024)  # Convert to Mbps
        return 0

    def to_dict(self) -> dict:
        return {
            "timestamp": datetime.now().isoformat(),
            "video_id": self.video_id,
            "video_title": self.video_title,
            "format_name": self.format_name,
            "format_string": self.format_string,
            "selected_format_id": self.selected_format_id,
            "resolution": self.resolution,
            "audio_bitrate": self.audio_bitrate,
            "file_size_mb": round(self.file_size_mb, 2),
            "duration_seconds": round(self.duration_seconds, 2),
            "avg_speed_mbps": round(self.avg_speed_mbps, 2),
            "success": self.success,
            "error": self.error_message if not self.success else None,
        }


def print_header():
    """Print the tool header"""
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'='*70}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}  YouTube Download Quality Benchmark Tool{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}  Testing download speed vs quality theory{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'='*70}{Colors.RESET}\n")


def print_menu():
    """Print the main menu"""
    print(f"\n{Colors.BOLD}Quality Presets:{Colors.RESET}")
    print(f"{Colors.DIM}{'─'*50}{Colors.RESET}")

    for key, preset in QUALITY_PRESETS.items():
        print(f"  {Colors.YELLOW}{key}{Colors.RESET}) {preset['name']}")
        print(f"     {Colors.DIM}{preset['description']}{Colors.RESET}")

    print(f"\n{Colors.BOLD}Other Options:{Colors.RESET}")
    print(f"{Colors.DIM}{'─'*50}{Colors.RESET}")
    print(f"  {Colors.YELLOW}f{Colors.RESET}) List available formats for a video")
    print(f"  {Colors.YELLOW}r{Colors.RESET}) View benchmark results summary")
    print(f"  {Colors.YELLOW}e{Colors.RESET}) Export results to JSON")
    print(f"  {Colors.YELLOW}q{Colors.RESET}) Quit")


def select_video() -> Optional[str]:
    """Let user select or enter a video ID"""
    print(f"\n{Colors.BOLD}Select Test Video:{Colors.RESET}")
    print(f"{Colors.DIM}{'─'*50}{Colors.RESET}")

    for key, video in TEST_VIDEOS.items():
        print(f"  {Colors.YELLOW}{key}{Colors.RESET}) {video['name']}")

    choice = input(f"\n{Colors.CYAN}Enter choice: {Colors.RESET}").strip().lower()

    if choice == "c":
        video_input = input(f"{Colors.CYAN}Enter YouTube video ID or URL: {Colors.RESET}").strip()
        # Extract video ID from URL if needed
        if "youtube.com" in video_input or "youtu.be" in video_input:
            if "v=" in video_input:
                video_id = video_input.split("v=")[1].split("&")[0]
            elif "youtu.be/" in video_input:
                video_id = video_input.split("youtu.be/")[1].split("?")[0]
            else:
                video_id = video_input
        else:
            video_id = video_input
        return video_id
    elif choice in TEST_VIDEOS:
        return TEST_VIDEOS[choice]["id"]
    else:
        print(f"{Colors.RED}Invalid choice{Colors.RESET}")
        return None


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


def run_benchmark(video_id: str, format_string: str, format_name: str) -> BenchmarkResult:
    """Run a single benchmark test with real-time output"""
    result = BenchmarkResult()
    result.video_id = video_id
    result.format_string = format_string
    result.format_name = format_name

    # Create temp directory for download
    temp_dir = tempfile.mkdtemp(prefix="yt_benchmark_")
    output_template = os.path.join(temp_dir, "%(id)s.%(ext)s")

    print(f"\n{Colors.BOLD}{Colors.BLUE}{'='*70}{Colors.RESET}")
    print(f"{Colors.BOLD}Starting Benchmark{Colors.RESET}")
    print(f"{Colors.BLUE}{'='*70}{Colors.RESET}")
    print(f"  Video ID: {Colors.YELLOW}{video_id}{Colors.RESET}")
    print(f"  Format:   {Colors.YELLOW}{format_name}{Colors.RESET}")
    print(f"  String:   {Colors.DIM}{format_string}{Colors.RESET}")
    print(f"{Colors.BLUE}{'─'*70}{Colors.RESET}\n")

    # Build yt-dlp command
    cmd = [
        "yt-dlp",
        "--verbose",
        "--newline",  # Output progress on new lines for real-time parsing
        "--no-warnings",
        "-f", format_string,
        "-o", output_template,
        "--no-playlist",
        "--print-to-file", "%(title)s", os.path.join(temp_dir, "title.txt"),
        f"https://www.youtube.com/watch?v={video_id}",
    ]

    result.start_time = time.time()

    try:
        # Run with real-time output using line buffering
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # Line buffered
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )

        output_lines = []
        current_speed = 0
        last_progress_line = ""

        print(f"{Colors.BOLD}Live Download Progress:{Colors.RESET}")
        print(f"{Colors.DIM}{'─'*70}{Colors.RESET}")

        # Read output line by line in real-time
        for line in iter(process.stdout.readline, ''):
            if not line:
                break

            line = line.rstrip()
            output_lines.append(line)

            # Parse and display progress information
            if "[download]" in line:
                # Progress line - show it
                if "%" in line or "Destination" in line or "100%" in line:
                    # Clear previous line and print new progress
                    if "of" in line and "at" in line:
                        # Full progress line: [download]  45.3% of 5.23MiB at 2.15MiB/s ETA 00:02
                        print(f"\r{Colors.GREEN}{line}{Colors.RESET}", end="", flush=True)
                        last_progress_line = line

                        # Extract speed
                        if " at " in line:
                            try:
                                speed_part = line.split(" at ")[1].split(" ")[0]
                                speed = parse_speed(speed_part)
                                if speed > 0:
                                    result.download_speed_samples.append(speed)
                                    current_speed = speed
                            except (IndexError, ValueError):
                                pass
                    elif "Destination" in line:
                        print(f"\n{Colors.CYAN}{line}{Colors.RESET}")
                    elif "100%" in line:
                        print(f"\r{Colors.GREEN}{line}{Colors.RESET}")

            elif "[info]" in line:
                # Info lines - format selection, etc.
                if "format" in line.lower() or "Downloading" in line:
                    print(f"{Colors.BLUE}{line}{Colors.RESET}")

                    # Try to extract format ID
                    if "Downloading" in line and "format" in line.lower():
                        # [info] Downloading format 251 - opus (audio only)
                        result.selected_format_note = line

            elif "format" in line.lower() and "requested" not in line.lower():
                # Format selection debug output
                if "[debug]" not in line:
                    print(f"{Colors.DIM}{line}{Colors.RESET}")

            elif "ERROR" in line or "error" in line.lower():
                print(f"{Colors.RED}{line}{Colors.RESET}")
                result.error_message = line

            elif "[ExtractAudio]" in line or "[Merger]" in line:
                print(f"{Colors.YELLOW}{line}{Colors.RESET}")

        # Ensure newline after progress
        print()

        # Wait for process to complete
        return_code = process.wait()
        result.end_time = time.time()
        result.raw_output = "\n".join(output_lines)

        if return_code == 0:
            result.success = True

            # Find the downloaded file
            files = list(Path(temp_dir).glob("*"))
            media_files = [f for f in files if f.suffix in ['.webm', '.m4a', '.mp4', '.mkv', '.opus', '.mp3', '.wav', '.ogg']]

            if media_files:
                downloaded_file = media_files[0]
                result.file_size_bytes = downloaded_file.stat().st_size

                # Get video title
                title_file = Path(temp_dir) / "title.txt"
                if title_file.exists():
                    result.video_title = title_file.read_text().strip()

            # Calculate average speed
            if result.download_speed_samples:
                result.download_speed_avg = sum(result.download_speed_samples) / len(result.download_speed_samples)
            elif result.file_size_bytes > 0 and result.duration_seconds > 0:
                # Estimate from file size and time
                result.download_speed_avg = result.file_size_bytes / result.duration_seconds

            # Parse format info from output
            for line in output_lines:
                if "format" in line.lower() and ("opus" in line.lower() or "m4a" in line.lower() or "aac" in line.lower() or "mp4" in line.lower()):
                    if "abr" in line.lower() or "tbr" in line.lower():
                        result.selected_format_note = line
        else:
            result.success = False
            if not result.error_message:
                result.error_message = f"Process exited with code {return_code}"

    except Exception as e:
        result.end_time = time.time()
        result.success = False
        result.error_message = str(e)
        print(f"{Colors.RED}Error: {e}{Colors.RESET}")

    finally:
        # Cleanup temp directory
        try:
            shutil.rmtree(temp_dir)
        except Exception:
            pass

    return result


def print_result(result: BenchmarkResult):
    """Print a formatted benchmark result"""
    print(f"\n{Colors.BOLD}{'='*70}{Colors.RESET}")
    print(f"{Colors.BOLD}Benchmark Result{Colors.RESET}")
    print(f"{'='*70}")

    status = f"{Colors.GREEN}SUCCESS{Colors.RESET}" if result.success else f"{Colors.RED}FAILED{Colors.RESET}"
    print(f"  Status:         {status}")
    print(f"  Video:          {result.video_title or result.video_id}")
    print(f"  Format:         {result.format_name}")

    if result.success:
        print(f"  File Size:      {Colors.YELLOW}{result.file_size_mb:.2f} MB{Colors.RESET}")
        print(f"  Duration:       {Colors.YELLOW}{result.duration_seconds:.2f} seconds{Colors.RESET}")
        print(f"  Avg Speed:      {Colors.CYAN}{result.avg_speed_mbps:.2f} Mbps{Colors.RESET}")

        if result.download_speed_samples:
            max_speed = max(result.download_speed_samples) * 8 / (1024 * 1024)
            min_speed = min(result.download_speed_samples) * 8 / (1024 * 1024)
            print(f"  Speed Range:    {Colors.DIM}{min_speed:.2f} - {max_speed:.2f} Mbps{Colors.RESET}")

        if result.selected_format_note:
            print(f"  Format Info:    {Colors.DIM}{result.selected_format_note}{Colors.RESET}")
    else:
        print(f"  Error:          {Colors.RED}{result.error_message}{Colors.RESET}")

    print(f"{'='*70}\n")


def list_formats(video_id: str):
    """List available formats for a video"""
    print(f"\n{Colors.BOLD}Fetching available formats for {video_id}...{Colors.RESET}\n")

    cmd = [
        "yt-dlp",
        "-F",  # List formats
        "--no-warnings",
        f"https://www.youtube.com/watch?v={video_id}",
    ]

    try:
        # Stream output in real-time
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        for line in iter(process.stdout.readline, ''):
            if not line:
                break
            # Color-code different format types
            if "audio only" in line.lower():
                print(f"{Colors.CYAN}{line.rstrip()}{Colors.RESET}")
            elif "video only" in line.lower():
                print(f"{Colors.YELLOW}{line.rstrip()}{Colors.RESET}")
            elif "─" in line or "ID" in line:
                print(f"{Colors.BOLD}{line.rstrip()}{Colors.RESET}")
            else:
                print(line.rstrip())

        process.wait()

    except Exception as e:
        print(f"{Colors.RED}Error listing formats: {e}{Colors.RESET}")


def print_results_summary(results: list):
    """Print a summary table of all benchmark results"""
    if not results:
        print(f"\n{Colors.YELLOW}No benchmark results yet.{Colors.RESET}")
        return

    print(f"\n{Colors.BOLD}{'='*90}{Colors.RESET}")
    print(f"{Colors.BOLD}Benchmark Results Summary{Colors.RESET}")
    print(f"{'='*90}")

    # Header
    print(f"{'Format':<25} {'Size (MB)':<12} {'Time (s)':<12} {'Speed (Mbps)':<15} {'Status':<10}")
    print(f"{'-'*90}")

    # Sort by average speed descending
    sorted_results = sorted(
        [r for r in results if r.success],
        key=lambda x: x.avg_speed_mbps,
        reverse=True
    )

    # Add failed results at the end
    sorted_results.extend([r for r in results if not r.success])

    for r in sorted_results:
        if r.success:
            speed_color = Colors.GREEN if r.avg_speed_mbps > 5 else (Colors.YELLOW if r.avg_speed_mbps > 2 else Colors.RED)
            print(f"{r.format_name:<25} {r.file_size_mb:<12.2f} {r.duration_seconds:<12.2f} {speed_color}{r.avg_speed_mbps:<15.2f}{Colors.RESET} {Colors.GREEN}OK{Colors.RESET}")
        else:
            print(f"{r.format_name:<25} {'-':<12} {'-':<12} {'-':<15} {Colors.RED}FAILED{Colors.RESET}")

    print(f"{'='*90}\n")

    # Analysis
    if len([r for r in results if r.success]) >= 2:
        successful = [r for r in results if r.success]
        fastest = max(successful, key=lambda x: x.avg_speed_mbps)
        slowest = min(successful, key=lambda x: x.avg_speed_mbps)

        print(f"{Colors.BOLD}Analysis:{Colors.RESET}")
        print(f"  Fastest: {Colors.GREEN}{fastest.format_name}{Colors.RESET} at {fastest.avg_speed_mbps:.2f} Mbps")
        print(f"  Slowest: {Colors.RED}{slowest.format_name}{Colors.RESET} at {slowest.avg_speed_mbps:.2f} Mbps")

        if fastest.avg_speed_mbps > 0 and slowest.avg_speed_mbps > 0:
            ratio = fastest.avg_speed_mbps / slowest.avg_speed_mbps
            print(f"  Speed Ratio: {Colors.YELLOW}{ratio:.2f}x{Colors.RESET} difference")
        print()


def export_results(results: list):
    """Export results to JSON file"""
    if not results:
        print(f"\n{Colors.YELLOW}No results to export.{Colors.RESET}")
        return

    filename = f"benchmark_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    data = {
        "benchmark_date": datetime.now().isoformat(),
        "results": [r.to_dict() for r in results],
    }

    with open(filename, 'w') as f:
        json.dump(data, f, indent=2)

    print(f"\n{Colors.GREEN}Results exported to: {filename}{Colors.RESET}")


def main():
    """Main interactive loop"""
    print_header()

    # Check for yt-dlp
    try:
        subprocess.run(["yt-dlp", "--version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print(f"{Colors.RED}Error: yt-dlp not found. Please install it first.{Colors.RESET}")
        sys.exit(1)

    results = []

    while True:
        print_menu()
        choice = input(f"\n{Colors.CYAN}Enter choice: {Colors.RESET}").strip().lower()

        if choice == 'q':
            print(f"\n{Colors.GREEN}Goodbye!{Colors.RESET}\n")
            break

        elif choice == 'r':
            print_results_summary(results)

        elif choice == 'e':
            export_results(results)

        elif choice == 'f':
            video_id = select_video()
            if video_id:
                list_formats(video_id)

        elif choice == 'c':
            # Custom format
            video_id = select_video()
            if not video_id:
                continue

            format_string = input(f"{Colors.CYAN}Enter custom format string: {Colors.RESET}").strip()
            if not format_string:
                print(f"{Colors.RED}Format string required{Colors.RESET}")
                continue

            result = run_benchmark(video_id, format_string, f"Custom: {format_string[:20]}")
            print_result(result)
            results.append(result)

        elif choice in QUALITY_PRESETS:
            preset = QUALITY_PRESETS[choice]

            video_id = select_video()
            if not video_id:
                continue

            format_string = preset["format"]
            if format_string is None:
                format_string = input(f"{Colors.CYAN}Enter custom format string: {Colors.RESET}").strip()
                if not format_string:
                    print(f"{Colors.RED}Format string required{Colors.RESET}")
                    continue

            result = run_benchmark(video_id, format_string, preset["name"])
            print_result(result)
            results.append(result)

        else:
            print(f"{Colors.RED}Invalid choice. Please try again.{Colors.RESET}")

    # Final summary if we have results
    if results:
        print_results_summary(results)


if __name__ == "__main__":
    main()
