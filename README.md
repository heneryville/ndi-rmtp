# ndi-rmtp

NDI → YouTube RTMP relay for Raspberry Pi 5.

See `docs/superpowers/specs/2026-06-07-ndi-rmtp-relay-design.md` for the design,
and `docs/superpowers/plans/2026-06-07-ndi-rmtp-relay-implementation.md` for the
build plan.

## Dev setup (Apple Silicon Mac)

```
./scripts/dev-setup-mac.sh
uv run uvicorn ndi_rmtp.app:app --host 0.0.0.0 --port 8080 --reload
```

## Production install (Raspberry Pi 5)

```
sudo ./scripts/install.sh
```
