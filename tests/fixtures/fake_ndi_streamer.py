#!/usr/bin/env python3
"""Fake ndi-streamer used in unit tests.

Reads commands from FAKE_NDI_SCRIPT env var (JSON list). Each element is one of:
  {"type": "stderr", "line": "..."}        - emit a line on stderr
  {"type": "sleep", "seconds": 0.05}       - sleep
  {"type": "exit", "code": 0}              - exit with code
If the script ends without an "exit" entry, the process loops sleeping until
killed (so tests can SIGTERM it).
"""
from __future__ import annotations

import json
import os
import sys
import time


def main() -> int:
    script_json = os.environ.get("FAKE_NDI_SCRIPT", "[]")
    script = json.loads(script_json)
    for step in script:
        kind = step["type"]
        if kind == "stderr":
            print(step["line"], file=sys.stderr, flush=True)
        elif kind == "sleep":
            time.sleep(step["seconds"])
        elif kind == "exit":
            return int(step.get("code", 0))
        else:
            raise ValueError(f"unknown fake step {kind}")
    while True:
        time.sleep(0.1)


if __name__ == "__main__":
    sys.exit(main())
