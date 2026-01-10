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

- **Runtime:** Python 3.11+
- **Framework:** FastAPI
- **Download:** yt-dlp
- **Proxy:** OxyLabs Residential Proxies
- **Storage:** Supabase Storage
- **Deployment:** Railway

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
    "proxy": "reachable"
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

1. Clone the repository
2. Create a virtual environment:
   ```bash
   python -m venv venv
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

## Deployment

This service is deployed on Railway using the included `railway.json` configuration.

**Important:** This service should NOT have a public domain. Only the Orchestration Service calls it via Railway internal networking.

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
- **Progress tracking:** In-memory progress tracking (consider Redis for production scale)

## License

Proprietary - All rights reserved
