#!/usr/bin/env bash
# DriveGuard Agent Installer — Ubuntu 20.04+ / Debian 11+
# curl install: bash <(curl -fsSL https://drive-guard.base44.app/functions/agentInstall) --token <TOKEN>
# local install: sudo bash install.sh --token <LICENSE_TOKEN> [--api-url <URL>]
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()     { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ── Parse arguments ──────────────────────────────────────────────────────────
LICENSE_TOKEN=""
API_URL="https://drive-guard.base44.app"

while [[ $# -gt 0 ]]; do
  case $1 in
    --token)   LICENSE_TOKEN="$2"; shift 2 ;;
    --api-url) API_URL="$2";        shift 2 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ -z "$LICENSE_TOKEN" ]] && die "License token required. Usage: sudo bash install.sh --token <TOKEN>"
[[ $EUID -ne 0 ]]         && die "This installer must be run as root (sudo)."

AGENT_DIR="/opt/driveguard"
CONFIG_DIR="/etc/driveguard"
LOG_DIR="/var/log/driveguard"
SERVICE_NAME="driveguard-agent"

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║       DriveGuard Agent Installer         ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
echo ""

# ── 1. Install system dependencies ───────────────────────────────────────────
info "Updating package lists..."
apt-get update -qq

info "Installing dependencies..."
PACKAGES=(
  python3 python3-pip python3-venv
  smartmontools          # smartctl — open-source HD health
  hdparm                 # ATA Secure Erase + NIST 800-88
  nvme-cli               # NVMe Secure Erase
  sg3-utils              # SAS/SCSI drives
  lshw pciutils usbutils # Hardware enumeration
  curl jq util-linux     # Misc tools
  dmidecode              # System info
  parted                 # Partition detection
  bc                     # Math for progress
  fio                    # Drive surface/stress testing
)
apt-get install -y --no-install-recommends "${PACKAGES[@]}" > /dev/null
success "System packages installed."

# ── 2. Create directories & user ─────────────────────────────────────────────
mkdir -p "$AGENT_DIR" "$CONFIG_DIR" "$LOG_DIR"

if ! id -u driveguard &>/dev/null; then
  useradd --system --no-create-home --shell /usr/sbin/nologin driveguard
  usermod -aG disk driveguard
fi

# ── 3. Download or copy agent source ─────────────────────────────────────────
info "Deploying agent source..."

# If running from a local directory that already has the agent files, copy them.
# Otherwise download them from the platform.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -f "$SCRIPT_DIR/driveguard_agent.py" ]]; then
  cp "$SCRIPT_DIR/driveguard_agent.py" "$AGENT_DIR/driveguard_agent.py"
  cp "$SCRIPT_DIR/wipe_engine.py"      "$AGENT_DIR/wipe_engine.py"
  success "Agent files copied from local directory."
else
  info "Downloading agent files..."
  AGENT_BASE="https://raw.githubusercontent.com/driveguard/agent/main"
  curl -fsSL "${AGENT_BASE}/driveguard_agent.py" -o "$AGENT_DIR/driveguard_agent.py"
  curl -fsSL "${AGENT_BASE}/wipe_engine.py"      -o "$AGENT_DIR/wipe_engine.py"
  success "Agent files downloaded."
fi

chmod +x "$AGENT_DIR/driveguard_agent.py"
chmod +x "$AGENT_DIR/wipe_engine.py"

# ── 4. Python virtual environment ─────────────────────────────────────────────
info "Creating Python virtual environment..."
python3 -m venv "$AGENT_DIR/venv" > /dev/null
"$AGENT_DIR/venv/bin/pip" install --quiet --upgrade pip
"$AGENT_DIR/venv/bin/pip" install --quiet requests psutil
success "Python environment ready."

# ── 5. Write config ───────────────────────────────────────────────────────────
HOSTNAME=$(hostname)
SYSTEM_ID=$(cat /sys/class/dmi/id/product_uuid 2>/dev/null || hostname)

cat > "$CONFIG_DIR/agent.conf" <<EOF
[agent]
license_token = ${LICENSE_TOKEN}
api_url       = ${API_URL}
hostname      = ${HOSTNAME}
system_id     = ${SYSTEM_ID}
poll_interval = 10
log_level     = INFO

[wipe]
default_standard = NIST_800_88
passes           = 1
verify           = true
EOF

chmod 600 "$CONFIG_DIR/agent.conf"
success "Config written to $CONFIG_DIR/agent.conf"

# ── 6. Install systemd service ────────────────────────────────────────────────
info "Installing systemd service..."
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=DriveGuard Hardware Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart=${AGENT_DIR}/venv/bin/python3 ${AGENT_DIR}/driveguard_agent.py --config ${CONFIG_DIR}/agent.conf
Restart=always
RestartSec=10
StandardOutput=append:${LOG_DIR}/agent.log
StandardError=append:${LOG_DIR}/agent.error.log

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable  "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
success "Service ${SERVICE_NAME} enabled and started."

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   DriveGuard Agent installed!            ║${NC}"
echo -e "${GREEN}║   Check status: systemctl status driveguard-agent ║${NC}"
echo -e "${GREEN}║   Logs: tail -f ${LOG_DIR}/agent.log      ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
echo ""
