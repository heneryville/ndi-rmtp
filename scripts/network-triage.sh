#!/usr/bin/env bash
# network-triage.sh — read-only LAN evidence gathering for the ndi-rmtp appliance.
#
# Answers ONE question: is the appliance's network actually saturated, and if so,
# by what? Changes nothing. Safe to run while a stream is live.
#
# Usage (on the Pi):
#   sudo bash scripts/network-triage.sh            # 10s sample window
#   sudo bash scripts/network-triage.sh 30         # 30s sample window
#
# Usage (from a Mac on the same building LAN):
#   sshpass -f .pi-password ssh pi@snowspringsnorth.local 'sudo bash -s' \
#       < scripts/network-triage.sh
#
# IMPORTANT LIMITATION, read before trusting section 6:
#   On a switched LAN this host can only see (a) its own traffic, (b) broadcast,
#   and (c) multicast. Unicast between two OTHER machines — e.g. someone's laptop
#   pulling NDI from the camera — is invisible here. That asymmetry is itself
#   diagnostic: if you CAN see a third party's NDI video from this box, it is
#   being multicast/flooded, which is the problem.

set -uo pipefail

WINDOW="${1:-10}"
IFACE="${IFACE:-}"

hr() { printf '\n========== %s ==========\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }
run() { printf '$ %s\n' "$*"; "$@" 2>&1 || true; }

# Root is needed for tcpdump; everything else degrades gracefully.
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    if sudo -n true 2>/dev/null; then SUDO="sudo -n"; fi
fi
# Can we packet-capture? (root, or passwordless sudo)
CAN_CAP=0
if [ "$(id -u)" -eq 0 ] || [ -n "$SUDO" ]; then CAN_CAP=1; fi

printf '############################################################\n'
printf '# ndi-rmtp network triage\n'
printf '# host: %s   window: %ss\n' "$(hostname)" "$WINDOW"
printf '############################################################\n'

# --------------------------------------------------------------------------
hr "0. DEFAULT INTERFACE"
if [ -z "$IFACE" ]; then
    IFACE="$(ip route show default 2>/dev/null | awk '/default/{print $5; exit}')"
fi
[ -z "$IFACE" ] && IFACE="eth0"
echo "interface: $IFACE"
run ip -brief addr show "$IFACE"
run ip route show default

# --------------------------------------------------------------------------
hr "1. LINK HEALTH (a 100Mb or half-duplex negotiation looks exactly like congestion)"
if have ethtool; then
    $SUDO ethtool "$IFACE" 2>&1 | grep -iE 'speed|duplex|link detected|auto-negotiation' || true
    echo "--- error counters ---"
    $SUDO ethtool -S "$IFACE" 2>/dev/null \
        | grep -iE 'err|drop|discard|collision|fifo|crc|pause' \
        | awk '$2 != 0' || echo "(no non-zero error counters, or -S unsupported)"
else
    echo "(ethtool not installed — install with: sudo apt-get install -y ethtool)"
    cat "/sys/class/net/$IFACE/speed" 2>/dev/null \
        | sed 's/^/speed (Mb\/s): /' || echo "(speed unreadable)"
    cat "/sys/class/net/$IFACE/duplex" 2>/dev/null | sed 's/^/duplex: /' || true
fi
echo "--- kernel-side drops ---"
run ip -s -s link show "$IFACE"

# --------------------------------------------------------------------------
hr "2. THROUGHPUT — ${WINDOW}s counter delta (this is the ground truth)"
read_counters() {
    # /proc/net/dev runs the byte count straight into "eth0:" once it is large
    # enough (e.g. "eth0:12345678901"), so force a separator before parsing.
    sed 's/:/ /' /proc/net/dev 2>/dev/null | awk -v i="$IFACE" '$1==i {print $2, $10}'
}
read -r RX1 TX1 <<<"$(read_counters)"
sleep "$WINDOW"
read -r RX2 TX2 <<<"$(read_counters)"

if [ -n "${RX1:-}" ] && [ -n "${RX2:-}" ]; then
    LINK_MB="$(cat "/sys/class/net/$IFACE/speed" 2>/dev/null || echo 1000)"
    awk -v rx1="$RX1" -v rx2="$RX2" -v tx1="$TX1" -v tx2="$TX2" \
        -v w="$WINDOW" -v link="$LINK_MB" 'BEGIN {
        rx = (rx2-rx1)*8/w/1e6; tx = (tx2-tx1)*8/w/1e6;
        printf "  RX (in) : %8.2f Mbps\n", rx;
        printf "  TX (out): %8.2f Mbps\n", tx;
        printf "  link    : %8s Mbps\n", link;
        if (link > 0) {
            printf "  RX util : %8.1f %% of link\n", 100*rx/link;
            printf "  TX util : %8.1f %% of link\n", 100*tx/link;
        }
        print "";
        print "  Interpretation:";
        print "   - camera NDI HX3 1080p in  ~= 8-25 Mbps RX";
        print "   - our RTMP push out (high) ~=  6.2 Mbps TX";
        print "   - full-bandwidth NDI (SpeedHQ) 1080p ~= 100-160 Mbps PER receiver";
        print "   - RX far above ~30 Mbps while we stream = someone else'\''s video is";
        print "     reaching this box, which on a switch means multicast/flood.";
    }'
else
    echo "(could not read /proc/net/dev counters for $IFACE)"
fi

# --------------------------------------------------------------------------
hr "3. NDI SOURCE CENSUS — everything advertising on this LAN"
NDI_BIN="$(command -v ndi-streamer || echo /usr/local/bin/ndi-streamer)"
if [ -x "$NDI_BIN" ]; then
    echo "$ $NDI_BIN  (2s capture, then killed)"
    timeout 4 "$NDI_BIN" </dev/null 2>/dev/null | grep -E '^\s*[0-9]+\.' || \
        echo "(no numbered source lines captured)"
else
    echo "(ndi-streamer not found at $NDI_BIN)"
fi
echo
echo "--- classification hints ---"
cat <<'EOF'
  CAMERA (expected, ours):
      "UHD CAMERA (NDI HX3, 192.168.111.248)"
  VIRTUAL / SOFTWARE senders (the thing we are hunting) look like:
      "<MACHINE> (Scan Converter)"        NDI Screen Capture  - full bandwidth
      "<MACHINE> (Screen Capture HX)"     newer, H.264        - moderate
      "<MACHINE> (Desktop N)"             screen share
      "<MACHINE> (Test Pattern)"          NDI Test Patterns   - full bandwidth
      "<MACHINE> (NDI Virtual Input)"     RECEIVER, not a sender - see note
      "<MACHINE> (OBS)" / "(vMix - ...)"  production software  - full bandwidth
  NOTE on "NDI Virtual Input": that tool CONSUMES an NDI stream and presents it
  as a webcam. It advertises itself but it is the RECEIVE side. A Virtual Input
  pointed at the chapel camera makes the CAMERA send an extra unicast copy —
  the cost lands on the camera's uplink, not on this Pi.
EOF

# --------------------------------------------------------------------------
hr "4. MULTICAST — the #1 way one device takes down a flat church LAN"
run ip maddr show "$IFACE"
echo
echo "--- IGMP group memberships ---"
run cat /proc/net/igmp
echo
if have tcpdump && [ "$CAN_CAP" = 1 ]; then
    echo "--- 5s multicast sample (239.0.0.0/8 = NDI's usual range) ---"
    echo "$ tcpdump -i $IFACE -n 'net 239.0.0.0/8' -c 200"
    $SUDO timeout 6 tcpdump -i "$IFACE" -n -q 'net 239.0.0.0/8' -c 200 2>/dev/null \
        | awk '{print $3}' | cut -d. -f1-4 | sort | uniq -c | sort -rn | head -10 \
        || echo "(no multicast captured — good; NDI is unicast by default)"
    echo "  ^ any high count here = a sender has MULTICAST SENDING enabled."
else
    echo "(tcpdump unavailable or no root — install: sudo apt-get install -y tcpdump)"
fi

# --------------------------------------------------------------------------
hr "5. WHO IS THIS BOX TALKING TO (our own sockets only)"
if have ss; then
    echo "--- established, non-loopback ---"
    ss -tunp 2>/dev/null | grep -vE '127\.0\.0\.1|\[::1\]' | head -30
    echo
    echo "--- anything on NDI ports 5353/5960-5970 ---"
    ss -tuna 2>/dev/null | grep -E ':(5353|59[0-7][0-9])\b' | head -20 \
        || echo "(none)"
else
    run netstat -tunp
fi

# --------------------------------------------------------------------------
hr "6. TOP TALKERS VISIBLE FROM HERE (see LIMITATION in the header)"
if have tcpdump && [ "$CAN_CAP" = 1 ]; then
    echo "$ tcpdump -i $IFACE -n -c 400   (~5s)"
    $SUDO timeout 6 tcpdump -i "$IFACE" -n -q -c 400 2>/dev/null \
        | awk '{print $3}' | sed -E 's/\.[0-9]+$//' | sort | uniq -c | sort -rn | head -12 \
        || echo "(capture failed)"
    echo "  ^ src IPs by packet count. Cross-reference against the NDI census above."
else
    echo "(tcpdump unavailable or no root)"
fi
echo
echo "--- mDNS chatter volume (discovery storms live here) ---"
if have tcpdump && [ "$CAN_CAP" = 1 ]; then
    CNT=$($SUDO timeout 6 tcpdump -i "$IFACE" -n 'udp port 5353' 2>/dev/null | wc -l)
    echo "  $CNT mDNS packets in ~5s  (normal idle: <50; >500 = storm)"
else
    echo "  (tcpdump unavailable or no root — skipped)"
fi

# --------------------------------------------------------------------------
hr "7. EGRESS / WAN — is the problem actually the uplink, not the LAN?"
echo "--- latency to YouTube RTMP ingest (bufferbloat shows as high stddev) ---"
run ping -c 8 -i 0.3 -W 2 a.rtmp.youtube.com
echo
echo "--- our app's own view of the stream ---"
run curl -s -m 5 http://localhost/api/status
echo
run curl -s -m 5 http://localhost/api/sources

# --------------------------------------------------------------------------
hr "8. STREAMER PROCESS"
run pgrep -a ndi-streamer
run systemctl is-active ndi-rmtp
echo "--- last 20 streamer log lines ---"
$SUDO journalctl -u ndi-rmtp -n 20 --no-pager 2>/dev/null || echo "(journal unavailable)"

printf '\n############################ END ###########################\n'
