"""Allow `python -m ndi_rmtp` to launch uvicorn."""
import os
import uvicorn


def main() -> None:
    port = int(os.environ.get("NDI_RMTP_PORT", "8080"))
    uvicorn.run("ndi_rmtp.bootstrap:app", host="0.0.0.0", port=port, log_config=None)


if __name__ == "__main__":
    main()
