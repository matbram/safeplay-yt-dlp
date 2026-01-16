#!/bin/bash
#
# Setup script for bgutil PO Token Server
# This enables Tier 1 downloads (no proxy needed) for ~56% cost savings
#
# Run as root: sudo bash setup-bgutil.sh
#

set -e

echo "=========================================="
echo "bgutil PO Token Server Setup"
echo "=========================================="

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "ERROR: Please run as root (sudo bash setup-bgutil.sh)"
    exit 1
fi

# Step 1: Install Docker if not present
echo ""
echo "[1/5] Checking Docker installation..."
if ! command -v docker &> /dev/null; then
    echo "Docker not found. Installing..."
    apt-get update
    apt-get install -y docker.io
    systemctl enable docker
    systemctl start docker
    echo "Docker installed successfully."
else
    echo "Docker already installed: $(docker --version)"
fi

# Step 2: Pull the bgutil image
echo ""
echo "[2/5] Pulling bgutil-ytdlp-pot-provider image..."
docker pull brainicism/bgutil-ytdlp-pot-provider:latest

# Step 3: Install systemd service
echo ""
echo "[3/5] Installing systemd service..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "$SCRIPT_DIR/bgutil-server.service" /etc/systemd/system/bgutil-server.service
systemctl daemon-reload

# Step 4: Enable and start the service
echo ""
echo "[4/5] Enabling and starting bgutil-server service..."
systemctl enable bgutil-server
systemctl start bgutil-server

# Step 5: Verify it's running
echo ""
echo "[5/5] Verifying service..."
sleep 3

if systemctl is-active --quiet bgutil-server; then
    echo "Service is running!"

    # Test the endpoint
    echo ""
    echo "Testing PO token server..."
    if curl -s http://127.0.0.1:4416/ping | grep -q "version"; then
        echo "PO token server is responding correctly!"
        echo ""
        echo "=========================================="
        echo "SUCCESS! bgutil PO Token Server is ready."
        echo "=========================================="
        echo ""
        echo "Tier 1 downloads (no proxy) are now enabled."
        echo "Expected cost savings: ~56%"
        echo ""
        echo "Management commands:"
        echo "  systemctl status bgutil-server   # Check status"
        echo "  systemctl restart bgutil-server  # Restart"
        echo "  journalctl -u bgutil-server -f   # View logs"
        echo ""
    else
        echo "WARNING: Server started but /ping not responding yet."
        echo "It may take a minute to fully initialize."
        echo "Check with: curl http://127.0.0.1:4416/ping"
    fi
else
    echo "ERROR: Service failed to start!"
    echo "Check logs with: journalctl -u bgutil-server -n 50"
    exit 1
fi
