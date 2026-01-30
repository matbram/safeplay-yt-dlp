#!/bin/bash
# SafePlay AI Agent Installation Script
# This script sets up the self-healing AI agent for the YouTube downloader

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}============================================${NC}"
echo -e "${BLUE}  SafePlay AI Agent Installation Script${NC}"
echo -e "${BLUE}============================================${NC}"
echo ""

# Configuration
REPO_PATH="${REPO_PATH:-/opt/safeplay-ytdlp}"
VENV_PATH="${REPO_PATH}/venv"
SERVICE_USER="${SERVICE_USER:-safeplay}"
DOWNLOADER_SERVICE="${DOWNLOADER_SERVICE:-safeplay-ytdlp}"

# Check if running as root for system operations
check_sudo() {
    if [ "$EUID" -ne 0 ]; then
        echo -e "${YELLOW}Note: Some operations require sudo. You may be prompted for password.${NC}"
    fi
}

# Step 1: Check prerequisites
echo -e "${GREEN}[1/8] Checking prerequisites...${NC}"

if [ ! -d "$REPO_PATH" ]; then
    echo -e "${RED}Error: Repository not found at $REPO_PATH${NC}"
    exit 1
fi

if [ ! -f "$REPO_PATH/.env" ]; then
    echo -e "${YELLOW}Warning: .env file not found. Copying from .env.example...${NC}"
    cp "$REPO_PATH/.env.example" "$REPO_PATH/.env"
    echo -e "${YELLOW}Please edit $REPO_PATH/.env with your API keys before starting the agent.${NC}"
fi

# Check Python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Error: Python 3 is required but not installed.${NC}"
    exit 1
fi

echo -e "  Python: $(python3 --version)"

# Step 2: Create/update virtual environment
echo -e "${GREEN}[2/8] Setting up Python virtual environment...${NC}"

if [ ! -d "$VENV_PATH" ]; then
    echo "  Creating virtual environment..."
    python3 -m venv "$VENV_PATH"
fi

source "$VENV_PATH/bin/activate"

# Step 3: Install dependencies
echo -e "${GREEN}[3/8] Installing Python dependencies...${NC}"
pip install --upgrade pip -q
pip install -r "$REPO_PATH/requirements.txt" -q
echo "  Dependencies installed."

# Step 4: Create log directory
echo -e "${GREEN}[4/8] Creating log directory...${NC}"
sudo mkdir -p /var/log/safeplay-agent
sudo chown -R ${SERVICE_USER}:${SERVICE_USER} /var/log/safeplay-agent 2>/dev/null || true
echo "  Log directory: /var/log/safeplay-agent"

# Step 5: Configure sudoers for service restart
echo -e "${GREEN}[5/8] Configuring sudoers for service restart...${NC}"

SUDOERS_FILE="/etc/sudoers.d/safeplay-agent"
SUDOERS_CONTENT="${SERVICE_USER} ALL=(ALL) NOPASSWD: /bin/systemctl restart ${DOWNLOADER_SERVICE}
${SERVICE_USER} ALL=(ALL) NOPASSWD: /bin/systemctl status ${DOWNLOADER_SERVICE}
${SERVICE_USER} ALL=(ALL) NOPASSWD: /bin/systemctl restart safeplay-agent
${SERVICE_USER} ALL=(ALL) NOPASSWD: /bin/systemctl status safeplay-agent"

if [ ! -f "$SUDOERS_FILE" ]; then
    echo "$SUDOERS_CONTENT" | sudo tee "$SUDOERS_FILE" > /dev/null
    sudo chmod 0440 "$SUDOERS_FILE"
    echo "  Sudoers configured for passwordless service restart."
else
    echo "  Sudoers file already exists."
fi

# Step 6: Install systemd service
echo -e "${GREEN}[6/8] Installing systemd service...${NC}"

# Update the service file with correct paths
SERVICE_FILE="$REPO_PATH/deploy/safeplay-agent.service"
if [ -f "$SERVICE_FILE" ]; then
    # Create a temporary file with updated paths
    sudo cp "$SERVICE_FILE" /etc/systemd/system/safeplay-agent.service

    # Update paths in the service file
    sudo sed -i "s|/home/user/safeplay-yt-dlp|${REPO_PATH}|g" /etc/systemd/system/safeplay-agent.service
    sudo sed -i "s|User=safeplay|User=${SERVICE_USER}|g" /etc/systemd/system/safeplay-agent.service
    sudo sed -i "s|Group=safeplay|Group=${SERVICE_USER}|g" /etc/systemd/system/safeplay-agent.service

    sudo systemctl daemon-reload
    echo "  Systemd service installed."
else
    echo -e "${YELLOW}  Warning: Service file not found at $SERVICE_FILE${NC}"
fi

# Step 7: Create the agent/auto-fixes branch if it doesn't exist
echo -e "${GREEN}[7/8] Setting up git branch for auto-fixes...${NC}"

cd "$REPO_PATH"
if ! git show-ref --verify --quiet refs/heads/agent/auto-fixes; then
    git branch agent/auto-fixes 2>/dev/null || true
    echo "  Created branch: agent/auto-fixes"
else
    echo "  Branch agent/auto-fixes already exists."
fi

# Step 8: Verify configuration
echo -e "${GREEN}[8/8] Verifying configuration...${NC}"

# Check for required environment variables
source "$REPO_PATH/.env" 2>/dev/null || true

MISSING_VARS=""

if [ -z "$SUPABASE_URL" ] || [ "$SUPABASE_URL" = "https://xxx.supabase.co" ]; then
    MISSING_VARS="$MISSING_VARS SUPABASE_URL"
fi

if [ -z "$SUPABASE_SERVICE_KEY" ] || [[ "$SUPABASE_SERVICE_KEY" == eyJhbGc* ]]; then
    MISSING_VARS="$MISSING_VARS SUPABASE_SERVICE_KEY"
fi

if [ -z "$GOOGLE_API_KEY" ] && [ -z "$ANTHROPIC_API_KEY" ]; then
    MISSING_VARS="$MISSING_VARS (GOOGLE_API_KEY or ANTHROPIC_API_KEY)"
fi

if [ -n "$MISSING_VARS" ]; then
    echo -e "${YELLOW}  Warning: The following environment variables need to be configured:${NC}"
    echo -e "${YELLOW}  $MISSING_VARS${NC}"
    echo ""
    echo -e "${YELLOW}  Edit $REPO_PATH/.env to add your API keys.${NC}"
fi

# Summary
echo ""
echo -e "${BLUE}============================================${NC}"
echo -e "${GREEN}  Installation Complete!${NC}"
echo -e "${BLUE}============================================${NC}"
echo ""
echo "Next steps:"
echo ""
echo -e "  1. ${YELLOW}Run the Supabase schema:${NC}"
echo "     Copy the contents of schema/agent_schema.sql"
echo "     and run it in your Supabase SQL Editor."
echo ""
echo -e "  2. ${YELLOW}Configure your .env file:${NC}"
echo "     nano $REPO_PATH/.env"
echo "     - Add your GOOGLE_API_KEY or ANTHROPIC_API_KEY"
echo "     - Verify SUPABASE_URL and SUPABASE_SERVICE_KEY"
echo ""
echo -e "  3. ${YELLOW}Start the agent:${NC}"
echo "     sudo systemctl enable safeplay-agent"
echo "     sudo systemctl start safeplay-agent"
echo ""
echo -e "  4. ${YELLOW}Check agent status:${NC}"
echo "     sudo systemctl status safeplay-agent"
echo "     journalctl -u safeplay-agent -f"
echo ""
echo -e "  5. ${YELLOW}For website integration:${NC}"
echo "     See docs/WEBSITE-INTEGRATION.md"
echo ""

# Optional: Start the agent now
read -p "Would you like to enable and start the agent now? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    sudo systemctl enable safeplay-agent
    sudo systemctl start safeplay-agent
    echo ""
    echo -e "${GREEN}Agent started! Check status with:${NC}"
    echo "  sudo systemctl status safeplay-agent"
else
    echo ""
    echo "You can start the agent later with:"
    echo "  sudo systemctl enable safeplay-agent"
    echo "  sudo systemctl start safeplay-agent"
fi
