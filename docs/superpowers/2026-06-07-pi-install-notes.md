# Pi Install — Field Notes

First real install of `scripts/install.sh` against a Raspberry Pi 5 running
Pi OS Bookworm (aarch64), accessed via `ssh harris@magicmirror`. Captured
here so the next install can skip rediscovery.

## What worked out of the box

- apt dependency list — installed cleanly, all packages still in Bookworm
- `uv` system-wide installer
- service user creation (`useradd --system`)
- systemd unit enable/start
- Python deps via `uv sync`
- gh CLI not present (we used rsync from the dev Mac instead of cloning)

## What needed manual fixing (now patched in the script)

### 1. NDI SDK extract was interactive (EULA prompt)

The downloaded `Install_NDI_SDK_v6_Linux.sh` is a makeself archive that
prints the entire EULA and waits for `Y` to proceed. `--noexec --target`
didn't bypass it.

**Workaround**: `yes Y | sh Install_NDI_SDK_v6_Linux.sh` auto-accepts. The
README still wants a human to accept the EULA *once* during download from
ndi.video; this just skips the re-prompt during extraction. Captured in
the install.sh help text.

### 2. NDI SDK layout mismatch with `FindNDI.cmake`

The original install.sh told operators to copy headers to
`/usr/local/include/ndi/` and libs to `/usr/local/lib/`. But ndi-streamer's
`cmake/FindNDI.cmake` on Linux looks for the SDK at a *single root* with
`include/` and `lib/<arch>/` underneath — the upstream-SDK layout. So
copying files individually meant cmake couldn't find them and the build
failed with `Could NOT find NDI`.

**Fix**: install.sh now expects the whole SDK tree to live at
`/opt/ndi-sdk/` (or wherever `$NDI_SDK_DIR` points). The script's instructions
now tell operators to move the extracted SDK there as one unit, and only
*copy* the runtime libs into `/usr/local/lib/` for `ld` to find. Headers
stay in `/opt/ndi-sdk/include/`.

### 3. install.sh was never applying our C patch (!)

This was the worst latent bug. `scripts/install.sh` cloned upstream
ndi-streamer and built it directly, never applying
`scripts/ndi-streamer-no-audio.patch`. The Pi got a vanilla ndi-streamer
with none of our features: no sacrament image, no CBR tuning, no
zerolatency tune, no `--no_audio` flag.

The mac dev-setup script (`dev-setup-mac.sh`) DID apply the patch, which
is why we never caught this — the dev path masked the prod path.

**Fix**: install.sh now does the same git-apply-with-reverse-check dance
that dev-setup-mac.sh does, idempotent.

### 4. systemd `ReadWritePaths` missed `.cache/`

`ProtectHome=read-only` + `ReadWritePaths=/home/ndi-rmtp/.config` blocked
uv from creating its cache at `/home/ndi-rmtp/.cache/uv/`. Result: every
restart of the service crashed at startup with
`Read-only file system (os error 30)`.

**Fix**: `ReadWritePaths=/home/ndi-rmtp/.config /home/ndi-rmtp/.cache` in
the unit file.

## Things still untested on the Pi

- Actually streaming to YouTube end-to-end from this hardware
- Sacrament mode toggle via the SIGUSR1 path
- PTZ control + preview via cyndilib (was that pulled in by uv sync? need
  to verify on the Pi)
- 30-minute burn-in (the Task 17 bar)
- CPU/temp performance under load — Pi 5 should be fine but no actual
  measurement yet

The service is alive on `http://magicmirror.local:8080`. Bootstrap config
will be at `/home/ndi-rmtp/.config/ndi-rmtp/config.toml`. To use the same
config as the dev Mac, `scp config.toml harris@magicmirror:/tmp/` then on
the Pi `sudo install -o ndi-rmtp -g ndi-rmtp -m 0600 /tmp/config.toml
/home/ndi-rmtp/.config/ndi-rmtp/config.toml`.

## Recovery / re-install command for future me

```sh
# from the dev Mac
rsync -aP --exclude=.venv --exclude=build --exclude=bin ./ harris@magicmirror:~/ndi-rmtp/
# on the Pi (assuming SDK already at /opt/ndi-sdk/ + libs in /usr/local/lib/)
ssh harris@magicmirror "cd ~/ndi-rmtp && sudo NDI_SDK_DIR=/opt/ndi-sdk bash scripts/install.sh"
```
