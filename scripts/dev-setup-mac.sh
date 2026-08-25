#!/usr/bin/env bash
# Apple Silicon Mac dev setup. Idempotent.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

say() { printf "\n\033[1;34m==>\033[0m %s\n" "$*"; }
warn() { printf "\n\033[1;33m!!\033[0m %s\n" "$*"; }
die() { printf "\n\033[1;31mxx\033[0m %s\n" "$*"; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || die "This script is macOS-only. Use scripts/install.sh on the Pi."

command -v brew >/dev/null || die "Homebrew not found. Install from https://brew.sh"

say "Installing Homebrew packages"
# ffmpeg + cmake + pkgconf for build; libndi cask for the NDI runtime dylib;
# librsvg for sacrament-mode SVG → PNG rasterization.
brew install ffmpeg cmake pkgconf librsvg >/dev/null
brew install --cask libndi >/dev/null || true  # cask install is fine if already present
# uv is the Python package manager.
if ! command -v uv >/dev/null; then
  say "Installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# --- NDI SDK ---
NDI_SDK_DIR="/Library/NDI SDK for Apple"
if [[ ! -d "$NDI_SDK_DIR" ]]; then
  warn "NDI SDK for Apple not found at $NDI_SDK_DIR"
  cat <<EOF

  The NDI SDK is required to BUILD ndi-streamer (it provides headers). The
  brew cask 'libndi' only installs the runtime .dylib, not the SDK.

  To install the SDK:
    1. Download "NDI SDK for Apple" from https://ndi.video/sdk/
    2. Accept the EULA, run the .pkg installer
    3. Re-run this script

EOF
  exit 1
fi

# --- Build ndi-streamer ---
BUILD_DIR="$PROJECT_ROOT/build/ndi_streamer"
if [[ ! -d "$BUILD_DIR/.git" ]]; then
  say "Cloning ndi-streamer"
  mkdir -p "$PROJECT_ROOT/build"
  git clone https://github.com/alim-zanibekov/ndi_streamer.git "$BUILD_DIR"
else
  say "ndi-streamer already cloned; pulling latest"
  git -C "$BUILD_DIR" pull --ff-only || true
fi

# Apply local patches. `git apply --check` tells us whether the patch
# applies cleanly; --reverse --check tells us it's already applied.
PATCH="$PROJECT_ROOT/scripts/ndi-streamer-no-audio.patch"
if [[ -f "$PATCH" ]]; then
  if git -C "$BUILD_DIR" apply --check "$PATCH" 2>/dev/null; then
    say "Applying patch: no-audio support"
    git -C "$BUILD_DIR" apply "$PATCH"
  elif git -C "$BUILD_DIR" apply --reverse --check "$PATCH" 2>/dev/null; then
    : # already applied, nothing to do
  else
    warn "Patch $PATCH does not apply cleanly to current ndi-streamer source"
    exit 1
  fi
fi

say "Building ndi-streamer"
cmake -B "$BUILD_DIR/build" -S "$BUILD_DIR"
cmake --build "$BUILD_DIR/build" --parallel

mkdir -p "$PROJECT_ROOT/bin"
ln -sf "$BUILD_DIR/build/ndi-streamer" "$PROJECT_ROOT/bin/ndi-streamer"

say "Verifying binary"
"$PROJECT_ROOT/bin/ndi-streamer" -h 2>&1 | head -5 || true

# --- Python env ---
say "Syncing Python environment"
uv sync --extra dev

# --- Local config ---
# The live config.*.toml files hold YouTube stream keys and are git-ignored;
# only the *.example.toml templates are tracked. On a fresh clone, seed a local
# config.south.toml from the template and point config.toml at it so
# `uv run ndi-rmtp` works out of the box (with placeholder keys to fill in).
if [[ ! -e "$PROJECT_ROOT/config.toml" ]]; then
  if [[ ! -f "$PROJECT_ROOT/config.south.toml" ]]; then
    say "Seeding config.south.toml from config.south.example.toml (fill in real stream keys)"
    cp "$PROJECT_ROOT/config.south.example.toml" "$PROJECT_ROOT/config.south.toml"
    chmod 600 "$PROJECT_ROOT/config.south.toml"
  fi
  ln -sf config.south.toml "$PROJECT_ROOT/config.toml"
  warn "config.toml -> config.south.toml created with PLACEHOLDER stream keys — edit it before going live."
fi

cat <<EOF

==> Dev setup complete. To run the app:

    uv run ndi-rmtp

or (with autoreload):

    uv run uvicorn ndi_rmtp.bootstrap:app --host 0.0.0.0 --port 8080 --reload

The first run prints an admin password to stderr — save it.
Open http://localhost:8080 in a browser.

To run the integration test (requires an NDI source on your LAN):

    NDI_TEST_SOURCE="MITCHELL-VIDERA (Scan Converter)" uv run pytest -m integration -v

EOF
