# Config changelog

One entry per `config_version` bump, per appliance. This is the record of *what*
each generation contains; the web-UI footer (`config vN`) only tells you *which*
generation is currently live. Together they let an operator map a footer reading
back to an actual change.

**When you bump `config_version`** in `config.south.toml` or `config.north.toml`
(step 0 of [`deploy.md`](deploy.md)), add a line here under the matching
appliance in the same commit. Newest version on top. Keep each entry to the
version, the date, and what changed — call out any stream-key rotation
explicitly (which ward, old → new suffix) since that's the thing operators most
often need to trace.

## South (`config.south.toml`)

- **v3** — 2026-08-29 — Restored `quality = "high"` (1080p30 @ 6 Mbps); the
  appliance had been left on `medium` (3 Mbps). No stream-key changes.
- **v2** — 2026-08-15 — Added the `config_version` stamp; fixed Test Ward stream
  key to `5wtm-…-4xea`.
- **v1** — pre-2026-08-15 — Initial stamped generation (backfilled from git
  history; predates this changelog).

## North (`config.north.toml`)

- **v3** — 2026-08-23 — Rotated Lehi 25th Ward stream key (`…-8zet` → `…-25rj`).
- **v2** — 2026-08-15 — Wired real ward stream keys + sacrament overlays; bumped
  for first North deploy.
- **v1** — pre-2026-08-15 — Initial stamped generation (backfilled from git
  history; predates this changelog).
