"""Admin dashboard routes."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["admin"])


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
        .log-entry { font-family: monospace; font-size: 12px; }
        .pulse { animation: pulse 2s infinite; }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
    </style>
</head>
<body class="bg-gray-900 text-gray-100 min-h-screen">
    <div class="container mx-auto px-4 py-8 max-w-6xl">
        <!-- Header -->
        <div class="flex items-center justify-between mb-8">
            <div>
                <h1 class="text-3xl font-bold text-white">SafePlay YT-DLP</h1>
                <p class="text-gray-400">Admin Dashboard</p>
            </div>
            <div id="connection-status" class="flex items-center gap-2">
                <span class="w-3 h-3 bg-green-500 rounded-full pulse"></span>
                <span class="text-sm text-gray-400">Connected</span>
            </div>
        </div>

        <!-- Health Status Cards -->
        <div class="grid grid-cols-1 md:grid-cols-4 gap-4 mb-8">
            <div class="bg-gray-800 rounded-lg p-4 border border-gray-700">
                <div class="flex items-center justify-between">
                    <span class="text-gray-400 text-sm">Service Status</span>
                    <i data-lucide="activity" class="w-5 h-5 text-gray-500"></i>
                </div>
                <div id="service-status" class="text-2xl font-bold mt-2 status-ok">--</div>
            </div>
            <div class="bg-gray-800 rounded-lg p-4 border border-gray-700">
                <div class="flex items-center justify-between">
                    <span class="text-gray-400 text-sm">YT-DLP</span>
                    <i data-lucide="download" class="w-5 h-5 text-gray-500"></i>
                </div>
                <div id="ytdlp-status" class="text-2xl font-bold mt-2">--</div>
            </div>
            <div class="bg-gray-800 rounded-lg p-4 border border-gray-700">
                <div class="flex items-center justify-between">
                    <span class="text-gray-400 text-sm">Supabase</span>
                    <i data-lucide="database" class="w-5 h-5 text-gray-500"></i>
                </div>
                <div id="supabase-status" class="text-2xl font-bold mt-2">--</div>
            </div>
            <div class="bg-gray-800 rounded-lg p-4 border border-gray-700">
                <div class="flex items-center justify-between">
                    <span class="text-gray-400 text-sm">Proxy</span>
                    <i data-lucide="shield" class="w-5 h-5 text-gray-500"></i>
                </div>
                <div id="proxy-status" class="text-2xl font-bold mt-2">--</div>
            </div>
        </div>

        <!-- Main Content Grid -->
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <!-- Download Test Panel -->
            <div class="bg-gray-800 rounded-lg border border-gray-700">
                <div class="p-4 border-b border-gray-700">
                    <h2 class="text-lg font-semibold flex items-center gap-2">
                        <i data-lucide="play-circle" class="w-5 h-5"></i>
                        Test Download
                    </h2>
                </div>
                <div class="p-4">
                    <div class="space-y-4">
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">API Key</label>
                            <input type="password" id="api-key"
                                   class="w-full bg-gray-700 border border-gray-600 rounded px-3 py-2 text-white focus:outline-none focus:border-blue-500"
                                   placeholder="Enter your API key">
                        </div>
                        <div>
                            <label class="block text-sm text-gray-400 mb-1">YouTube URL or Video ID</label>
                            <input type="text" id="youtube-input"
                                   class="w-full bg-gray-700 border border-gray-600 rounded px-3 py-2 text-white focus:outline-none focus:border-blue-500"
                                   placeholder="e.g., dQw4w9WgXcQ or https://youtube.com/watch?v=...">
                        </div>
                        <button onclick="startDownload()"
                                class="w-full bg-blue-600 hover:bg-blue-700 text-white font-medium py-2 px-4 rounded flex items-center justify-center gap-2 transition">
                            <i data-lucide="download" class="w-4 h-4"></i>
                            Start Download
                        </button>
                    </div>

                    <!-- Progress Section -->
                    <div id="progress-section" class="mt-6 hidden">
                        <div class="flex items-center justify-between mb-2">
                            <span class="text-sm text-gray-400">Progress</span>
                            <span id="progress-percent" class="text-sm font-mono">0%</span>
                        </div>
                        <div class="w-full bg-gray-700 rounded-full h-2">
                            <div id="progress-bar" class="bg-blue-500 h-2 rounded-full transition-all duration-300" style="width: 0%"></div>
                        </div>
                        <div id="progress-status" class="text-sm text-gray-400 mt-2">Waiting...</div>
                    </div>

                    <!-- Result Section -->
                    <div id="result-section" class="mt-6 hidden">
                        <div class="bg-gray-700 rounded p-4">
                            <h3 class="font-medium mb-2 flex items-center gap-2">
                                <i data-lucide="check-circle" class="w-4 h-4 text-green-500"></i>
                                Download Complete
                            </h3>
                            <div id="result-details" class="text-sm space-y-1 text-gray-300"></div>
                        </div>
                    </div>

                    <!-- Error Section -->
                    <div id="error-section" class="mt-6 hidden">
                        <div class="bg-red-900/50 border border-red-700 rounded p-4">
                            <h3 class="font-medium mb-2 flex items-center gap-2 text-red-400">
                                <i data-lucide="alert-circle" class="w-4 h-4"></i>
                                Error
                            </h3>
                            <div id="error-details" class="text-sm text-red-300"></div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Recent Activity / Jobs -->
            <div class="bg-gray-800 rounded-lg border border-gray-700">
                <div class="p-4 border-b border-gray-700 flex items-center justify-between">
                    <h2 class="text-lg font-semibold flex items-center gap-2">
                        <i data-lucide="list" class="w-5 h-5"></i>
                        Recent Jobs
                    </h2>
                    <button onclick="clearJobs()" class="text-sm text-gray-400 hover:text-white">Clear</button>
                </div>
                <div class="p-4">
                    <div id="jobs-list" class="space-y-3 max-h-96 overflow-y-auto">
                        <p class="text-gray-500 text-sm text-center py-8">No jobs yet. Start a download to see activity.</p>
                    </div>
                </div>
            </div>
        </div>

        <!-- Quick Stats -->
        <div class="mt-6 bg-gray-800 rounded-lg border border-gray-700">
            <div class="p-4 border-b border-gray-700">
                <h2 class="text-lg font-semibold flex items-center gap-2">
                    <i data-lucide="bar-chart-2" class="w-5 h-5"></i>
                    Session Statistics
                </h2>
            </div>
            <div class="p-4 grid grid-cols-2 md:grid-cols-4 gap-4">
                <div class="text-center">
                    <div id="stat-total" class="text-3xl font-bold text-blue-400">0</div>
                    <div class="text-sm text-gray-400">Total Jobs</div>
                </div>
                <div class="text-center">
                    <div id="stat-success" class="text-3xl font-bold text-green-400">0</div>
                    <div class="text-sm text-gray-400">Successful</div>
                </div>
                <div class="text-center">
                    <div id="stat-failed" class="text-3xl font-bold text-red-400">0</div>
                    <div class="text-sm text-gray-400">Failed</div>
                </div>
                <div class="text-center">
                    <div id="stat-bytes" class="text-3xl font-bold text-purple-400">0 MB</div>
                    <div class="text-sm text-gray-400">Total Downloaded</div>
                </div>
            </div>
        </div>

        <!-- System Info -->
        <div class="mt-6 text-center text-gray-500 text-sm">
            <p>SafePlay YT-DLP Service • <span id="server-time">--</span></p>
        </div>
    </div>

    <script>
        // Initialize Lucide icons
        lucide.createIcons();

        // State
        let jobs = JSON.parse(localStorage.getItem('safeplay_jobs') || '[]');
        let stats = JSON.parse(localStorage.getItem('safeplay_stats') || '{"total":0,"success":0,"failed":0,"bytes":0}');
        let pollInterval = null;

        // Load saved API key
        document.getElementById('api-key').value = localStorage.getItem('safeplay_api_key') || '';

        // Save API key on change
        document.getElementById('api-key').addEventListener('change', (e) => {
            localStorage.setItem('safeplay_api_key', e.target.value);
        });

        // Fetch health status
        async function fetchHealth() {
            try {
                const response = await fetch('/health');
                const data = await response.json();

                document.getElementById('service-status').textContent = data.status.toUpperCase();
                document.getElementById('service-status').className = 'text-2xl font-bold mt-2 ' +
                    (data.status === 'ok' ? 'status-ok' : 'status-error');

                updateCheckStatus('ytdlp-status', data.checks.ytdlp, 'available');
                updateCheckStatus('supabase-status', data.checks.supabase, 'connected');
                updateCheckStatus('proxy-status', data.checks.proxy, ['configured', 'reachable']);

                document.getElementById('server-time').textContent = new Date(data.timestamp).toLocaleString();
            } catch (error) {
                document.getElementById('service-status').textContent = 'OFFLINE';
                document.getElementById('service-status').className = 'text-2xl font-bold mt-2 status-error';
            }
        }

        function updateCheckStatus(elementId, value, goodValues) {
            const el = document.getElementById(elementId);
            const isGood = Array.isArray(goodValues) ? goodValues.includes(value) : value === goodValues;
            el.textContent = value ? value.toUpperCase() : 'UNKNOWN';
            el.className = 'text-2xl font-bold mt-2 ' + (isGood ? 'status-ok' : 'status-error');
        }

        // Extract YouTube ID from URL or ID
        function extractVideoId(input) {
            input = input.trim();

            // Already an ID
            if (/^[a-zA-Z0-9_-]{11}$/.test(input)) {
                return input;
            }

            // URL patterns
            const patterns = [
                /(?:youtube\\.com\\/watch\\?v=|youtu\\.be\\/|youtube\\.com\\/embed\\/)([a-zA-Z0-9_-]{11})/,
                /youtube\\.com\\/shorts\\/([a-zA-Z0-9_-]{11})/
            ];

            for (const pattern of patterns) {
                const match = input.match(pattern);
                if (match) return match[1];
            }

            return input; // Return as-is, let server validate
        }

        // Start download
        async function startDownload() {
            const apiKey = document.getElementById('api-key').value;
            const input = document.getElementById('youtube-input').value;

            if (!apiKey) {
                showError('Please enter your API key');
                return;
            }

            if (!input) {
                showError('Please enter a YouTube URL or video ID');
                return;
            }

            const videoId = extractVideoId(input);
            const jobId = 'job-' + Date.now();

            // Reset UI
            hideResults();
            showProgress();
            updateProgress(0, 'Starting download...');

            // Add to jobs list
            const job = {
                id: jobId,
                videoId: videoId,
                status: 'pending',
                startTime: new Date().toISOString(),
                progress: 0
            };
            addJob(job);

            try {
                const response = await fetch('/api/download', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-API-Key': apiKey
                    },
                    body: JSON.stringify({
                        youtube_id: videoId,
                        job_id: jobId
                    })
                });

                const data = await response.json();

                if (response.ok && data.status === 'success') {
                    updateProgress(100, 'Complete!');
                    showSuccess(data);
                    updateJob(jobId, 'success', data);
                    stats.success++;
                    stats.bytes += data.filesize_bytes || 0;
                } else {
                    throw new Error(data.detail?.message || data.message || 'Download failed');
                }
            } catch (error) {
                showError(error.message);
                updateJob(jobId, 'failed', { error: error.message });
                stats.failed++;
            }

            stats.total++;
            saveStats();
            updateStatsDisplay();
        }

        // Progress UI
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
                <p><strong>Video ID:</strong> ${data.youtube_id}</p>
                <p><strong>Duration:</strong> ${formatDuration(data.duration_seconds)}</p>
                <p><strong>File Size:</strong> ${formatBytes(data.filesize_bytes)}</p>
                <p><strong>Storage Path:</strong> ${data.storage_path}</p>
            `;
        }

        function showError(message) {
            document.getElementById('error-section').classList.remove('hidden');
            document.getElementById('error-details').textContent = message;
        }

        // Jobs management
        function addJob(job) {
            jobs.unshift(job);
            if (jobs.length > 50) jobs = jobs.slice(0, 50);
            saveJobs();
            renderJobs();
        }

        function updateJob(jobId, status, data) {
            const job = jobs.find(j => j.id === jobId);
            if (job) {
                job.status = status;
                job.endTime = new Date().toISOString();
                job.data = data;
                saveJobs();
                renderJobs();
            }
        }

        function renderJobs() {
            const container = document.getElementById('jobs-list');

            if (jobs.length === 0) {
                container.innerHTML = '<p class="text-gray-500 text-sm text-center py-8">No jobs yet. Start a download to see activity.</p>';
                return;
            }

            container.innerHTML = jobs.map(job => `
                <div class="bg-gray-700 rounded p-3">
                    <div class="flex items-center justify-between">
                        <span class="font-mono text-sm">${job.videoId}</span>
                        <span class="text-xs px-2 py-1 rounded ${getStatusClass(job.status)}">${job.status.toUpperCase()}</span>
                    </div>
                    <div class="text-xs text-gray-400 mt-1">
                        ${new Date(job.startTime).toLocaleString()}
                        ${job.data?.title ? ' • ' + job.data.title : ''}
                    </div>
                    ${job.data?.filesize_bytes ? '<div class="text-xs text-gray-500 mt-1">' + formatBytes(job.data.filesize_bytes) + '</div>' : ''}
                    ${job.data?.error ? '<div class="text-xs text-red-400 mt-1">' + job.data.error + '</div>' : ''}
                </div>
            `).join('');
        }

        function getStatusClass(status) {
            switch(status) {
                case 'success': return 'bg-green-600';
                case 'failed': return 'bg-red-600';
                case 'pending': return 'bg-yellow-600';
                default: return 'bg-gray-600';
            }
        }

        function clearJobs() {
            jobs = [];
            stats = { total: 0, success: 0, failed: 0, bytes: 0 };
            saveJobs();
            saveStats();
            renderJobs();
            updateStatsDisplay();
        }

        function saveJobs() {
            localStorage.setItem('safeplay_jobs', JSON.stringify(jobs));
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

        // Utilities
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
        fetchHealth();
        setInterval(fetchHealth, 10000); // Refresh health every 10s
        renderJobs();
        updateStatsDisplay();
    </script>
</body>
</html>
"""


@router.get("/admin", response_class=HTMLResponse)
async def admin_dashboard():
    """Serve the admin dashboard."""
    return HTMLResponse(content=ADMIN_HTML)
