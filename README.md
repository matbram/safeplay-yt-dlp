# SafePlay YT-DLP Download Service

YouTube video download service for the SafePlay profanity filtering platform.

## Overview

This service is responsible for:
- Downloading YouTube videos using yt-dlp
- Routing downloads through OxyLabs residential proxies to bypass bot detection
- Uploading video files to Supabase Storage
- Reporting progress back to the orchestration service

**Note:** This is an internal service - it's only called by the Orchestration Service, never directly by users.

## Tech Stack

- **Runtime:** Python 3.12+
- **Framework:** FastAPI
- **Download:** yt-dlp
- **Proxy:** OxyLabs Residential Proxies
- **Storage:** Supabase Storage
- **Deployment:** Digital Ocean Droplet (Ubuntu 24.04 LTS)

## Quick Deploy (Digital Ocean)

### One-Line Install

SSH into your Ubuntu 24.04 droplet and run:

```bash
curl -sSL https://raw.githubusercontent.com/matbram/safeplay-yt-dlp/main/deploy/install.sh | sudo bash
```

### What the Installer Does

1. Installs system dependencies (Python 3.12, ffmpeg, yt-dlp)
2. Creates a dedicated `safeplay` user
3. Clones this repository
4. Sets up Python virtual environment
5. Configures systemd service with auto-restart
6. Sets up self-healing health checks (every 2 minutes)
7. Configures UFW firewall and fail2ban
8. Sets up log rotation
9. Creates diagnostic tools

### Self-Healing Features

- **Health monitoring:** Checks service every 2 minutes
- **Auto-restart:** Service restarts on failure
- **Temp cleanup:** Old downloads cleaned automatically
- **Auto-update:** Weekly checks for updates
- **yt-dlp updates:** Daily updates to latest version

### Management Commands

After installation, use these commands:

```bash
# Check service status
safeplay-status

# View logs
safeplay-logs app     # Application logs
safeplay-logs error   # Error logs
safeplay-logs health  # Health check logs
safeplay-logs all     # All logs

# Restart service
safeplay-restart

# Manual update
safeplay-update
```

### Uninstall

```bash
curl -sSL https://raw.githubusercontent.com/matbram/safeplay-yt-dlp/main/deploy/uninstall.sh | sudo bash
```

## API Endpoints

### POST /api/download

Download a YouTube video and upload to storage.

**Headers:**
```
X-API-Key: your-internal-api-key
```

**Request:**
```json
{
  "youtube_id": "dQw4w9WgXcQ",
  "job_id": "uuid-job-id"
}
```

**Response:**
```json
{
  "status": "success",
  "youtube_id": "dQw4w9WgXcQ",
  "job_id": "uuid-job-id",
  "storage_path": "dQw4w9WgXcQ/video.mp4",
  "duration_seconds": 213,
  "title": "Rick Astley - Never Gonna Give You Up",
  "filesize_bytes": 15234567
}
```

### GET /api/download/{job_id}/status

Check download progress.

**Response:**
```json
{
  "job_id": "uuid-job-id",
  "status": "downloading",
  "progress": 45,
  "error": null
}
```

### GET /health

Health check endpoint.

**Response:**
```json
{
  "status": "ok",
  "timestamp": "2024-01-15T10:30:00Z",
  "checks": {
    "ytdlp": "available",
    "supabase": "connected",
    "proxy": "configured"
  }
}
```

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `PORT` | Server port (default: 3002) | No |
| `ENVIRONMENT` | Environment (development/production) | No |
| `SUPABASE_URL` | Supabase project URL | Yes |
| `SUPABASE_SERVICE_KEY` | Supabase service role key | Yes |
| `OXYLABS_USERNAME` | OxyLabs proxy username | Yes |
| `OXYLABS_PASSWORD` | OxyLabs proxy password | Yes |
| `API_KEY` | Internal API key for auth | Yes |
| `TEMP_DIR` | Temp storage path | No |

## Local Development

1. Clone the repository:
   ```bash
   git clone https://github.com/matbram/safeplay-yt-dlp.git
   cd safeplay-yt-dlp
   ```

2. Create a virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Copy `.env.example` to `.env` and fill in values

5. Run the server:
   ```bash
   uvicorn app.main:app --reload --port 3002
   ```

## Testing

```bash
# Health check
curl http://localhost:3002/health

# Download video (requires API key)
curl -X POST http://localhost:3002/api/download \
  -H "X-API-Key: your-internal-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "youtube_id": "dQw4w9WgXcQ",
    "job_id": "test-job-123"
  }'

# Check status
curl http://localhost:3002/api/download/test-job-123/status \
  -H "X-API-Key: your-internal-api-key"
```

## Security Considerations

1. **Network Security:** Use Digital Ocean's VPC/private networking between this service and the Orchestration Service
2. **Firewall:** UFW is configured to only allow SSH and port 3002
3. **API Key:** Use a strong, random API key (auto-generated during install)
4. **fail2ban:** Protects against brute force attacks

## Error Codes

| Code | Description |
|------|-------------|
| `VIDEO_UNAVAILABLE` | Video deleted or region-locked |
| `PRIVATE_VIDEO` | Video is private |
| `AGE_RESTRICTED` | Requires login |
| `COPYRIGHT_BLOCKED` | DMCA takedown |
| `DOWNLOAD_ERROR` | General download failure |
| `UPLOAD_FAILED` | Storage upload failure |

## Architecture Notes

- **No audio extraction needed:** ElevenLabs accepts video files directly via `cloud_storage_url`
- **Proxy rotation:** OxyLabs automatically rotates IPs for each request
- **Temp file cleanup:** Files are automatically cleaned up after upload
- **Progress tracking:** In-memory progress tracking

## Recommended Droplet Specs

| Tier | Specs | Videos/Day |
|------|-------|------------|
| Basic | 1 vCPU, 1GB RAM | ~50 |
| Standard | 2 vCPU, 2GB RAM | ~200 |
| Performance | 4 vCPU, 8GB RAM | ~500+ |

## License

Proprietary - All rights reserved
