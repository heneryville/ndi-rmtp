#!/usr/bin/env python3
"""A process that ignores SIGTERM. Used to test SIGKILL fallback."""
import signal
import sys
import time


def _ignore(_signum, _frame) -> None:
    print("SIGTERM received, ignoring", file=sys.stderr, flush=True)


def main() -> int:
    signal.signal(signal.SIGTERM, _ignore)
    # Emit Output #0 so the streamer transitions to STREAMING
    print("Output #0, flv, to 'rtmp://test/x':", file=sys.stderr, flush=True)
    while True:
        time.sleep(0.1)


if __name__ == "__main__":
    sys.exit(main())
