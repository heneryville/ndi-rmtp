#!/usr/bin/env bash
# Raspberry Pi 5 production installer. Idempotent. Run as root.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run as root: sudo $0"; exit 1; }
[[ "$(uname -m)" == "aarch64" ]] || { echo "Expected aarch64, got $(uname -m)"; exit 1; }

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_ROOT="/opt/ndi-rmtp"
SVC_USER="ndi-rmtp"

say() { printf "\n\033[1;34m==>\033[0m %s\n" "$*"; }

say "Installing apt dependencies"
apt-get update
apt-get install -y --no-install-recommends \
    build-essential cmake git pkg-config curl ca-certificates rsync \
    libavformat-dev libavcodec-dev libavutil-dev libswresample-dev libswscale-dev \
    ffmpeg python3.11 python3.11-venv librsvg2-bin

# --- uv as the service user ---
say "Ensuring service user $SVC_USER exists"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$SVC_USER"

say "Installing uv (system-wide, into /usr/local/bin)"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh
fi

# --- NDI SDK ---
NDI_HEADER=/usr/local/include/ndi/Processing.NDI.Lib.h
if [[ ! -f "$NDI_HEADER" ]]; then
  cat <<EOF
NDI SDK for Linux ARM64 not yet installed.

  1. Download "NDI SDK for Linux" from https://ndi.video/sdk/ (accept EULA)
  2. Extract and run the installer (it'll prompt for EULA acceptance).
  3. Copy the aarch64 library and headers:
       sudo cp -r 'NDI SDK for Linux'/lib/aarch64-rpi4-linux-gnueabi/* /usr/local/lib/
       sudo mkdir -p /usr/local/include/ndi
       sudo cp 'NDI SDK for Linux'/include/*.h /usr/local/include/ndi/
       sudo ldconfig
  4. Re-run this script.

EOF
  exit 1
fi

# --- Build ndi-streamer (the binary is ndi-streamer; the repo is ndi_streamer) ---
say "Building ndi-streamer"
BUILD_DIR="$INSTALL_ROOT/build/ndi_streamer"
mkdir -p "$INSTALL_ROOT/build"
if [[ ! -d "$BUILD_DIR/.git" ]]; then
  git clone https://github.com/alim-zanibekov/ndi_streamer.git "$BUILD_DIR"
else
  git -C "$BUILD_DIR" pull --ff-only
fi
# The CMakeLists checks NDI_SDK_DIR env var when on Linux.
NDI_SDK_DIR="${NDI_SDK_DIR:-/usr/local}" cmake -B "$BUILD_DIR/build" -S "$BUILD_DIR" -DNDI_SDK_DIR="${NDI_SDK_DIR:-/usr/local}"
cmake --build "$BUILD_DIR/build" --parallel
install -m 0755 "$BUILD_DIR/build/ndi-streamer" /usr/local/bin/ndi-streamer

# --- Install the Python app ---
say "Installing Python app to $INSTALL_ROOT"
mkdir -p "$INSTALL_ROOT/app"
rsync -a --delete \
    --exclude '.venv' --exclude 'build' --exclude 'bin' --exclude '.git' \
    --exclude '__pycache__' --exclude '.pytest_cache' \
    "$PROJECT_ROOT/" "$INSTALL_ROOT/app/"
chown -R "$SVC_USER:$SVC_USER" "$INSTALL_ROOT/app"
cd "$INSTALL_ROOT/app"
sudo -u "$SVC_USER" -H env "PATH=/usr/local/bin:$PATH" uv sync

# --- Config dir for the service user ---
say "Preparing config directory"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 0700 "/home/$SVC_USER/.config/ndi-rmtp"

# --- systemd unit ---
say "Installing systemd unit"
install -m 0644 "$PROJECT_ROOT/scripts/ndi-rmtp.service" /etc/systemd/system/ndi-rmtp.service
systemctl daemon-reload
systemctl enable ndi-rmtp.service
systemctl restart ndi-rmtp.service

# --- Surface the first-run password ---
sleep 2
say "Reading bootstrap password from service log (if just created)"
journalctl -u ndi-rmtp.service -n 50 --no-pager | grep -A2 "first-run bootstrap" || true

HOSTNAME_LOCAL="$(hostname).local"
cat <<EOF

==> Done. Web UI: http://${HOSTNAME_LOCAL}:8080

  - systemctl status ndi-rmtp
  - journalctl -u ndi-rmtp -f

The first-run admin password was printed above (and once on boot in
journalctl). If you missed it, delete the config and restart the service:

  sudo -u $SVC_USER rm -f /home/$SVC_USER/.config/ndi-rmtp/config.toml
  sudo systemctl restart ndi-rmtp

EOF
