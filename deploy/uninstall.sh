#!/bin/bash
# ============================================================================
# SafePlay YT-DLP Service - Uninstall Script
# ============================================================================

set -euo pipefail

readonly APP_NAME="safeplay-ytdlp"
readonly APP_USER="safeplay"
readonly APP_DIR="/opt/safeplay-ytdlp"
readonly LOG_DIR="/var/log/safeplay"
readonly TEMP_DIR="/tmp/safeplay-downloads"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo ""
echo -e "${YELLOW}============================================${NC}"
echo -e "${YELLOW}  SafePlay YT-DLP Service Uninstaller${NC}"
echo -e "${YELLOW}============================================${NC}"
echo ""

if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}This script must be run as root${NC}"
    exit 1
fi

read -p "Are you sure you want to uninstall SafePlay YT-DLP Service? (y/N): " confirm
if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
    echo "Uninstall cancelled"
    exit 0
fi

echo ""
echo "Stopping service..."
systemctl stop ${APP_NAME} 2>/dev/null || true
systemctl disable ${APP_NAME} 2>/dev/null || true

echo "Removing systemd service..."
rm -f /etc/systemd/system/${APP_NAME}.service
systemctl daemon-reload

echo "Removing cron jobs..."
rm -f /etc/cron.d/safeplay-health
rm -f /etc/cron.d/safeplay-update

echo "Removing log rotation config..."
rm -f /etc/logrotate.d/safeplay

echo "Removing diagnostic tools..."
rm -f /usr/local/bin/safeplay-status
rm -f /usr/local/bin/safeplay-logs
rm -f /usr/local/bin/safeplay-restart
rm -f /usr/local/bin/safeplay-update
rm -f /usr/local/bin/safeplay-health-check

echo "Removing application files..."
rm -rf "${APP_DIR}"
rm -rf "${LOG_DIR}"
rm -rf "${TEMP_DIR}"

read -p "Remove user '${APP_USER}'? (y/N): " remove_user
if [ "$remove_user" = "y" ] || [ "$remove_user" = "Y" ]; then
    userdel -r ${APP_USER} 2>/dev/null || true
    echo "User removed"
fi

echo ""
echo -e "${GREEN}Uninstall complete!${NC}"
echo ""
echo "Note: The following were NOT removed:"
echo "  - UFW firewall rules (run: ufw delete allow 3002/tcp)"
echo "  - fail2ban configuration"
echo "  - System packages (python3, ffmpeg, etc.)"
echo "  - yt-dlp (/usr/local/bin/yt-dlp)"
echo ""
