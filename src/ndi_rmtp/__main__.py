"""Allow `python -m ndi_rmtp` to launch uvicorn."""
import uvicorn


def main() -> None:
    uvicorn.run("ndi_rmtp.bootstrap:app", host="0.0.0.0", port=8080, log_config=None)


if __name__ == "__main__":
    main()
