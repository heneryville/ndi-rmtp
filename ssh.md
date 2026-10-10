# SSH access to the appliances

Both ndi-rmtp appliances use **password** SSH auth (not key auth), on purpose —
so any operator can connect without first installing a personal SSH key.

| Building | mDNS host                 | IP (may change via DHCP) | User | Password    |
|----------|---------------------------|--------------------------|------|-------------|
| North    | `snowspringsnorth.local`  | 192.168.108.207          | `pi` | in `.pi-password` |
| South    | `snowspringssouth.local`  | 192.168.109.134          | `pi` | in `.pi-password` |

## Connect

Interactive (type the password from `.pi-password` when prompted):

```sh
ssh pi@snowspringssouth.local     # or snowspringsnorth.local
```

Non-interactive (scripts) — macOS: `brew install sshpass`:

```sh
sshpass -f .pi-password ssh pi@snowspringssouth.local 'hostname; uptime'
```

## Notes

- The two boxes are on **different building LANs** and are not routable to each
  other — you can only reach the one whose network you're on.
- The web control UI is on port 80 (also 8080 on some builds):
  `http://snowspringssouth.local/`.
- The password lives in `.pi-password` at the repo root (git-ignored, one line).
  Get it from the Stake Technology Specialist. If these boxes ever move off a
  trusted LAN, change it (`passwd`) and update that file — and rotate the YouTube stream keys too (see `CLAUDE.md`).
