# YT-DLP Service - Implementation Notes for Orchestration

> **IMPORTANT:** This document describes the **actual implementation** which differs from ARCHITECTURE_v2.md in several ways. Use this as the source of truth when building the orchestration service.

---

## Key Differences from Architecture Docs

### 1. Audio Downloads by Default (Video Also Supported)

We download **audio by default** to reduce file sizes and stay under Supabase's 50MB free tier limit. Video downloads are supported but not the default.

| Aspect | Docs Said | Default Implementation |
|--------|-----------|----------------------|
| Format | `best[height<=720][ext=mp4]` | `worstaudio` (audio only) |
| Storage path | `{youtube_id}/video.mp4` | `{youtube_id}/audio.{ext}` |
| File size | ~100-200MB | ~5MB for 15 min |
| Extension | `.mp4` | `.m4a`, `.webm`, `.opus`, etc. |

**Why audio?** ElevenLabs Scribe v2 accepts audio files - video is unnecessary for transcription, and audio files are 10-20x smaller.

---

## API Contract

### Endpoint: Download Video

```
POST /api/download
```

**Headers:**
```
X-API-Key: {YTDLP_API_KEY}
Content-Type: application/json
```

**Request Body:**
```json
{
  "youtube_id": "K7gCkbZ1TbU",
  "job_id": "job-1234567890"
}
```

**Success Response (200 OK):**
```json
{
  "status": "success",
  "youtube_id": "K7gCkbZ1TbU",
  "job_id": "job-1234567890",
  "storage_path": "K7gCkbZ1TbU/audio.m4a",
  "duration_seconds": 945,
  "title": "Video Title Here",
  "filesize_bytes": 5242880
}
```

**Error Response (400/500):**
```json
{
  "error_code": "PRIVATE_VIDEO",
  "message": "Video is private: This video is private.",
  "retryable": false,
  "user_message": "This video is private and cannot be accessed."
}
```

---

## Error Handling

### Error Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `error_code` | string | Machine-readable error identifier |
| `message` | string | Technical error details (for logging) |
| `retryable` | boolean | Whether orchestration should retry |
| `user_message` | string | User-friendly message to display |

### Orchestration Logic

```typescript
try {
  const result = await downloadService.download(youtubeId, jobId);
  // Success - proceed to transcription
  return result.storage_path;
} catch (error) {
  if (error.retryable === false) {
    // PERMANENT ERROR - Show message to user, don't retry
    throw new UserFacingError(error.user_message);
  } else {
    // RETRYABLE ERROR - Could retry or ask user to try again
    // Note: Service already retried 5 times internally
    throw new RetryableError(error.user_message);
  }
}
```

### Error Codes Reference

#### Permanent Errors (retryable: false)

| Error Code | Description | User Message |
|------------|-------------|--------------|
| `VIDEO_UNAVAILABLE` | Video deleted, region-blocked, or doesn't exist | "This video doesn't exist or has been removed." |
| `PRIVATE_VIDEO` | Video is set to private | "This video is private and cannot be accessed." |
| `AGE_RESTRICTED` | Requires age verification/login | "This video requires age verification and cannot be processed." |
| `COPYRIGHT_BLOCKED` | DMCA takedown or copyright claim | "This video is blocked due to copyright restrictions." |
| `LIVE_STREAM` | Video is currently live | "Live streams cannot be processed. Please try again after the stream ends." |
| `PREMIUM_CONTENT` | YouTube Premium or channel membership required | "This video requires YouTube Premium and cannot be processed." |

#### Retryable Errors (retryable: true)

| Error Code | Description | User Message |
|------------|-------------|--------------|
| `DOWNLOAD_ERROR` | Generic download failure | "Download failed. Please try again." |
| `DOWNLOAD_TIMEOUT` | Download exceeded 180 seconds | "Download took too long. Please try again." |
| `BOT_DETECTED` | YouTube bot detection triggered | "Temporary issue with YouTube. Please try again in a moment." |
| `UPLOAD_FAILED` | Supabase storage upload failed | "Failed to save the video. Please try again." |
| `RATE_LIMITED` | Too many requests | "Too many requests. Please wait a moment and try again." |
| `INTERNAL_ERROR` | Unexpected server error | "An unexpected error occurred. Please try again." |

---

## Timeouts & Reliability

### Internal Retry Logic

The download service handles retries internally:
- **5 retry attempts** with new proxy IP on each attempt
- **Escalating backoff:** 1s, 2s, 5s, 10s between attempts
- **Circuit breaker:** 30s delay + final attempt if all fail with bot detection
- **180 second timeout** per download attempt

### Recommended Orchestration Timeout

```typescript
const DOWNLOAD_TIMEOUT_MS = 5 * 60 * 1000; // 5 minutes

const result = await fetch(downloadUrl, {
  method: 'POST',
  headers: { 'X-API-Key': apiKey },
  body: JSON.stringify({ youtube_id, job_id }),
  signal: AbortSignal.timeout(DOWNLOAD_TIMEOUT_MS)
});
```

**Why 5 minutes?** The service can take up to:
- 180s per attempt × 5 attempts = 15 min worst case
- But most downloads complete in 10-30 seconds
- 5 minutes covers normal operation + some retries

---

## Generating Signed URLs for ElevenLabs

After receiving `storage_path` from the download service:

```typescript
import { createClient } from '@supabase/supabase-js';

const supabase = createClient(
  process.env.SUPABASE_URL!,
  process.env.SUPABASE_SERVICE_KEY!
);

async function getSignedUrl(storagePath: string): Promise<string> {
  const { data, error } = await supabase.storage
    .from('videos')  // Bucket name
    .createSignedUrl(storagePath, 3600);  // 1 hour expiry

  if (error || !data?.signedUrl) {
    throw new Error('Failed to generate signed URL');
  }

  return data.signedUrl;
}

// Usage
const storagePath = downloadResult.storage_path; // "K7gCkbZ1TbU/audio.m4a"
const signedUrl = await getSignedUrl(storagePath);

// Pass to ElevenLabs
const transcript = await elevenlabs.transcribe({
  model_id: 'scribe_v2',
  cloud_storage_url: signedUrl,
  timestamps_granularity: 'character'
});
```

---

## Environment Variables

The orchestration service needs:

```env
# YT-DLP Download Service
YTDLP_SERVICE_URL=https://your-ytdlp-server.com
YTDLP_API_KEY=your-api-key-here

# Supabase (for signed URLs)
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_SERVICE_KEY=your-service-key

# ElevenLabs
ELEVENLABS_API_KEY=your-elevenlabs-key
```

---

## Complete Flow Example

```typescript
async function processVideo(youtubeId: string, userId: string) {
  const jobId = `job-${Date.now()}`;

  try {
    // 1. Call download service
    const downloadResult = await fetch(`${YTDLP_SERVICE_URL}/api/download`, {
      method: 'POST',
      headers: {
        'X-API-Key': YTDLP_API_KEY,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ youtube_id: youtubeId, job_id: jobId }),
      signal: AbortSignal.timeout(300000) // 5 min
    });

    if (!downloadResult.ok) {
      const error = await downloadResult.json();
      if (!error.retryable) {
        // Permanent error - inform user
        await updateJobStatus(jobId, 'failed', error.user_message);
        return { success: false, error: error.user_message };
      }
      throw new Error(error.user_message);
    }

    const { storage_path, duration_seconds, title } = await downloadResult.json();

    // 2. Generate signed URL
    const signedUrl = await getSignedUrl(storage_path);

    // 3. Call ElevenLabs for transcription
    const transcript = await transcribeWithElevenLabs(signedUrl);

    // 4. Store transcript and clean up
    await storeTranscript(youtubeId, transcript);
    await deleteFromStorage(storage_path); // Optional: save storage costs

    return { success: true, transcript };

  } catch (error) {
    await updateJobStatus(jobId, 'failed', error.message);
    return { success: false, error: error.message };
  }
}
```

---

## Health Check

Before processing, verify the service is healthy:

```
GET /health
```

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

---

## Batch Downloads (Optional)

For processing multiple videos:

```
POST /api/download/batch
```

**Request:**
```json
{
  "videos": [
    { "youtube_id": "ABC123", "job_id": "job-1" },
    { "youtube_id": "DEF456", "job_id": "job-2" }
  ]
}
```

**Response:**
```json
{
  "status": "completed",
  "total": 2,
  "succeeded": 1,
  "failed": 1,
  "cached": 0,
  "results": [
    {
      "youtube_id": "ABC123",
      "job_id": "job-1",
      "status": "success",
      "storage_path": "ABC123/audio.m4a"
    },
    {
      "youtube_id": "DEF456",
      "job_id": "job-2",
      "status": "failed",
      "error": "Video is private",
      "error_code": "PRIVATE_VIDEO",
      "retryable": false,
      "user_message": "This video is private and cannot be accessed."
    }
  ]
}
```

Max 20 videos per batch. All processed in parallel.

---

## Summary

1. **Audio only** - Files are ~5MB, stored as `{youtube_id}/audio.{ext}`
2. **Check `retryable`** - Don't retry permanent errors, show `user_message` to user
3. **5 minute timeout** - Service handles internal retries
4. **Generate signed URL** - Pass to ElevenLabs `cloud_storage_url`
5. **Delete after transcription** - Optional, saves storage costs
