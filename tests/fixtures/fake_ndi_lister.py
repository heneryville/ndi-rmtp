#!/usr/bin/env python3
"""Fake source lister. Emits scripted lines to stdout and exits 0."""
from __future__ import annotations

import json
import os
import sys


def main() -> int:
    payload = json.loads(os.environ.get("FAKE_NDI_LISTER", "[]"))
    for line in payload:
        print(line, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
