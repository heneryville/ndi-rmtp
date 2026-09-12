#!/usr/bin/env python3
"""Fake source lister. Emits scripted lines to stdout and exits 0.

Two optional env vars model how the real ndi-streamer behaves, so the
discovery reader can be tested against it:

    FAKE_NDI_LISTER_DELAYS  JSON list of floats, seconds to sleep *before*
                            the line at the same index. Models ndi-streamer
                            settling its NDI source list before printing.
    FAKE_NDI_LISTER_HANG    "1" to block forever after printing instead of
                            exiting. Models the interactive
                            "select source" prompt, which means stdout never
                            reaches EOF.
"""
from __future__ import annotations

import json
import os
import sys
import time


def main() -> int:
    payload = json.loads(os.environ.get("FAKE_NDI_LISTER", "[]"))
    delays = json.loads(os.environ.get("FAKE_NDI_LISTER_DELAYS", "[]"))
    for i, line in enumerate(payload):
        if i < len(delays) and delays[i]:
            time.sleep(float(delays[i]))
        print(line, flush=True)
    if os.environ.get("FAKE_NDI_LISTER_HANG") == "1":
        while True:
            time.sleep(3600)
    return 0


if __name__ == "__main__":
    sys.exit(main())
