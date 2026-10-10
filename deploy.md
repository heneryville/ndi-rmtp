# Deploying ndi-rmtp to a Pi appliance

Canonical deploy runbook. Deploys go through Claude following these steps.
SSH hosts and credentials live in [`ssh.md`](ssh.md); first-time Pi provisioning
lives in [`docs/pi-setup.md`](docs/pi-setup.md). This file is just the
routine "push my changes to a live appliance" flow.

## Stream keys are no longer part of a deploy

Stream keys and YouTube channel IDs are entered by a human on the appliance's
**`/setup`** page and stored device-locally in `credentials.toml`, beside the
Pi's `config.toml`. Nothing in this runbook touches that file, and `install.sh`
must never write, copy or delete it.

That means a deploy can no longer take a ward off the air by clobbering its key
— which is exactly what would happen otherwise, since a south/north deploy
replaces `config.toml` wholesale. It also means a fresh clone with no
git-ignored `config.<deployment>.toml` is now safe to deploy from: it installs
placeholder keys, and `credentials.toml` overrides them.

Wards themselves are still enumerated in the config file. **Adding or renaming
a ward is still a config deploy** — and after a rename, its key must be
re-entered on `/setup`, because credentials are keyed by ward name.

To check what an appliance has: open `/setup`. Keys are never displayed, only
their last 4 characters, which is enough to match against YouTube Studio and
against the suffixes recorded in `CONFIG_CHANGELOG.md`.

## 0. Before every deploy: bump the config version

Increment `config_version` (the first line) in the config you're about to
deploy — `config.south.toml` for South, `config.north.toml` for North — by 1,
and commit it:

```toml
config_version = 2   # was 1
```

**Why this matters.** `config_version` renders faintly in the web-UI footer as
`config vN`. After deploying, you reload the appliance's page and confirm the
number matches what you just bumped to. If it's stale, the config did **not**
land (wrong deployment mode, or the deploy didn't run) — this is exactly the
"I edited the config but the Pi never got it" failure it exists to catch.

The stamp tracks **config** generations. A standard deploy below copies the
config, so bump it every time. (A code-only `keep` deploy leaves the Pi's
config — and its version — untouched on purpose; see step 2.)

**Log every version bump.** In the same commit that bumps `config_version`, add
a one-line entry to [`CONFIG_CHANGELOG.md`](CONFIG_CHANGELOG.md) under the
appliance you bumped (South / North), recording the new version, the date, and
what changed. The footer only shows *which* generation is live; the changelog is
the record of *what each generation contains*, so an operator can map a `config
vN` reading back to the actual change (e.g. "which key did v3 rotate?"). No
changelog entry ⇒ the bump is incomplete.

## 1. Pick the target

You can only reach the appliance whose building LAN you're on (the two Pis are
not routable to each other). From `ssh.md`:

| Building | Host | Deployment |
|----------|------|------------|
| South | `snowspringssouth.local` | `south` |
| North | `snowspringsnorth.local` | `north` |

User `pi`, password in `.pi-password` (git-ignored, see `ssh.md`), passwordless sudo.

## 2. Rsync the repo to the Pi

```sh
sshpass -f .pi-password rsync -a -e ssh \
  --exclude .venv --exclude build --exclude .git \
  ./ pi@snowspringssouth.local:/tmp/ndi-rmtp/
```

## 3. Run the installer on the Pi

`NDI_RMTP_DEPLOYMENT` controls what happens to the config:

- **`south` / `north`** — copies that repo config (with your bumped
  `config_version`) to the appliance. **Use this for any deploy that includes a
  config change** — it's the normal path.
- **`keep`** — leaves the existing on-appliance config in place (code-only
  update). The version on the Pi will not change; don't use this when you meant
  to ship a config edit.

```sh
sshpass -f .pi-password ssh pi@snowspringssouth.local \
  'cd /tmp/ndi-rmtp && sudo NDI_RMTP_DEPLOYMENT=south bash scripts/install.sh'
```

**Gotcha — vendored `ndi-streamer` patch.** The clone at
`/opt/ndi-rmtp/build/ndi_streamer` is patched in place and left dirty with the
*old* patch. If `scripts/ndi-streamer-no-audio.patch` changed, `install.sh`
fails with "patch does not apply cleanly" until you reset the clone first:

```sh
sshpass -f .pi-password ssh pi@snowspringssouth.local \
  'sudo git -C /opt/ndi-rmtp/build/ndi_streamer reset --hard HEAD'
```

## 4. Verify the deploy landed

1. **Config version** — reload `http://snowspringssouth.local/` and check the
   footer reads `config vN` with the N you bumped to. Stale N ⇒ the config
   didn't deploy (you likely used `keep`, or the wrong building).
2. **Service health** — `curl -s http://snowspringssouth.local/api/status`
   should return JSON; or
   `sshpass -f .pi-password ssh pi@snowspringssouth.local 'systemctl is-active ndi-rmtp'`.
3. If you changed streaming/encoder behavior, do a real Start and confirm the
   stream reaches `STREAMING` (the app declares it only when `ndi-streamer`
   prints `Output #0`).
