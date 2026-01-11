#!/bin/bash
# Install SafePlay helper commands
# Run as root: sudo bash /opt/safeplay-ytdlp/deploy/install-commands.sh

set -e

echo "Installing SafePlay helper commands..."

# Create safeplay-update command
cat > /usr/local/bin/safeplay-update << 'EOF'
#!/bin/bash
set -e
echo "Updating SafePlay YT-DLP service..."
cd /opt/safeplay-ytdlp
sudo -u safeplay git fetch origin
sudo -u safeplay git reset --hard origin/main
echo "Restarting service..."
sudo systemctl restart safeplay-ytdlp
echo "Done! Checking status..."
sudo systemctl status safeplay-ytdlp --no-pager
EOF

# Create safeplay-status command
cat > /usr/local/bin/safeplay-status << 'EOF'
#!/bin/bash
echo "=== SafePlay YT-DLP Service Status ==="
systemctl status safeplay-ytdlp --no-pager
echo ""
echo "=== Recent Logs ==="
journalctl -u safeplay-ytdlp -n 20 --no-pager
EOF

# Create safeplay-logs command
cat > /usr/local/bin/safeplay-logs << 'EOF'
#!/bin/bash
# Show logs, optionally follow with -f
if [ "$1" == "-f" ]; then
    journalctl -u safeplay-ytdlp -f
else
    journalctl -u safeplay-ytdlp -n ${1:-50} --no-pager
fi
EOF

# Create safeplay-restart command
cat > /usr/local/bin/safeplay-restart << 'EOF'
#!/bin/bash
echo "Restarting SafePlay YT-DLP service..."
sudo systemctl restart safeplay-ytdlp
sleep 2
systemctl status safeplay-ytdlp --no-pager
EOF

# Create safeplay-health command
cat > /usr/local/bin/safeplay-health << 'EOF'
#!/bin/bash
echo "=== Health Check ==="
curl -s http://localhost:3002/health | python3 -m json.tool 2>/dev/null || curl -s http://localhost:3002/health
echo ""
echo ""
echo "=== Service Status ==="
systemctl is-active safeplay-ytdlp && echo "Service: RUNNING" || echo "Service: STOPPED"
EOF

# Make all commands executable
chmod +x /usr/local/bin/safeplay-update
chmod +x /usr/local/bin/safeplay-status
chmod +x /usr/local/bin/safeplay-logs
chmod +x /usr/local/bin/safeplay-restart
chmod +x /usr/local/bin/safeplay-health

echo ""
echo "✓ Installed commands:"
echo "  - safeplay-update   : Pull latest code and restart"
echo "  - safeplay-status   : Show service status and recent logs"
echo "  - safeplay-logs     : Show logs (use -f to follow)"
echo "  - safeplay-restart  : Restart the service"
echo "  - safeplay-health   : Check health endpoint"
echo ""
