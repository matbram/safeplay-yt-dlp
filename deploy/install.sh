#!/bin/bash
# ============================================================================
# SafePlay YT-DLP Service - Self-Healing Deployment Script
# Target: Ubuntu 24.04 LTS (Digital Ocean Droplet)
# ============================================================================
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/matbram/safeplay-yt-dlp/main/deploy/install.sh | sudo bash
#
# Or download and run:
#   wget https://raw.githubusercontent.com/matbram/safeplay-yt-dlp/main/deploy/install.sh
#   chmod +x install.sh
#   sudo ./install.sh
#
# ============================================================================

set -euo pipefail

# ============================================================================
# CONFIGURATION
# ============================================================================

readonly APP_NAME="safeplay-ytdlp"
readonly APP_USER="safeplay"
readonly APP_DIR="/opt/safeplay-ytdlp"
readonly VENV_DIR="${APP_DIR}/venv"
readonly LOG_DIR="/var/log/safeplay"
readonly TEMP_DIR="/tmp/safeplay-downloads"
readonly REPO_URL="https://github.com/matbram/safeplay-yt-dlp.git"
readonly BRANCH="main"
readonly SERVICE_PORT="3002"

# Colors for output
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly BLUE='\033[0;34m'
readonly NC='\033[0m' # No Color

# Retry configuration
readonly MAX_RETRIES=3
readonly RETRY_DELAY=5

# ============================================================================
# LOGGING FUNCTIONS
# ============================================================================

log_info() {
    echo -e "${BLUE}[INFO]${NC} $(date '+%Y-%m-%d %H:%M:%S') - $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $(date '+%Y-%m-%d %H:%M:%S') - $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $(date '+%Y-%m-%d %H:%M:%S') - $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $(date '+%Y-%m-%d %H:%M:%S') - $1"
}

# ============================================================================
# SELF-HEALING RETRY FUNCTION
# ============================================================================

retry_command() {
    local cmd="$1"
    local description="${2:-Command}"
    local retries=0

    while [ $retries -lt $MAX_RETRIES ]; do
        log_info "Attempting: ${description} (attempt $((retries + 1))/${MAX_RETRIES})"

        if eval "$cmd"; then
            log_success "${description} completed successfully"
            return 0
        else
            retries=$((retries + 1))
            if [ $retries -lt $MAX_RETRIES ]; then
                log_warn "${description} failed, retrying in ${RETRY_DELAY}s..."
                sleep $RETRY_DELAY
            fi
        fi
    done

    log_error "${description} failed after ${MAX_RETRIES} attempts"
    return 1
}

# ============================================================================
# SYSTEM CHECKS
# ============================================================================

check_root() {
    if [ "$EUID" -ne 0 ]; then
        log_error "This script must be run as root (use sudo)"
        exit 1
    fi
}

check_ubuntu() {
    if [ ! -f /etc/os-release ]; then
        log_error "Cannot detect OS version"
        exit 1
    fi

    source /etc/os-release

    if [ "$ID" != "ubuntu" ]; then
        log_error "This script is designed for Ubuntu. Detected: $ID"
        exit 1
    fi

    log_info "Detected: Ubuntu ${VERSION_ID}"
}

check_resources() {
    local mem_total=$(free -m | awk '/^Mem:/{print $2}')
    local disk_free=$(df -m / | awk 'NR==2{print $4}')

    log_info "System resources: ${mem_total}MB RAM, ${disk_free}MB disk free"

    if [ "$mem_total" -lt 512 ]; then
        log_warn "Low memory detected (${mem_total}MB). Recommended: 1GB+"
    fi

    if [ "$disk_free" -lt 2048 ]; then
        log_warn "Low disk space (${disk_free}MB). Recommended: 5GB+"
    fi
}

# ============================================================================
# DEPENDENCY INSTALLATION
# ============================================================================

install_system_deps() {
    log_info "Updating package lists..."
    retry_command "apt-get update -qq" "Package list update"

    log_info "Installing system dependencies..."

    local packages=(
        python3.12
        python3.12-venv
        python3.12-dev
        python3-pip
        ffmpeg
        git
        curl
        wget
        ufw
        fail2ban
        htop
        jq
        unzip
        ca-certificates
        gnupg
        lsb-release
    )

    retry_command "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ${packages[*]}" "System package installation"

    # Install yt-dlp from official release (latest version)
    log_info "Installing yt-dlp..."
    retry_command "curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp && chmod a+rx /usr/local/bin/yt-dlp" "yt-dlp installation"

    log_success "System dependencies installed"
}

# ============================================================================
# USER AND DIRECTORY SETUP
# ============================================================================

setup_user() {
    if id "${APP_USER}" &>/dev/null; then
        log_info "User ${APP_USER} already exists"
    else
        log_info "Creating user ${APP_USER}..."
        useradd --system --shell /bin/bash --home-dir "${APP_DIR}" --create-home "${APP_USER}"
        log_success "User ${APP_USER} created"
    fi
}

setup_directories() {
    log_info "Setting up directories..."

    mkdir -p "${APP_DIR}"
    mkdir -p "${LOG_DIR}"
    mkdir -p "${TEMP_DIR}"
    mkdir -p "${APP_DIR}/backups"

    chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
    chown -R "${APP_USER}:${APP_USER}" "${LOG_DIR}"
    chown -R "${APP_USER}:${APP_USER}" "${TEMP_DIR}"

    chmod 755 "${APP_DIR}"
    chmod 755 "${LOG_DIR}"
    chmod 1777 "${TEMP_DIR}"

    log_success "Directories configured"
}

# ============================================================================
# APPLICATION DEPLOYMENT
# ============================================================================

clone_repository() {
    log_info "Cloning repository..."

    if [ -d "${APP_DIR}/.git" ]; then
        log_info "Repository exists, pulling latest changes..."
        cd "${APP_DIR}"

        # Backup current .env if exists
        if [ -f "${APP_DIR}/.env" ]; then
            cp "${APP_DIR}/.env" "${APP_DIR}/backups/.env.backup.$(date +%Y%m%d%H%M%S)"
        fi

        retry_command "sudo -u ${APP_USER} git fetch origin && sudo -u ${APP_USER} git reset --hard origin/${BRANCH}" "Git pull"
    else
        # Fresh clone
        rm -rf "${APP_DIR:?}/"*
        retry_command "sudo -u ${APP_USER} git clone --branch ${BRANCH} ${REPO_URL} ${APP_DIR}" "Git clone"
    fi

    chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
    log_success "Repository ready"
}

setup_virtualenv() {
    log_info "Setting up Python virtual environment..."

    cd "${APP_DIR}"

    if [ ! -d "${VENV_DIR}" ]; then
        sudo -u "${APP_USER}" python3.12 -m venv "${VENV_DIR}"
    fi

    # Upgrade pip
    sudo -u "${APP_USER}" "${VENV_DIR}/bin/pip" install --upgrade pip wheel setuptools

    # Install requirements
    retry_command "sudo -u ${APP_USER} ${VENV_DIR}/bin/pip install -r ${APP_DIR}/requirements.txt" "Python dependencies installation"

    log_success "Virtual environment configured"
}

# ============================================================================
# ENVIRONMENT CONFIGURATION
# ============================================================================

configure_environment() {
    log_info "Configuring environment..."

    # Generate a secure API key if not provided
    local api_key=$(openssl rand -hex 32)

    # Prompt for required values
    echo ""
    echo -e "${YELLOW}============================================${NC}"
    echo -e "${YELLOW}  ENVIRONMENT CONFIGURATION${NC}"
    echo -e "${YELLOW}============================================${NC}"
    echo ""

    # Check if .env already exists
    if [ -f "${APP_DIR}/.env" ]; then
        read -p "Existing .env found. Overwrite? (y/N): " overwrite
        if [ "$overwrite" != "y" ] && [ "$overwrite" != "Y" ]; then
            log_info "Keeping existing .env configuration"
            return 0
        fi
    fi

    read -p "Supabase URL (e.g., https://xxx.supabase.co): " supabase_url
    read -p "Supabase Service Role Key: " supabase_key
    read -p "Internal API Key [auto-generated]: " custom_api_key

    api_key="${custom_api_key:-$api_key}"

    cat > "${APP_DIR}/.env" << EOF
# SafePlay YT-DLP Service Configuration
# Generated: $(date -Iseconds)

# Server
PORT=${SERVICE_PORT}
ENVIRONMENT=production
HOST=0.0.0.0

# Supabase
SUPABASE_URL=${supabase_url}
SUPABASE_SERVICE_KEY=${supabase_key}
STORAGE_BUCKET=videos

# OxyLabs Proxy
OXYLABS_USERNAME=cleantube_S6y8C
OXYLABS_PASSWORD=fDv~7ZH~Wr+8qj

# Internal API Key (share with orchestration service)
API_KEY=${api_key}

# Temp storage
TEMP_DIR=${TEMP_DIR}
EOF

    chmod 600 "${APP_DIR}/.env"
    chown "${APP_USER}:${APP_USER}" "${APP_DIR}/.env"

    echo ""
    log_success "Environment configured"
    echo ""
    echo -e "${GREEN}Your Internal API Key is:${NC}"
    echo -e "${YELLOW}${api_key}${NC}"
    echo ""
    echo -e "${YELLOW}Save this key! You'll need it for the Orchestration Service.${NC}"
    echo ""
}

# ============================================================================
# SYSTEMD SERVICE
# ============================================================================

setup_systemd() {
    log_info "Configuring systemd service..."

    cat > /etc/systemd/system/${APP_NAME}.service << EOF
[Unit]
Description=SafePlay YT-DLP Download Service
Documentation=https://github.com/matbram/safeplay-yt-dlp
After=network.target network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=exec
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
Environment="PATH=${VENV_DIR}/bin:/usr/local/bin:/usr/bin:/bin"
EnvironmentFile=${APP_DIR}/.env
ExecStart=${VENV_DIR}/bin/uvicorn app.main:app --host 0.0.0.0 --port ${SERVICE_PORT} --workers 2
ExecReload=/bin/kill -HUP \$MAINPID

# Logging
StandardOutput=append:${LOG_DIR}/app.log
StandardError=append:${LOG_DIR}/error.log

# Security hardening
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=${APP_DIR} ${LOG_DIR} ${TEMP_DIR}

# Restart policy (self-healing)
Restart=always
RestartSec=5
TimeoutStartSec=30
TimeoutStopSec=30

# Resource limits
LimitNOFILE=65535
LimitNPROC=4096

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable ${APP_NAME}

    log_success "Systemd service configured"
}

# ============================================================================
# SELF-HEALING COMPONENTS
# ============================================================================

setup_health_monitor() {
    log_info "Setting up health monitoring..."

    # Create health check script
    cat > /usr/local/bin/safeplay-health-check << 'HEALTHEOF'
#!/bin/bash
# SafePlay Health Check Script

readonly SERVICE="safeplay-ytdlp"
readonly PORT="3002"
readonly LOG_FILE="/var/log/safeplay/health-check.log"
readonly MAX_RESTART_ATTEMPTS=3
readonly RESTART_COOLDOWN=300

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

check_service_running() {
    systemctl is-active --quiet "$SERVICE"
}

check_http_health() {
    local response
    response=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "http://127.0.0.1:${PORT}/health" 2>/dev/null)
    [ "$response" = "200" ]
}

check_memory_usage() {
    local mem_percent
    mem_percent=$(free | awk '/Mem/{printf("%.0f"), $3/$2*100}')
    [ "$mem_percent" -lt 90 ]
}

check_disk_space() {
    local disk_percent
    disk_percent=$(df / | awk 'NR==2{print int($5)}')
    [ "$disk_percent" -lt 90 ]
}

cleanup_temp_files() {
    # Remove temp files older than 2 hours
    find /tmp/safeplay-downloads -type f -mmin +120 -delete 2>/dev/null
    find /tmp/safeplay-downloads -type d -empty -delete 2>/dev/null
}

restart_service() {
    log "Attempting service restart..."
    systemctl restart "$SERVICE"
    sleep 10

    if check_service_running && check_http_health; then
        log "Service successfully restarted"
        return 0
    else
        log "Service restart failed"
        return 1
    fi
}

main() {
    local issues=0

    # Check if service is running
    if ! check_service_running; then
        log "ERROR: Service not running"
        issues=$((issues + 1))
    fi

    # Check HTTP endpoint
    if check_service_running && ! check_http_health; then
        log "ERROR: HTTP health check failed"
        issues=$((issues + 1))
    fi

    # Check resources
    if ! check_memory_usage; then
        log "WARN: High memory usage detected"
        cleanup_temp_files
    fi

    if ! check_disk_space; then
        log "WARN: Low disk space detected"
        cleanup_temp_files
    fi

    # Self-heal if issues detected
    if [ "$issues" -gt 0 ]; then
        log "Issues detected, attempting self-heal..."
        cleanup_temp_files
        restart_service
    fi

    # Always cleanup old temp files
    cleanup_temp_files
}

main "$@"
HEALTHEOF

    chmod +x /usr/local/bin/safeplay-health-check

    # Create cron job for health checks
    cat > /etc/cron.d/safeplay-health << EOF
# SafePlay health check - runs every 2 minutes
*/2 * * * * root /usr/local/bin/safeplay-health-check
EOF

    chmod 644 /etc/cron.d/safeplay-health

    log_success "Health monitoring configured"
}

setup_log_rotation() {
    log_info "Configuring log rotation..."

    cat > /etc/logrotate.d/safeplay << EOF
${LOG_DIR}/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0644 ${APP_USER} ${APP_USER}
    postrotate
        systemctl reload ${APP_NAME} > /dev/null 2>&1 || true
    endscript
}
EOF

    log_success "Log rotation configured"
}

setup_auto_update() {
    log_info "Setting up auto-update mechanism..."

    # Create update script
    cat > /usr/local/bin/safeplay-update << 'UPDATEEOF'
#!/bin/bash
# SafePlay Auto-Update Script

readonly APP_DIR="/opt/safeplay-ytdlp"
readonly VENV_DIR="${APP_DIR}/venv"
readonly SERVICE="safeplay-ytdlp"
readonly LOG_FILE="/var/log/safeplay/update.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

cd "$APP_DIR" || exit 1

# Check for updates
log "Checking for updates..."
git fetch origin main

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    log "Already up to date"
    exit 0
fi

log "Update available, deploying..."

# Backup current state
cp .env backups/.env.backup.$(date +%Y%m%d%H%M%S) 2>/dev/null || true

# Pull changes
git reset --hard origin/main

# Update dependencies
"${VENV_DIR}/bin/pip" install -r requirements.txt --quiet

# Update yt-dlp
curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp 2>/dev/null
chmod a+rx /usr/local/bin/yt-dlp

# Restart service
systemctl restart "$SERVICE"

log "Update completed successfully"
UPDATEEOF

    chmod +x /usr/local/bin/safeplay-update

    # Weekly update check
    cat > /etc/cron.d/safeplay-update << EOF
# Check for SafePlay updates weekly (Sunday 3 AM)
0 3 * * 0 root /usr/local/bin/safeplay-update
# Update yt-dlp daily (4 AM)
0 4 * * * root curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp && chmod a+rx /usr/local/bin/yt-dlp
EOF

    chmod 644 /etc/cron.d/safeplay-update

    log_success "Auto-update configured"
}

# ============================================================================
# SECURITY CONFIGURATION
# ============================================================================

setup_firewall() {
    log_info "Configuring firewall..."

    # Reset UFW
    ufw --force reset

    # Default policies
    ufw default deny incoming
    ufw default allow outgoing

    # Allow SSH
    ufw allow 22/tcp comment 'SSH'

    # Allow service port (restrict to known IPs if possible)
    ufw allow ${SERVICE_PORT}/tcp comment 'SafePlay YT-DLP Service'

    # Enable UFW
    ufw --force enable

    log_success "Firewall configured"
}

setup_fail2ban() {
    log_info "Configuring fail2ban..."

    cat > /etc/fail2ban/jail.local << EOF
[DEFAULT]
bantime = 3600
findtime = 600
maxretry = 5

[sshd]
enabled = true
port = ssh
logpath = %(sshd_log)s
backend = %(sshd_backend)s
maxretry = 3
EOF

    systemctl enable fail2ban
    systemctl restart fail2ban

    log_success "Fail2ban configured"
}

# ============================================================================
# DIAGNOSTIC TOOLS
# ============================================================================

create_diagnostic_tools() {
    log_info "Creating diagnostic tools..."

    # Status command
    cat > /usr/local/bin/safeplay-status << 'STATUSEOF'
#!/bin/bash
echo "============================================"
echo "  SafePlay YT-DLP Service Status"
echo "============================================"
echo ""
echo "Service Status:"
systemctl status safeplay-ytdlp --no-pager -l
echo ""
echo "Recent Logs:"
tail -20 /var/log/safeplay/app.log 2>/dev/null || echo "No logs yet"
echo ""
echo "Health Check:"
curl -s http://127.0.0.1:3002/health | jq . 2>/dev/null || echo "Service not responding"
echo ""
echo "Resource Usage:"
echo "  Memory: $(free -h | awk '/^Mem:/{print $3 "/" $2}')"
echo "  Disk: $(df -h / | awk 'NR==2{print $3 "/" $2 " (" $5 " used)"}')"
echo "  Temp files: $(du -sh /tmp/safeplay-downloads 2>/dev/null | cut -f1 || echo "0")"
STATUSEOF

    chmod +x /usr/local/bin/safeplay-status

    # Logs command
    cat > /usr/local/bin/safeplay-logs << 'LOGSEOF'
#!/bin/bash
case "${1:-app}" in
    app)
        tail -f /var/log/safeplay/app.log
        ;;
    error)
        tail -f /var/log/safeplay/error.log
        ;;
    health)
        tail -f /var/log/safeplay/health-check.log
        ;;
    all)
        tail -f /var/log/safeplay/*.log
        ;;
    *)
        echo "Usage: safeplay-logs [app|error|health|all]"
        ;;
esac
LOGSEOF

    chmod +x /usr/local/bin/safeplay-logs

    # Restart command
    cat > /usr/local/bin/safeplay-restart << 'RESTARTEOF'
#!/bin/bash
echo "Restarting SafePlay YT-DLP Service..."
systemctl restart safeplay-ytdlp
sleep 3
systemctl status safeplay-ytdlp --no-pager
RESTARTEOF

    chmod +x /usr/local/bin/safeplay-restart

    log_success "Diagnostic tools created"
}

# ============================================================================
# MAIN INSTALLATION FLOW
# ============================================================================

print_banner() {
    echo ""
    echo -e "${BLUE}============================================${NC}"
    echo -e "${BLUE}  SafePlay YT-DLP Service Installer${NC}"
    echo -e "${BLUE}  Ubuntu 24.04 LTS - Self-Healing Edition${NC}"
    echo -e "${BLUE}============================================${NC}"
    echo ""
}

print_completion() {
    local droplet_ip=$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')

    echo ""
    echo -e "${GREEN}============================================${NC}"
    echo -e "${GREEN}  INSTALLATION COMPLETE!${NC}"
    echo -e "${GREEN}============================================${NC}"
    echo ""
    echo -e "Service URL: ${YELLOW}http://${droplet_ip}:${SERVICE_PORT}${NC}"
    echo ""
    echo -e "Useful Commands:"
    echo -e "  ${BLUE}safeplay-status${NC}  - Check service status"
    echo -e "  ${BLUE}safeplay-logs${NC}    - View logs (app/error/health/all)"
    echo -e "  ${BLUE}safeplay-restart${NC} - Restart the service"
    echo -e "  ${BLUE}safeplay-update${NC}  - Manually trigger update"
    echo ""
    echo -e "Test the service:"
    echo -e "  ${BLUE}curl http://localhost:${SERVICE_PORT}/health | jq${NC}"
    echo ""
    echo -e "${YELLOW}IMPORTANT:${NC}"
    echo -e "  1. Save your API key shown above"
    echo -e "  2. Configure your Orchestration Service to call:"
    echo -e "     ${YELLOW}http://${droplet_ip}:${SERVICE_PORT}/api/download${NC}"
    echo -e "  3. Consider setting up a private network between services"
    echo ""
}

main() {
    print_banner

    # Pre-flight checks
    check_root
    check_ubuntu
    check_resources

    # Installation
    install_system_deps
    setup_user
    setup_directories
    clone_repository
    setup_virtualenv
    configure_environment

    # Service configuration
    setup_systemd
    setup_health_monitor
    setup_log_rotation
    setup_auto_update

    # Security
    setup_firewall
    setup_fail2ban

    # Tools
    create_diagnostic_tools

    # Start service
    log_info "Starting service..."
    systemctl start ${APP_NAME}
    sleep 5

    # Verify
    if systemctl is-active --quiet ${APP_NAME}; then
        log_success "Service started successfully"
    else
        log_error "Service failed to start. Check logs: journalctl -u ${APP_NAME}"
        exit 1
    fi

    print_completion
}

# Run main installation
main "$@"
