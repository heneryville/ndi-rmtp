#!/usr/bin/env bash
# appliance-fingerprint.sh — dump an exhaustive, diff-friendly inventory of an
# ndi-rmtp appliance (OS, kernel, firmware, FFmpeg/codec libs, NDI SDK + libndi,
# cyndilib, the ndi-streamer binary, discovered camera, app version).
#
# Purpose: run on BOTH the north and south appliances and diff the outputs to
# find why one decodes the camera's native HX3 feed and the other doesn't.
#
# Run:  bash scripts/appliance-fingerprint.sh > fingerprint-$(hostname).txt
# SHA-256 hashes are included on purpose: they catch "same version string,
# different build" — the most likely hidden difference between the two boxes.
#
# Designed to run without sudo. A few items (the service config) use `sudo -n`
# and degrade gracefully to "(needs sudo)" if not available.

set -u
sec() { printf '\n========== %s ==========\n' "$1"; }
run() { echo "\$ $*"; eval "$@" 2>&1 || echo "(command failed / n/a)"; }
hash_of() { [ -e "$1" ] && sha256sum "$1" 2>/dev/null || echo "(absent) $1"; }

VENV_PY=/opt/ndi-rmtp/app/.venv/bin/python
APP_DIR=/opt/ndi-rmtp/app
NDI_ARCH_LIB=/opt/ndi-sdk/lib/aarch64-rpi4-linux-gnueabi

echo "############################################################"
echo "# ndi-rmtp appliance fingerprint"
echo "# host: $(hostname)   collected(uptime-rel): $(cat /proc/uptime | awk '{print $1"s up"}')"
echo "############################################################"

sec "OS / RELEASE"
run "cat /etc/os-release"

sec "KERNEL / ARCH / PI MODEL / FIRMWARE"
run "uname -srvm"
run "tr -d '\0' < /proc/device-tree/model; echo"
run "cat /proc/cpuinfo | grep -iE 'model name|Hardware|Revision|Model' | sort -u"
run "vcgencmd version"
run "vcgencmd get_config int 2>/dev/null | grep -iE 'arm|gpu|hevc|codec|h265|h264' || true"

sec "FFMPEG — version + FULL build config (decoder support lives here)"
run "ffmpeg -hide_banner -version"
run "ffmpeg -hide_banner -buildconf"

sec "FFMPEG / libav / codec PACKAGES (dpkg)"
run "dpkg -l 2>/dev/null | grep -iE 'ffmpeg|libav|libsw|libx26|libde265|libheif|libvpx|libaom|libdav1d|gstreamer1.0-libav' | awk '{print \$2, \$3}' | sort"

sec "HEVC / H.264 / AV1 DECODERS available to ffmpeg"
run "ffmpeg -hide_banner -decoders 2>/dev/null | grep -iE ' hevc| h264| av1| vp9' | sort"

sec "HARDWARE VIDEO (V4L2 devices / codec nodes)"
run "ls -la /dev/video* 2>/dev/null || echo '(no /dev/video*)'"
run "for d in /dev/video*; do echo \"-- \$d\"; v4l2-ctl -d \"\$d\" --info 2>/dev/null | grep -iE 'Card type|Driver name'; done 2>/dev/null || echo '(v4l2-ctl n/a)'"

sec "ldconfig — avcodec / avutil / avformat / swscale resolution"
run "/sbin/ldconfig -p 2>/dev/null | grep -iE 'libavcodec|libavutil|libavformat|libswscale|libswresample' | sort"

sec "SHA-256 of the system FFmpeg libs libndi dlopens for decode"
for f in /lib/aarch64-linux-gnu/libavcodec.so.61 \
         /lib/aarch64-linux-gnu/libavutil.so.59 \
         /lib/aarch64-linux-gnu/libavformat.so.61 \
         /lib/aarch64-linux-gnu/libswscale.so.8 \
         /lib/aarch64-linux-gnu/libswresample.so.5; do
  hash_of "$f"
done

sec "NDI SDK version"
run "cat /opt/ndi-sdk/Version.txt"

sec "libndi — every copy on the box, with SHA-256 (catches different builds)"
run "find / -xdev -name 'libndi*.so*' -type f 2>/dev/null | sort | while read -r f; do sha256sum \"\$f\"; done"
run "find / -xdev -name 'libndi*.so*' 2>/dev/null | sort"

sec "SYSTEM libndi (used by ndi-streamer) — runtime deps"
run "ldd /usr/local/lib/libndi.so.6 2>&1 | sort"

sec "ndi-streamer BINARY — SHA-256 + full linkage"
run "which ndi-streamer; sha256sum \$(command -v ndi-streamer) 2>/dev/null"
run "ldd \$(command -v ndi-streamer) 2>&1 | sort"

sec "cyndilib — version + its BUNDLED libndi + av libs (with SHA-256)"
run "$VENV_PY -c \"import cyndilib, importlib.metadata as m; print('cyndilib', m.version('cyndilib'))\""
run "ls -la \$(dirname \$($VENV_PY -c 'import cyndilib,os;print(os.path.dirname(cyndilib.__file__))')/../*.libs 2>/dev/null) 2>/dev/null || true"
run "find /opt/ndi-rmtp/app/.venv -name 'libndi*' -o -name 'libav*' 2>/dev/null | sort | while read -r f; do sha256sum \"\$f\"; done"

sec "APP — python env (pip freeze) + git commit + configured quality"
run "$VENV_PY -m pip freeze 2>/dev/null | sort || echo '(pip n/a)'"
run "git -C $APP_DIR rev-parse HEAD 2>/dev/null || echo '(app dir not a git checkout)'"
run "git -C $APP_DIR log -1 --format='%h %ci %s' 2>/dev/null || true"
run "sudo -n grep -E '^quality|active_ward' /home/ndi-rmtp/.config/ndi-rmtp/config.toml 2>/dev/null || echo '(config needs sudo)'"

sec "CAMERA — discovered NDI source(s) via the running app"
run "curl -s -m 8 http://localhost/api/sources 2>/dev/null || curl -s -m 8 http://localhost:8080/api/sources 2>/dev/null || echo '(app API unreachable)'"

sec "CAMERA — normalized model string (strip IP so it diffs cleanly)"
run "curl -s -m 8 http://localhost/api/sources 2>/dev/null | grep -oE '\"name\":\"[^\"]*\"' | sed -E 's/, ?[0-9.]+\)/)/' | sort -u || echo '(n/a)'"

sec "CAMERA — web-server banner (weak identity probe, no auth)"
run "curl -s -m 5 -i http://\$(curl -s -m 8 http://localhost/api/sources 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | head -1)/ 2>&1 | grep -iE '^HTTP|Server:|WWW-Authenticate' | head -5 || echo '(camera web probe n/a)'"

echo
echo "############################ END ###########################"
