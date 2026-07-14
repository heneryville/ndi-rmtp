# Pi 5 Setup — ndi-rmtp

Validated on Pi 5 + Pi OS Lite arm64 (Trixie, 2026-06-18 image).

## 1. Image the SD card (from Mac)

```sh
# Download Pi OS Lite arm64 (Trixie / Debian 13)
curl -L https://downloads.raspberrypi.com/raspios_lite_arm64_latest -o /tmp/raspios_lite_arm64.img.xz

# Find the SD card — look for the external/removable disk matching the card size
diskutil list

# Unmount, write, eject (replace disk4 with your disk identifier)
diskutil unmountDisk /dev/disk4
time (xzcat /tmp/raspios_lite_arm64.img.xz | sudo dd of=/dev/rdisk4 bs=4m status=progress)
diskutil mountDisk /dev/disk4
```

The `r` in `rdisk4` (raw character device) is ~10× faster than `/dev/disk4` on
macOS. Expect 3–5 min for the 2.8 GB write at USB3 speeds.

If `diskutil list` doesn't show the SD card, check `system_profiler
SPCardReaderDataType` — if the hardware sees it but no `/dev/diskN` appears,
the IOMedia layer failed to enumerate. Re-seat the card.

## 2. Configure cloud-init (bootfs partition)

After imaging, the boot partition mounts at `/Volumes/bootfs`. Three files
matter — `user-data` (required; also where we enable SSH, see below),
`network-config` (only for wifi — a wired appliance needs none), and
`meta-data` (only if re-editing after a previous boot).

### `/Volumes/bootfs/user-data`

```yaml
#cloud-config
hostname: <your-hostname>
manage_etc_hosts: true

users:
- name: pi
  gecos: Pi User
  groups: adm,dialout,cdrom,sudo,audio,video,plugdev,games,users,input,netdev,gpio,i2c,spi,render
  sudo: ALL=(ALL) NOPASSWD:ALL
  lock_passwd: false
  passwd: "<output of: openssl passwd -6 YOUR_PASSWORD>"
  shell: /bin/bash

ssh_pwauth: true

packages:
- avahi-daemon

# Pi OS ships with the SSH server DISABLED. ssh_pwauth above only sets
# PasswordAuthentication *inside* sshd's config — it does NOT start the
# service. Enable + start it, generating host keys in case the service never
# ran to create them:
runcmd:
- [ ssh-keygen, -A ]
- [ systemctl, enable, --now, ssh ]
```

> **SSH is disabled by default on Pi OS.** The `runcmd` above turns it on via
> cloud-init. The classic headless switch also works and is independent of
> cloud-init: create an empty file named `ssh` in the boot partition
> (`touch /Volumes/bootfs/ssh`), which Pi OS's firstboot detects and uses to
> enable the service. Doing both is harmless belt-and-suspenders. Without one
> of these, the Pi boots and answers mDNS/ping but **refuses port 22** — the
> exact symptom that cost us a card round-trip on the north build.

### `/Volumes/bootfs/network-config` — wired by default, wifi for the bench

**Production appliances run on wired ethernet.** On Trixie the network renderer
is NetworkManager, which auto-manages `eth0` with DHCP, so an ethernet-only box
needs no `network-config` at all. Declaring `eth0` explicitly (below) is
harmless and states intent — it comes up whenever a cable is present.

**Wifi is for bench testing only.** Its password is stored in cleartext on the
Pi (`/etc/netplan/90-NM-*.yaml` and `/etc/NetworkManager/system-connections/`),
so don't ship an appliance carrying your home/lab wifi — strip it first (below).
**Never commit wifi credentials to the repo.**

```yaml
network:
  version: 2
  ethernets:
    eth0:
      dhcp4: true
      optional: true
  wifis:              # bench testing only — omit entirely for a wired appliance
    wlan0:
      dhcp4: true
      optional: true
      access-points:
        "<SSID>":
          password: "<wifi-password>"
      regulatory-domain: US
```

**`regulatory-domain` is mandatory.** Without a country code the Pi 5's wifi
radio firmware stays locked — the SSID won't even appear in scans. Use the
ISO-3166 code for your country.

**Strip bench wifi before deploy.** Once the appliance is on its wired network,
delete the bench connection so the credentials don't travel with it:

```sh
sudo nmcli con delete netplan-wlan0-<SSID>   # e.g. netplan-wlan0-chele
```

### `/Volumes/bootfs/meta-data` — only if you've already booted this card

cloud-init keys on `instance_id` to decide whether to re-run user-data and
network-config. If the Pi has already booted once with this card, the cached
`rpios-image` instance_id makes subsequent edits no-ops. Bump it to force a
re-run:

```
instance_id: <something-new-each-edit>
```

After editing all three, `diskutil eject /dev/disk4`.

## 3. First boot

Insert into Pi 5, plug ethernet in (or rely on wifi from network-config), power
on. First boot takes ~3–5 min:

1. cloud-init resizes rootfs to fill the card
2. Network comes up (~30s for wifi after radio enable)
3. `apt update` + `apt install avahi-daemon` (~30–60s)
4. avahi-daemon starts advertising the hostname via mDNS

If you're not sure what stage it's at, plug a monitor in. The login prompt
isn't always obviously rendered — **press Enter** to draw it. Log in as
`pi` with the password you set.

Verify mDNS from the Mac:

```sh
dscacheutil -q host -a name <hostname>.local
ssh pi@<hostname>.local
```

### Console fallback: configure wifi by hand

If `network-config` didn't take (most common cause: regulatory domain wasn't
applied, leaving the radio locked), connect from the Pi console:

```sh
sudo raspi-config nonint do_wifi_country US
sudo nmcli radio wifi on
sudo nmcli device wifi connect "<SSID>" password "<password>"
```

NetworkManager persists the connection across reboots. Verify with `nmcli
device status` and `ip -4 addr show wlan0`.

### Hostname rename (later)

If you ever rename the Pi (e.g., `hostnamectl set-hostname new-name`), the
Mac may keep resolving the old name from its mDNS cache. Flush it:

```sh
dscacheutil -flushcache && sudo killall -HUP mDNSResponder
```

## 4. Install the NDI SDK (EULA, manual)

The NDI SDK must be downloaded manually from <https://ndi.video/sdk/> (accept
EULA). The download is a tarball wrapping the actual `.sh` installer.

```sh
# From Mac:
scp ~/Downloads/Install_NDI_SDK_v6_Linux.tar.gz pi@<hostname>.local:/tmp/

# On the Pi:
cd /tmp
tar xzf Install_NDI_SDK_v6_Linux.tar.gz
yes Y | sh ./Install_NDI_SDK_v6_Linux.sh        # auto-accepts EULA
sudo mv "NDI SDK for Linux" /opt/ndi-sdk
sudo cp -P /opt/ndi-sdk/lib/aarch64-rpi4-linux-gnueabi/libndi.so* /usr/local/lib/
sudo ldconfig
```

## 5. Rsync the app and run the installer

```sh
# From Mac:
rsync -a ~/src/vibe-apps/ndi-rmtp/ pi@<hostname>.local:/tmp/ndi-rmtp/

# On the Pi:
sudo /tmp/ndi-rmtp/scripts/install.sh
```

`install.sh` is idempotent. It:

- Installs apt deps (FFmpeg 7, `libavahi-client-dev`, Python 3, build tools,
  librsvg)
- On Bookworm only: side-loads `jellyfin-ffmpeg7` for `libavcodec.so.61`.
  Skipped automatically on Trixie because the base `ffmpeg` package already
  provides that SONAME.
- Creates the `ndi-rmtp` service user
- Installs `uv` system-wide
- Clones, patches, and builds the vendored `ndi-streamer` binary
- Rsyncs the app under `/opt/ndi-rmtp/app/` and runs `uv sync`
- Installs the systemd unit and starts the service

The systemd unit binds port **80** via `AmbientCapabilities=CAP_NET_BIND_SERVICE`
(no root required). Dev mode (`uv run ndi-rmtp` from the project dir) defaults
to port 8080 instead; override either with `NDI_RMTP_PORT`.

## 6. Verify

- **Web UI**: <http://<hostname>.local/>
- **Service**: `sudo systemctl status ndi-rmtp`
- **Logs**: `sudo journalctl -u ndi-rmtp -f`

Auth is gated by `config.toml`:

```toml
[auth]
enabled = false   # default ships in the repo config; appliance behind LAN
```

When `enabled = true`, the first-run bootstrap generates a random admin
password and prints it to the service journal once on first start. If you
miss it:

```sh
sudo -u ndi-rmtp rm -f /home/ndi-rmtp/.config/ndi-rmtp/config.toml
sudo systemctl restart ndi-rmtp
sudo journalctl -u ndi-rmtp -n 50 | grep -A2 "first-run bootstrap"
```

---

## How `.local` hostnames work

`.local` names use **mDNS (Multicast DNS)** — the same protocol as Apple
Bonjour. The Pi's `avahi-daemon` periodically broadcasts its hostname on the
local network via multicast UDP (port 5353). Any device on the same subnet
that speaks mDNS can resolve the name without touching the router at all.

**The router is not involved.** There is no DNS registration, no DHCP
hostname injection, nothing. It's purely peer-to-peer on the LAN segment.

Supported natively by:

- macOS / iOS (Bonjour)
- Windows 10/11 (built-in mDNS resolver since ~2018)
- Linux with `avahi-daemon` or `systemd-resolved`

**Caveats:**

- Only works on the same subnet. mDNS multicast does not cross routers unless
  the router runs an mDNS proxy (some do — Ubiquiti UniFi, for example, has
  an mDNS repeater option for multi-VLAN setups).
- If the church network puts client devices and the Pi on different VLANs,
  `.local` won't resolve. In that case, use the Pi's IP address directly, or
  ask the network admin to enable the mDNS proxy.
- The name is not persistent in any DNS server — if you need the Pi reachable
  from outside the LAN or by hostname from devices that don't support mDNS,
  set a static IP and a DHCP reservation on the router instead.
