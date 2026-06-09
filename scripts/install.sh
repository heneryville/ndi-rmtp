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

# --- FFmpeg 7 for libndi's HX/HX3 decoder ---
# libndi 6.3.2 dlopens libavcodec.so.61 / libavutil.so.59 (FFmpeg 7) for HX
# and HX3 H.264/H.265 decode. Pi OS Bookworm only ships FFmpeg 5
# (libavcodec.so.59), so without this step, libndi falls back to a static
# "Video decoder not found" splash for any HX/HX3 source — including the
# UHD camera. We install jellyfin-ffmpeg7's prefixed build (in
# /usr/lib/jellyfin-ffmpeg) so it coexists with system FFmpeg 5, then add
# its lib dir to ldconfig so libndi's dlopen finds the .so.61 libs.
# Remove this workaround once Pi OS Trixie (FFmpeg 7) is the install target.
JF_FFMPEG_VERSION="7.1.4-3"
JF_FFMPEG_DEB="jellyfin-ffmpeg7_${JF_FFMPEG_VERSION}-bookworm_arm64.deb"
JF_FFMPEG_URL="https://github.com/jellyfin/jellyfin-ffmpeg/releases/download/v${JF_FFMPEG_VERSION}/${JF_FFMPEG_DEB}"
if ! dpkg -s jellyfin-ffmpeg7 >/dev/null 2>&1; then
  say "Installing jellyfin-ffmpeg7 ${JF_FFMPEG_VERSION} (provides libavcodec.so.61 for libndi HX/HX3 decode)"
  curl -fsSL -o "/tmp/${JF_FFMPEG_DEB}" "$JF_FFMPEG_URL"
  apt-get install -y "/tmp/${JF_FFMPEG_DEB}"
  rm -f "/tmp/${JF_FFMPEG_DEB}"
fi
if [[ ! -f /etc/ld.so.conf.d/jellyfin-ffmpeg.conf ]]; then
  echo /usr/lib/jellyfin-ffmpeg/lib > /etc/ld.so.conf.d/jellyfin-ffmpeg.conf
  ldconfig
fi

# --- uv as the service user ---
say "Ensuring service user $SVC_USER exists"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$SVC_USER"

say "Installing uv (system-wide, into /usr/local/bin)"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh
fi

# --- NDI SDK ---
# Two things have to happen:
#   1. The aarch64 libndi.so* must be on the runtime linker path (we install
#      a copy into /usr/local/lib/ + ldconfig).
#   2. The full SDK tree (with include/ and lib/<arch>/) must live somewhere
#      stable for ndi-streamer's cmake FindNDI to use. We standardise on
#      /opt/ndi-sdk/, mirroring the SDK's own layout.
NDI_SDK_ROOT="${NDI_SDK_DIR:-/opt/ndi-sdk}"
if [[ ! -f "$NDI_SDK_ROOT/include/Processing.NDI.Lib.h" ]]; then
  cat <<EOF
NDI SDK for Linux ARM64 not yet installed.

  1. Download "NDI SDK for Linux" from https://ndi.video/sdk/ (accept EULA)
  2. Copy the tarball to the Pi (e.g., scp from your laptop to /tmp/) and run:
       cd /tmp
       yes Y | sh ./Install_NDI_SDK_v6_Linux.sh    # auto-accepts EULA
       sudo mv "NDI SDK for Linux" /opt/ndi-sdk
  3. Install the aarch64 runtime libs onto the system linker path:
       sudo cp -P /opt/ndi-sdk/lib/aarch64-rpi4-linux-gnueabi/libndi.so* /usr/local/lib/
       sudo ldconfig
  4. Re-run this script.

(Set NDI_SDK_DIR=<path> if the SDK lives elsewhere.)

EOF
  exit 1
fi
# Make sure runtime libs are findable too — install.sh users who only ran
# step (2) above and forgot step (3) deserve a clean message rather than a
# cryptic dlopen failure later.
if ! ldconfig -p | grep -q '\blibndi\.so\b'; then
  cat <<EOF
NDI SDK headers found at $NDI_SDK_ROOT but libndi.so is not on the linker path.

  sudo cp -P $NDI_SDK_ROOT/lib/aarch64-rpi4-linux-gnueabi/libndi.so* /usr/local/lib/
  sudo ldconfig

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
# Apply local C patch (adds --no_audio, --sacrament_image, signal-driven
# sacrament toggle, CBR/zerolatency tuning). Idempotent.
PATCH="$PROJECT_ROOT/scripts/ndi-streamer-no-audio.patch"
if [[ -f "$PATCH" ]]; then
  if git -C "$BUILD_DIR" apply --check "$PATCH" 2>/dev/null; then
    say "Applying patch: scripts/ndi-streamer-no-audio.patch"
    git -C "$BUILD_DIR" apply "$PATCH"
  elif git -C "$BUILD_DIR" apply --reverse --check "$PATCH" 2>/dev/null; then
    : # already applied, nothing to do
  else
    say "WARNING: patch does not apply cleanly. Investigating upstream changes manually."
    exit 1
  fi
fi
# ndi-streamer's cmake/FindNDI.cmake expects the SDK at $NDI_SDK_DIR with
# include/ and lib/<arch>/ subdirs (NOT the /usr/local/include/ndi layout).
cmake -B "$BUILD_DIR/build" -S "$BUILD_DIR" -DNDI_SDK_DIR="$NDI_SDK_ROOT"
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
