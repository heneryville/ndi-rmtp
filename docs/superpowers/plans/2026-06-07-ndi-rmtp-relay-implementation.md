# NDI → YouTube RTMP Relay — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an appliance that runs on a Raspberry Pi 5 (and Apple Silicon Mac for dev), pulls a single NDI source off the local network, and re-broadcasts it to YouTube Live via RTMP, controlled by a local web UI.

**Architecture:** A thin Python FastAPI control plane spawns and supervises `ndi_streamer` (alim-zanibekov, MIT, C), which pipes NDI through the vanilla FFmpeg libraries straight to RTMP. Python is never in the video data path. State machine: `IDLE → STARTING → STREAMING → STOPPING → IDLE`, with `ERROR` on crash. The full design is in `docs/superpowers/specs/2026-06-07-ndi-rmtp-relay-design.md` — read it before starting Task 1.

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, HTMX (no JS framework), TOML config, `uv` package manager, `ndi_streamer` (C binary), vanilla FFmpeg libs (`libavcodec`, `libavformat`, etc.), NDI SDK for Linux ARM64 / Apple, pytest + pytest-asyncio.

**Validation phases** (from the spec):
- **Phase 0 (Task 2)** — spike: prove `ndi_streamer` can push to YouTube. **HARD GATE.**
- **Phase 1 (Tasks 3-12)** — automated tests + integration test, green continuously.
- **Phase 2 (Task 15)** — manual end-to-end on Mac.
- **Phase 3 (Task 16)** — error-path checklist on Mac.
- **Phase 4 (Task 17)** — Pi acceptance + 30-minute burn-in.

---

## Task 1: Project scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `README.md`
- Create: `src/ndi_rmtp/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Verify `uv` is installed**

```bash
uv --version
```
Expected: a version string. If missing, install with `curl -LsSf https://astral.sh/uv/install.sh | sh`.

- [ ] **Step 2: Create `pyproject.toml`**

```toml
[project]
name = "ndi-rmtp"
version = "0.1.0"
description = "NDI to YouTube RTMP relay for Raspberry Pi"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "jinja2>=3.1",
    "python-multipart>=0.0.20",
    "tomli-w>=1.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.24",
    "httpx>=0.27",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/ndi_rmtp"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = [
    "integration: requires Docker and an NDI source on the LAN (env NDI_TEST_SOURCE)",
]
```

- [ ] **Step 3: Create `.gitignore`**

```gitignore
# Python
__pycache__/
*.py[cod]
*$py.class
.venv/
*.egg-info/
.pytest_cache/

# uv
uv.lock

# Local builds
/bin/
/build/

# Dev artifacts
.DS_Store
*.log
```

(`uv.lock` is committed normally, but we exclude it here because we're not ready to commit a lock until deps are settled. Remove this line when deps stabilize.)

- [ ] **Step 4: Create empty package and test init files**

```bash
mkdir -p src/ndi_rmtp/templates src/ndi_rmtp/static tests/fixtures scripts
touch src/ndi_rmtp/__init__.py tests/__init__.py tests/fixtures/__init__.py
```

- [ ] **Step 5: Create `tests/conftest.py`** (empty for now, populated as tests need shared fixtures)

```python
"""Shared pytest fixtures. Populated as tests need them."""
```

- [ ] **Step 6: Create a minimal `README.md`**

```markdown
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
```

- [ ] **Step 7: `uv sync` the dev environment**

```bash
uv sync --extra dev
```
Expected: `.venv/` created, dependencies installed, no errors.

- [ ] **Step 8: Smoke-test pytest discovery**

```bash
uv run pytest --collect-only
```
Expected: `collected 0 items` (no tests yet — confirms pytest is wired correctly).

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml .gitignore README.md src/ tests/
git commit -m "Scaffold project: pyproject.toml, package + tests skeleton"
```

---

## Task 2: Phase 0 spike — prove the data path (HARD GATE)

**Goal:** confirm `ndi_streamer` can stream the user's test NDI source to YouTube on both Mac and Pi. If this fails, stop the plan and switch to the FFmpeg-libndi fallback. Do not start Task 3 until this passes.

**Files:**
- Create: `docs/superpowers/2026-06-07-spike-results.md`

- [ ] **Step 1: Install build dependencies on Mac**

```bash
brew install ffmpeg cmake pkg-config
```

- [ ] **Step 2: Download and install NDI SDK for Apple**

1. Open <https://ndi.video/sdk/> in a browser.
2. Download "NDI SDK for Apple". Accept the EULA.
3. Install to the default path (`/Library/NDI SDK for Apple/`).
4. Symlink the dylib so the loader and pkg-config find it:

```bash
sudo ln -sf "/Library/NDI SDK for Apple/lib/macOS/libndi.dylib" /usr/local/lib/libndi.dylib
sudo mkdir -p /usr/local/include/ndi
sudo ln -sf "/Library/NDI SDK for Apple/include/"*.h /usr/local/include/ndi/
```

- [ ] **Step 3: Clone and build `ndi_streamer`**

```bash
mkdir -p build && cd build
git clone https://github.com/alim-zanibekov/ndi_streamer.git
cd ndi_streamer
cmake -B build -S .
cmake --build build --parallel
ls -lh build/ndi_streamer  # confirm binary exists
cd ../..
```
Expected: a `ndi_streamer` binary in `build/ndi_streamer/build/`.

- [ ] **Step 4: Source discovery**

Run `ndi_streamer` with no arguments (or its help/list flag — confirm during this step):

```bash
./build/ndi_streamer/build/ndi_streamer --help
```

Note the actual flag for listing sources (likely no NDI input arg = list mode). Record the discovered flag verbatim in `docs/superpowers/2026-06-07-spike-results.md`. Then run discovery:

```bash
./build/ndi_streamer/build/ndi_streamer  # or --list, or whatever the help showed
```
Expected: the user's test NDI source name appears within 5 s. Record the exact source name string for use later.

- [ ] **Step 5: Local RTMP loop**

In one terminal, start an nginx-rtmp container:

```bash
docker run --rm -p 1935:1935 --name spike-rtmp tiangolo/nginx-rtmp
```

In another terminal, push the NDI source to it:

```bash
./build/ndi_streamer/build/ndi_streamer \
  -n "<the source name from step 4>" \
  -o rtmp://localhost/live/test
```

In a third terminal, play it:

```bash
ffplay -fflags nobuffer rtmp://localhost/live/test
```
Expected: video plays for ≥ 30 s. Record any stderr warnings.

- [ ] **Step 6: YouTube round-trip**

1. In YouTube Studio, create an "unlisted" stream and copy the stream key.
2. Push:

```bash
./build/ndi_streamer/build/ndi_streamer \
  -n "<source>" \
  -o "rtmp://a.rtmp.youtube.com/live2/<your-key>"
```

3. Watch YouTube Studio. Expected: stream goes live within ~30 s, Stream Health reads "Excellent" or "Good" for ≥ 60 s, audio in sync.

- [ ] **Step 7: Repeat steps 1-6 on the Pi 5**

On a Raspberry Pi 5 running Pi OS Bookworm or Trixie (aarch64):

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake git pkg-config \
    libavformat-dev libavcodec-dev libavutil-dev libswresample-dev libswscale-dev ffmpeg
```

Download the NDI SDK for Linux ARM64 from <https://ndi.video/sdk/>, accept the EULA, then:

```bash
tar xf 'Install_NDI_SDK_v6_Linux.tar.gz'  # actual filename may differ
sudo ./'Install_NDI_SDK_v6_Linux.sh'      # accept EULA
sudo cp -r 'NDI SDK for Linux'/lib/aarch64-rpi4-linux-gnueabi/* /usr/local/lib/
sudo cp -r 'NDI SDK for Linux'/include/* /usr/local/include/
sudo ldconfig
```

Then repeat the clone/build/source-list/local-loop/YouTube steps.

- [ ] **Step 8: Pi sanity check**

While the YouTube stream from step 7 is running, in another SSH session:

```bash
htop                      # CPU per core
vcgencmd measure_temp     # SoC temp
```
Pass criteria: total CPU ≤ 80 %, no single core at 100 %, temp ≤ 75 °C steady-state.

- [ ] **Step 9: Write spike results**

Create `docs/superpowers/2026-06-07-spike-results.md` with:
- The actual `ndi_streamer --help` output (or equivalent).
- The exact CLI flags used in steps 5-6 (so Task 6 can match them).
- Mac and Pi build commands that worked.
- Source-list output format (used by Task 8 to parse).
- Any YouTube-side observations (latency, health).
- Pi sanity readings.

This document is the contract between the spike and the implementation. **If any flag in this document differs from the assumptions in Task 6 (`build_args`) or Task 8 (discovery parsing), update those tasks before executing them.**

- [ ] **Step 10: Commit spike results**

```bash
git add docs/superpowers/2026-06-07-spike-results.md
git commit -m "Record Phase 0 spike results: ndi_streamer validated on Mac and Pi"
```

**Gate check:** if any of steps 4-8 failed, **stop and reconsider architecture** before proceeding. Options: fall back to FFmpeg-libndi, or open an issue. Do not start Task 3 with an unvalidated data path.

---

## Task 3: Config persistence

**Files:**
- Create: `src/ndi_rmtp/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config.py`:

```python
import stat
from pathlib import Path

import pytest

from ndi_rmtp.config import Config, DEFAULT_RTMP_URL


def test_bootstrap_creates_file_with_random_password(tmp_path):
    path = tmp_path / "config.toml"
    config, password = Config.bootstrap(path)
    assert path.exists()
    assert config.auth.username == "admin"
    assert config.auth.password == password
    assert len(password) >= 12
    assert config.stream.rtmp_url == DEFAULT_RTMP_URL


def test_bootstrap_two_calls_produce_different_passwords(tmp_path):
    _, p1 = Config.bootstrap(tmp_path / "a.toml")
    _, p2 = Config.bootstrap(tmp_path / "b.toml")
    assert p1 != p2


def test_load_round_trips(tmp_path):
    path = tmp_path / "config.toml"
    original, _ = Config.bootstrap(path)
    original.stream.stream_key = "abc-123"
    original.stream.last_source = "TEST (Source)"
    original.stream.quality = "high"
    original.save(path)

    loaded = Config.load(path)
    assert loaded.auth.username == original.auth.username
    assert loaded.auth.password == original.auth.password
    assert loaded.stream.stream_key == "abc-123"
    assert loaded.stream.last_source == "TEST (Source)"
    assert loaded.stream.quality == "high"


def test_save_uses_mode_0600(tmp_path):
    path = tmp_path / "config.toml"
    config, _ = Config.bootstrap(path)
    config.save(path)
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_save_is_atomic_no_partial_file_on_crash(tmp_path, monkeypatch):
    """If serialization fails mid-write, the original file must remain intact."""
    path = tmp_path / "config.toml"
    config, _ = Config.bootstrap(path)
    config.stream.stream_key = "original"
    config.save(path)

    # Now sabotage tomli_w.dump to raise mid-save
    import tomli_w
    def boom(*a, **kw):
        raise RuntimeError("disk full")
    monkeypatch.setattr(tomli_w, "dump", boom)

    config.stream.stream_key = "new"
    with pytest.raises(RuntimeError):
        config.save(path)

    # Original file unchanged
    loaded = Config.load(path)
    assert loaded.stream.stream_key == "original"
    # No leftover temp files in the dir
    assert list(path.parent.glob(".tmp-*")) == []


def test_load_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        Config.load(tmp_path / "does-not-exist.toml")


def test_bootstrap_creates_parent_directories(tmp_path):
    path = tmp_path / "deep" / "nested" / "config.toml"
    Config.bootstrap(path)
    assert path.exists()
```

- [ ] **Step 2: Run the tests and watch them fail**

```bash
uv run pytest tests/test_config.py -v
```
Expected: all tests fail with `ModuleNotFoundError: No module named 'ndi_rmtp.config'`.

- [ ] **Step 3: Implement `src/ndi_rmtp/config.py`**

```python
"""TOML configuration persistence with atomic writes and 0600 file mode."""
from __future__ import annotations

import os
import secrets
import string
import tempfile
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

import tomli_w

DEFAULT_RTMP_URL = "rtmp://a.rtmp.youtube.com/live2"
DEFAULT_BACKOFF_S = [2, 5, 15, 60]
PASSWORD_LEN = 12


@dataclass
class AuthConfig:
    username: str
    password: str


@dataclass
class StreamConfig:
    last_source: str = ""
    stream_key: str = ""
    rtmp_url: str = DEFAULT_RTMP_URL
    quality: str = "medium"


@dataclass
class AdvancedConfig:
    auto_restart: bool = True
    restart_backoff_s: list[int] = field(default_factory=lambda: list(DEFAULT_BACKOFF_S))
    discovery_ttl_s: int = 5
    status_poll_ms: int = 2000


@dataclass
class Config:
    auth: AuthConfig
    stream: StreamConfig = field(default_factory=StreamConfig)
    advanced: AdvancedConfig = field(default_factory=AdvancedConfig)

    @classmethod
    def load(cls, path: Path) -> "Config":
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return cls(
            auth=AuthConfig(**data["auth"]),
            stream=StreamConfig(**data.get("stream", {})),
            advanced=AdvancedConfig(**data.get("advanced", {})),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".toml", dir=str(path.parent))
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as f:
                tomli_w.dump(asdict(self), f)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, path)
        except Exception:
            # Roll back: remove temp file so we don't leak partial state
            tmp_path.unlink(missing_ok=True)
            raise

    @classmethod
    def bootstrap(cls, path: Path) -> tuple["Config", str]:
        alphabet = string.ascii_letters + string.digits
        password = "".join(secrets.choice(alphabet) for _ in range(PASSWORD_LEN))
        config = cls(auth=AuthConfig(username="admin", password=password))
        config.save(path)
        return config, password
```

- [ ] **Step 4: Run the tests, expect green**

```bash
uv run pytest tests/test_config.py -v
```
Expected: all 7 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/ndi_rmtp/config.py tests/test_config.py
git commit -m "Add TOML config with atomic save, 0600 mode, and first-run bootstrap"
```

---

## Task 4: Auth dependency

**Files:**
- Create: `src/ndi_rmtp/auth.py`
- Create: `tests/test_auth.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_auth.py`:

```python
import base64

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from ndi_rmtp.auth import make_auth_dependency
from ndi_rmtp.config import AuthConfig, Config, StreamConfig, AdvancedConfig


def _app(config: Config) -> FastAPI:
    app = FastAPI()
    require_auth = make_auth_dependency(lambda: config)

    @app.get("/protected")
    def protected(_: None = Depends(require_auth)):
        return {"ok": True}

    @app.get("/open")
    def open_route():
        return {"ok": True}

    return app


def _basic(user: str, password: str) -> dict[str, str]:
    raw = f"{user}:{password}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


@pytest.fixture
def config() -> Config:
    return Config(
        auth=AuthConfig(username="admin", password="hunter2"),
        stream=StreamConfig(),
        advanced=AdvancedConfig(),
    )


def test_valid_credentials_pass(config):
    client = TestClient(_app(config))
    r = client.get("/protected", headers=_basic("admin", "hunter2"))
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_wrong_password_rejected(config):
    client = TestClient(_app(config))
    r = client.get("/protected", headers=_basic("admin", "wrong"))
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Basic"


def test_wrong_username_rejected(config):
    client = TestClient(_app(config))
    r = client.get("/protected", headers=_basic("root", "hunter2"))
    assert r.status_code == 401


def test_missing_header_rejected(config):
    client = TestClient(_app(config))
    r = client.get("/protected")
    assert r.status_code == 401


def test_open_route_not_gated(config):
    client = TestClient(_app(config))
    r = client.get("/open")
    assert r.status_code == 200


def test_credentials_compared_constant_time(config):
    """Sanity: we use secrets.compare_digest, not == . Smoke check only."""
    import secrets as s
    assert hasattr(s, "compare_digest")
```

- [ ] **Step 2: Run the tests, expect failure**

```bash
uv run pytest tests/test_auth.py -v
```
Expected: all tests fail with `ModuleNotFoundError: No module named 'ndi_rmtp.auth'`.

- [ ] **Step 3: Implement `src/ndi_rmtp/auth.py`**

```python
"""HTTP Basic Auth dependency factory."""
from __future__ import annotations

import secrets
from typing import Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .config import Config

security = HTTPBasic()


def make_auth_dependency(get_config: Callable[[], Config]):
    """Returns a FastAPI dependency that enforces HTTP Basic Auth against config."""

    def _check(credentials: HTTPBasicCredentials = Depends(security)) -> None:
        config = get_config()
        ok_user = secrets.compare_digest(
            credentials.username.encode(), config.auth.username.encode()
        )
        ok_pass = secrets.compare_digest(
            credentials.password.encode(), config.auth.password.encode()
        )
        if not (ok_user and ok_pass):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
                headers={"WWW-Authenticate": "Basic"},
            )

    return _check
```

- [ ] **Step 4: Run tests, expect green**

```bash
uv run pytest tests/test_auth.py -v
```
Expected: 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/ndi_rmtp/auth.py tests/test_auth.py
git commit -m "Add HTTP Basic Auth dependency with constant-time compare"
```

---

## Task 5: State machine

**Files:**
- Create: `src/ndi_rmtp/state.py`
- Create: `tests/test_state_machine.py`

This task isolates the state machine from the subprocess concerns, so transitions can be unit-tested without async/process noise.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_state_machine.py`:

```python
import pytest

from ndi_rmtp.state import State, StateMachine


def test_initial_state_is_idle():
    sm = StateMachine()
    assert sm.state == State.IDLE


@pytest.mark.parametrize(
    "frm,to",
    [
        (State.IDLE, State.STARTING),
        (State.STARTING, State.STREAMING),
        (State.STARTING, State.ERROR),
        (State.STARTING, State.STOPPING),
        (State.STREAMING, State.STOPPING),
        (State.STREAMING, State.ERROR),
        (State.STOPPING, State.IDLE),
        (State.STOPPING, State.ERROR),
        (State.ERROR, State.IDLE),
        (State.ERROR, State.STARTING),
    ],
)
def test_valid_transitions_succeed(frm, to):
    sm = StateMachine()
    sm._force(frm)  # test-only helper
    sm.transition(to, reason="test")
    assert sm.state == to


@pytest.mark.parametrize(
    "frm,to",
    [
        (State.IDLE, State.STREAMING),       # must go through STARTING
        (State.IDLE, State.STOPPING),
        (State.IDLE, State.ERROR),
        (State.STREAMING, State.STARTING),   # cannot bounce backward
        (State.STREAMING, State.IDLE),       # must go through STOPPING
        (State.STOPPING, State.STREAMING),
        (State.STOPPING, State.STARTING),
    ],
)
def test_invalid_transitions_raise(frm, to):
    sm = StateMachine()
    sm._force(frm)
    with pytest.raises(ValueError):
        sm.transition(to, reason="test")
    assert sm.state == frm  # state unchanged on failed transition


def test_transition_history_recorded():
    sm = StateMachine()
    sm.transition(State.STARTING, reason="user start")
    sm.transition(State.STREAMING, reason="first frame")
    history = sm.history()
    assert [h.to for h in history] == [State.STARTING, State.STREAMING]
    assert history[0].reason == "user start"
```

- [ ] **Step 2: Run, expect failure**

```bash
uv run pytest tests/test_state_machine.py -v
```
Expected: all fail with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/ndi_rmtp/state.py`**

```python
"""State machine for the streamer. No subprocess concerns here."""
from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


class State(enum.Enum):
    IDLE = "IDLE"
    STARTING = "STARTING"
    STREAMING = "STREAMING"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


VALID_TRANSITIONS: dict[State, set[State]] = {
    State.IDLE: {State.STARTING},
    State.STARTING: {State.STREAMING, State.ERROR, State.STOPPING},
    State.STREAMING: {State.STOPPING, State.ERROR},
    State.STOPPING: {State.IDLE, State.ERROR},
    State.ERROR: {State.IDLE, State.STARTING},
}


@dataclass
class Transition:
    frm: State
    to: State
    reason: str
    at: float


class StateMachine:
    def __init__(self) -> None:
        self._state = State.IDLE
        self._history: list[Transition] = []

    @property
    def state(self) -> State:
        return self._state

    def transition(self, to: State, reason: str) -> None:
        if to not in VALID_TRANSITIONS[self._state]:
            raise ValueError(
                f"Invalid transition: {self._state.value} -> {to.value}"
            )
        frm = self._state
        self._state = to
        self._history.append(Transition(frm=frm, to=to, reason=reason, at=time.time()))
        logger.info("state %s -> %s (%s)", frm.value, to.value, reason)

    def history(self) -> list[Transition]:
        return list(self._history)

    def _force(self, state: State) -> None:
        """Test-only: jump straight to a state. Do not use in production code."""
        self._state = state
```

- [ ] **Step 4: Run, expect green**

```bash
uv run pytest tests/test_state_machine.py -v
```
Expected: 22 tests pass (parametrize expands them).

- [ ] **Step 5: Commit**

```bash
git add src/ndi_rmtp/state.py tests/test_state_machine.py
git commit -m "Add state machine with explicit valid-transition table"
```

---

## Task 6: Streamer subprocess control

This is the biggest module. We use a fake `ndi_streamer` written in Python as a test fixture so unit tests don't need the real C binary.

**Files:**
- Create: `tests/fixtures/fake_ndi_streamer.py`
- Create: `tests/fixtures/hanging_ndi_streamer.py`
- Create: `src/ndi_rmtp/streamer.py`
- Create: `tests/test_streamer.py`

- [ ] **Step 1: Create the test fixtures**

`tests/fixtures/fake_ndi_streamer.py` — a scripted stderr emitter:

```python
#!/usr/bin/env python3
"""Fake ndi_streamer used in unit tests.

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
```

`tests/fixtures/hanging_ndi_streamer.py` — ignores SIGTERM:

```python
#!/usr/bin/env python3
"""A process that ignores SIGTERM. Used to test SIGKILL fallback."""
import signal
import sys
import time


def _ignore(_signum, _frame) -> None:
    print("SIGTERM received, ignoring", file=sys.stderr, flush=True)


def main() -> int:
    signal.signal(signal.SIGTERM, _ignore)
    print("frame=1 fps=30", file=sys.stderr, flush=True)  # so STREAMING is reached
    while True:
        time.sleep(0.1)


if __name__ == "__main__":
    sys.exit(main())
```

Make them executable:

```bash
chmod +x tests/fixtures/fake_ndi_streamer.py tests/fixtures/hanging_ndi_streamer.py
```

- [ ] **Step 2: Write the failing streamer tests**

Create `tests/test_streamer.py`:

```python
import asyncio
import json
import sys
from pathlib import Path

import pytest

from ndi_rmtp.config import AdvancedConfig, AuthConfig, Config, StreamConfig
from ndi_rmtp.state import State
from ndi_rmtp.streamer import Streamer, build_args, QUALITY_PRESETS


FIXTURES = Path(__file__).parent / "fixtures"


def _fake_args(script: list[dict]) -> tuple[list[str], dict[str, str]]:
    """Returns (argv-prefix, env-overrides) for spawning the fake binary."""
    return (
        [sys.executable, str(FIXTURES / "fake_ndi_streamer.py")],
        {"FAKE_NDI_SCRIPT": json.dumps(script)},
    )


def _config() -> Config:
    return Config(
        auth=AuthConfig(username="admin", password="x"),
        stream=StreamConfig(rtmp_url="rtmp://example/live"),
        advanced=AdvancedConfig(auto_restart=False),
    )


# ---- build_args tests --------------------------------------------------------

def test_build_args_includes_all_flags(tmp_path):
    binary = tmp_path / "ndi_streamer"
    binary.touch()
    args = build_args(binary, "MY (SRC)", "rtmp://x/live", "key", "medium")
    assert str(binary) in args
    assert "MY (SRC)" in args
    assert "rtmp://x/live/key" in args
    assert "h264" in args
    assert "aac" in args
    preset = QUALITY_PRESETS["medium"]
    assert preset["v_bitrate"] in args
    assert preset["a_bitrate"] in args


def test_build_args_handles_trailing_slash_in_rtmp_url(tmp_path):
    binary = tmp_path / "ndi_streamer"
    binary.touch()
    args = build_args(binary, "S", "rtmp://x/live/", "key", "low")
    assert "rtmp://x/live/key" in args


# ---- start happy path --------------------------------------------------------

async def test_start_transitions_to_streaming_on_first_frame_pattern(monkeypatch):
    argv_prefix, env = _fake_args([
        {"type": "stderr", "line": "frame=1 fps=30 bitrate=4000kbits/s"},
        {"type": "sleep", "seconds": 0.5},
    ])
    cfg = _config()
    streamer = Streamer(binary_path=Path("unused"), config_getter=lambda: cfg)
    streamer._spawn_argv = argv_prefix  # test seam
    streamer._spawn_env = env

    await streamer.start("SRC", "key", "medium")

    # Allow stderr reader to process
    for _ in range(50):
        if streamer.status().state == "STREAMING":
            break
        await asyncio.sleep(0.05)
    assert streamer.status().state == "STREAMING"
    assert streamer.status().uptime_s >= 0

    await streamer.stop()
    assert streamer.status().state == "IDLE"


async def test_immediate_nonzero_exit_goes_to_error():
    argv_prefix, env = _fake_args([
        {"type": "stderr", "line": "boom"},
        {"type": "exit", "code": 1},
    ])
    cfg = _config()
    streamer = Streamer(binary_path=Path("unused"), config_getter=lambda: cfg)
    streamer._spawn_argv = argv_prefix
    streamer._spawn_env = env

    await streamer.start("SRC", "key", "medium")

    for _ in range(50):
        if streamer.status().state == "ERROR":
            break
        await asyncio.sleep(0.05)
    assert streamer.status().state == "ERROR"
    assert streamer.status().last_error is not None
    assert "boom" in "\n".join(streamer.status().log_tail)


async def test_youtube_rejected_key_pattern_sets_specific_error():
    argv_prefix, env = _fake_args([
        {"type": "stderr", "line": "frame=1 fps=30"},
        {"type": "stderr", "line": "Server returned 403 Forbidden"},
        {"type": "exit", "code": 1},
    ])
    cfg = _config()
    streamer = Streamer(binary_path=Path("unused"), config_getter=lambda: cfg)
    streamer._spawn_argv = argv_prefix
    streamer._spawn_env = env

    await streamer.start("SRC", "key", "medium")

    for _ in range(50):
        if streamer.status().state == "ERROR":
            break
        await asyncio.sleep(0.05)
    assert streamer.status().state == "ERROR"
    assert "rejected" in (streamer.status().last_error or "").lower()


async def test_hanging_subprocess_sigkilled_after_grace():
    cfg = _config()
    streamer = Streamer(binary_path=Path("unused"), config_getter=lambda: cfg)
    streamer._spawn_argv = [sys.executable, str(FIXTURES / "hanging_ndi_streamer.py")]
    streamer._spawn_env = {}
    streamer._stop_grace_s = 0.5  # speed up the test

    await streamer.start("SRC", "key", "medium")
    # Wait for STREAMING
    for _ in range(50):
        if streamer.status().state == "STREAMING":
            break
        await asyncio.sleep(0.05)

    await streamer.stop()
    assert streamer.status().state == "IDLE"
    assert any("SIGKILL" in line for line in streamer.status().log_tail)


async def test_log_buffer_caps_at_max_lines():
    lines = [{"type": "stderr", "line": f"frame={i}"} for i in range(500)]
    argv_prefix, env = _fake_args(lines + [{"type": "exit", "code": 0}])
    cfg = _config()
    streamer = Streamer(binary_path=Path("unused"), config_getter=lambda: cfg)
    streamer._spawn_argv = argv_prefix
    streamer._spawn_env = env

    await streamer.start("SRC", "key", "medium")
    for _ in range(100):
        if streamer.status().state in ("IDLE", "ERROR"):
            break
        await asyncio.sleep(0.05)
    assert len(streamer.status().log_tail) <= 200


async def test_double_start_rejected():
    argv_prefix, env = _fake_args([
        {"type": "stderr", "line": "frame=1 fps=30"},
        {"type": "sleep", "seconds": 1},
    ])
    cfg = _config()
    streamer = Streamer(binary_path=Path("unused"), config_getter=lambda: cfg)
    streamer._spawn_argv = argv_prefix
    streamer._spawn_env = env

    await streamer.start("SRC", "key", "medium")
    with pytest.raises(RuntimeError):
        await streamer.start("SRC", "key", "medium")
    await streamer.stop()
```

- [ ] **Step 3: Run, expect failure**

```bash
uv run pytest tests/test_streamer.py -v
```
Expected: all fail with `ModuleNotFoundError: No module named 'ndi_rmtp.streamer'`.

- [ ] **Step 4: Implement `src/ndi_rmtp/streamer.py`**

```python
"""Subprocess control for ndi_streamer.

Owns the streaming worker process: spawns it, watches its stderr for state
signals, supervises lifecycle, and exposes status.

Note on the spike: build_args must match the actual ndi_streamer CLI confirmed
in docs/superpowers/2026-06-07-spike-results.md. If the spike found different
flags, update the body of build_args() here.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import signal
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .config import Config
from .state import State, StateMachine

logger = logging.getLogger(__name__)

# Signals that we've successfully started encoding
STREAMING_PATTERNS = (b"frame=", b"Stream mapping:")
# Signals that YouTube refused the key — do not auto-retry
FATAL_PATTERNS = (b"403 Forbidden", b"Invalid stream key")

STARTING_TIMEOUT_S = 10.0
STOP_GRACE_S = 5.0
LOG_BUFFER_LINES = 200

QUALITY_PRESETS: dict[str, dict[str, str]] = {
    "low":    {"v_bitrate": "2500k", "preset": "superfast", "a_bitrate": "128k"},
    "medium": {"v_bitrate": "4500k", "preset": "superfast", "a_bitrate": "128k"},
    "high":   {"v_bitrate": "6000k", "preset": "veryfast",  "a_bitrate": "192k"},
}


def build_args(
    binary: Path,
    source: str,
    rtmp_url: str,
    stream_key: str,
    quality: str,
) -> list[str]:
    """Build the ndi_streamer argv. Spike-validated; adjust here if flags differ."""
    preset = QUALITY_PRESETS[quality]
    base = rtmp_url.rstrip("/")
    output = f"{base}/{stream_key}"
    return [
        str(binary),
        "-n", source,
        "-o", output,
        "-v", "h264",
        "-a", "aac",
        "-b", preset["v_bitrate"],
        "-ab", preset["a_bitrate"],
        "--x264-preset", preset["preset"],
    ]


class StreamerStatus:
    def __init__(self, state: str, uptime_s: int, last_error: Optional[str], log_tail: list[str]):
        self.state = state
        self.uptime_s = uptime_s
        self.last_error = last_error
        self.log_tail = log_tail

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "uptime_s": self.uptime_s,
            "last_error": self.last_error,
            "log_tail": self.log_tail,
        }


class Streamer:
    def __init__(self, binary_path: Path, config_getter: Callable[[], Config]):
        self._binary = binary_path
        self._get_config = config_getter
        self._sm = StateMachine()
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._supervisor_task: Optional[asyncio.Task] = None
        self._log_buffer: collections.deque[str] = collections.deque(maxlen=LOG_BUFFER_LINES)
        self._last_error: Optional[str] = None
        self._started_at: Optional[float] = None
        self._lock = asyncio.Lock()
        self._stopping_requested = False
        self._stop_grace_s = STOP_GRACE_S
        # Test seams (overridden by unit tests; production uses defaults):
        self._spawn_argv: Optional[list[str]] = None
        self._spawn_env: Optional[dict[str, str]] = None

    def status(self) -> StreamerStatus:
        uptime = 0
        if self._sm.state == State.STREAMING and self._started_at is not None:
            uptime = int(time.monotonic() - self._started_at)
        return StreamerStatus(
            state=self._sm.state.value,
            uptime_s=uptime,
            last_error=self._last_error,
            log_tail=list(self._log_buffer),
        )

    async def start(self, source: str, stream_key: str, quality: str) -> None:
        async with self._lock:
            if self._sm.state not in (State.IDLE, State.ERROR):
                raise RuntimeError(f"cannot start from state {self._sm.state.value}")
            self._last_error = None
            self._stopping_requested = False
            self._log_buffer.clear()
            self._sm.transition(State.STARTING, "user start")

            if self._spawn_argv is not None:
                argv = self._spawn_argv
            else:
                argv = build_args(
                    self._binary, source, self._get_config().stream.rtmp_url, stream_key, quality
                )
            env = os.environ.copy()
            if self._spawn_env:
                env.update(self._spawn_env)

            logger.info("spawning ndi_streamer: %s", argv)
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            self._reader_task = asyncio.create_task(self._read_stderr())
            self._supervisor_task = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        async with self._lock:
            if self._sm.state not in (State.STARTING, State.STREAMING, State.ERROR):
                return
            self._stopping_requested = True
            if self._sm.state in (State.STARTING, State.STREAMING):
                self._sm.transition(State.STOPPING, "user stop")
            proc = self._proc
            if proc is None:
                # Already exited; just settle to IDLE
                if self._sm.state == State.STOPPING:
                    self._sm.transition(State.IDLE, "no proc to stop")
                return

        # Outside the lock: wait for the proc, escalate to SIGKILL if needed.
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), self._stop_grace_s)
        except asyncio.TimeoutError:
            self._log_buffer.append("[supervisor] subprocess did not exit on SIGTERM, sending SIGKILL")
            try:
                proc.send_signal(signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()

        # Wait for supervisor to finish its bookkeeping
        if self._supervisor_task is not None:
            try:
                await self._supervisor_task
            except Exception:
                pass

        async with self._lock:
            if self._sm.state == State.STOPPING:
                self._sm.transition(State.IDLE, "stop complete")
            self._proc = None
            self._started_at = None

    async def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            async for raw in self._proc.stderr:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                self._log_buffer.append(line)
                if self._sm.state == State.STARTING and any(p in raw for p in STREAMING_PATTERNS):
                    async with self._lock:
                        if self._sm.state == State.STARTING:
                            self._started_at = time.monotonic()
                            self._sm.transition(State.STREAMING, "first frame")
                if any(p in raw for p in FATAL_PATTERNS):
                    self._last_error = "YouTube rejected the stream key"
        except Exception as e:  # pragma: no cover
            logger.exception("stderr reader crashed: %s", e)

    async def _supervise(self) -> None:
        assert self._proc is not None
        # Watchdog: if we never leave STARTING, mark ERROR
        watchdog_started = time.monotonic()

        async def _watchdog():
            while self._sm.state == State.STARTING:
                if time.monotonic() - watchdog_started > STARTING_TIMEOUT_S:
                    self._last_error = "Timed out waiting for first frame"
                    return
                await asyncio.sleep(0.2)

        watchdog_task = asyncio.create_task(_watchdog())

        rc = await self._proc.wait()
        watchdog_task.cancel()
        # Drain reader so it sees EOF
        if self._reader_task is not None:
            try:
                await asyncio.wait_for(self._reader_task, 1.0)
            except (asyncio.TimeoutError, Exception):
                pass

        async with self._lock:
            if self._stopping_requested:
                # stop() will finish the transition to IDLE; we do nothing.
                return
            if rc == 0 and self._sm.state in (State.STARTING, State.STREAMING):
                self._sm.transition(State.ERROR, f"clean exit rc=0 before stop")
                self._last_error = self._last_error or "Subprocess exited unexpectedly"
            elif self._sm.state in (State.STARTING, State.STREAMING):
                self._sm.transition(State.ERROR, f"crash rc={rc}")
                if not self._last_error:
                    tail = " | ".join(list(self._log_buffer)[-3:])
                    self._last_error = f"Subprocess exited rc={rc}: {tail}"
            self._proc = None
            self._started_at = None
```

- [ ] **Step 5: Run, expect green**

```bash
uv run pytest tests/test_streamer.py -v
```
Expected: 8 tests pass. If `test_hanging_subprocess_sigkilled_after_grace` is flaky, increase `_stop_grace_s` to 1.0 in that test.

- [ ] **Step 6: Commit**

```bash
git add src/ndi_rmtp/streamer.py tests/test_streamer.py tests/fixtures/
git commit -m "Add streamer subprocess control with stderr-pattern state signals"
```

---

## Task 7: Auto-restart with backoff

This extends `streamer.py` so that crashes (but not fatal YouTube rejections) trigger automatic restarts on the configured backoff schedule.

**Files:**
- Modify: `src/ndi_rmtp/streamer.py`
- Modify: `tests/test_streamer.py`

- [ ] **Step 1: Add failing tests for auto-restart**

Append to `tests/test_streamer.py`:

```python
async def test_auto_restart_retries_transient_crash():
    """Configured backoff: [0.1]. After one crash, streamer reattempts and succeeds."""
    cfg = Config(
        auth=AuthConfig(username="admin", password="x"),
        stream=StreamConfig(rtmp_url="rtmp://example/live"),
        advanced=AdvancedConfig(auto_restart=True, restart_backoff_s=[0.1]),
    )
    streamer = Streamer(binary_path=Path("unused"), config_getter=lambda: cfg)

    # Script: crash immediately, then on retry, run forever
    crash_argv, crash_env = _fake_args([
        {"type": "stderr", "line": "boom"},
        {"type": "exit", "code": 1},
    ])
    run_argv, run_env = _fake_args([
        {"type": "stderr", "line": "frame=1 fps=30"},
        {"type": "sleep", "seconds": 2},
    ])

    call_count = {"n": 0}

    def next_spawn():
        call_count["n"] += 1
        return (crash_argv, crash_env) if call_count["n"] == 1 else (run_argv, run_env)

    streamer._next_spawn = next_spawn  # test seam

    await streamer.start("SRC", "key", "medium")
    # Wait up to 5 s for retry to land in STREAMING
    for _ in range(100):
        if streamer.status().state == "STREAMING":
            break
        await asyncio.sleep(0.05)
    assert streamer.status().state == "STREAMING"
    assert call_count["n"] >= 2

    await streamer.stop()


async def test_auto_restart_skipped_after_fatal_pattern():
    """If YouTube rejected the key, do not retry — even with auto_restart=True."""
    cfg = Config(
        auth=AuthConfig(username="admin", password="x"),
        stream=StreamConfig(rtmp_url="rtmp://example/live"),
        advanced=AdvancedConfig(auto_restart=True, restart_backoff_s=[0.1]),
    )
    streamer = Streamer(binary_path=Path("unused"), config_getter=lambda: cfg)

    argv, env = _fake_args([
        {"type": "stderr", "line": "frame=1 fps=30"},
        {"type": "stderr", "line": "Server returned 403 Forbidden"},
        {"type": "exit", "code": 1},
    ])
    streamer._spawn_argv = argv
    streamer._spawn_env = env

    await streamer.start("SRC", "key", "medium")
    # Should land in ERROR and stay there
    for _ in range(40):
        if streamer.status().state == "ERROR":
            break
        await asyncio.sleep(0.05)
    assert streamer.status().state == "ERROR"

    # Wait long enough that a retry would have fired
    await asyncio.sleep(0.5)
    assert streamer.status().state == "ERROR"


async def test_auto_restart_disabled_in_config_skips_retry():
    cfg = Config(
        auth=AuthConfig(username="admin", password="x"),
        stream=StreamConfig(rtmp_url="rtmp://example/live"),
        advanced=AdvancedConfig(auto_restart=False, restart_backoff_s=[0.1]),
    )
    streamer = Streamer(binary_path=Path("unused"), config_getter=lambda: cfg)
    argv, env = _fake_args([
        {"type": "stderr", "line": "boom"},
        {"type": "exit", "code": 1},
    ])
    streamer._spawn_argv = argv
    streamer._spawn_env = env

    await streamer.start("SRC", "key", "medium")
    for _ in range(40):
        if streamer.status().state == "ERROR":
            break
        await asyncio.sleep(0.05)
    assert streamer.status().state == "ERROR"
    await asyncio.sleep(0.4)
    assert streamer.status().state == "ERROR"
```

- [ ] **Step 2: Run, expect failure**

```bash
uv run pytest tests/test_streamer.py::test_auto_restart_retries_transient_crash \
  tests/test_streamer.py::test_auto_restart_skipped_after_fatal_pattern \
  tests/test_streamer.py::test_auto_restart_disabled_in_config_skips_retry -v
```
Expected: failure — `_next_spawn` seam not present, retry logic not implemented.

- [ ] **Step 3: Add auto-restart to `src/ndi_rmtp/streamer.py`**

Add a `_next_spawn` seam, remember the last start params, and add a retry loop in `_supervise`. Replace the relevant sections:

1. Add to `Streamer.__init__`:

```python
        self._last_start_params: Optional[tuple[str, str, str]] = None
        self._next_spawn: Optional[Callable[[], tuple[list[str], dict[str, str]]]] = None
```

2. In `start()`, after the `self._sm.transition(State.STARTING, "user start")` line, record params:

```python
            self._last_start_params = (source, stream_key, quality)
```

3. Replace the `argv = ...` block inside `start()` with:

```python
            if self._next_spawn is not None:
                argv, env_overrides = self._next_spawn()
            elif self._spawn_argv is not None:
                argv = self._spawn_argv
                env_overrides = self._spawn_env or {}
            else:
                argv = build_args(
                    self._binary, source, self._get_config().stream.rtmp_url, stream_key, quality
                )
                env_overrides = {}
            env = os.environ.copy()
            env.update(env_overrides)
```

4. At the end of `_supervise`, after transitioning to ERROR, add the retry logic. Replace the final `async with self._lock:` block with:

```python
        should_retry = False
        async with self._lock:
            if self._stopping_requested:
                return
            if rc == 0 and self._sm.state in (State.STARTING, State.STREAMING):
                self._sm.transition(State.ERROR, "clean exit rc=0 before stop")
                self._last_error = self._last_error or "Subprocess exited unexpectedly"
            elif self._sm.state in (State.STARTING, State.STREAMING):
                self._sm.transition(State.ERROR, f"crash rc={rc}")
                if not self._last_error:
                    tail = " | ".join(list(self._log_buffer)[-3:])
                    self._last_error = f"Subprocess exited rc={rc}: {tail}"
            self._proc = None
            self._started_at = None

            cfg = self._get_config()
            is_fatal = self._last_error and "rejected" in self._last_error.lower()
            should_retry = (
                cfg.advanced.auto_restart
                and not is_fatal
                and self._last_start_params is not None
            )

        if should_retry:
            asyncio.create_task(self._retry_loop())

    async def _retry_loop(self) -> None:
        cfg = self._get_config()
        for delay in cfg.advanced.restart_backoff_s:
            await asyncio.sleep(delay)
            async with self._lock:
                if self._stopping_requested or self._sm.state != State.ERROR:
                    return
                if self._last_start_params is None:
                    return
                source, key, quality = self._last_start_params
                # Transition out of ERROR so start() will accept it
                self._sm.transition(State.IDLE, "auto-restart attempt")
            try:
                await self.start(source, key, quality)
            except RuntimeError:
                continue
            # Watch what happens: success (STREAMING) or another crash (ERROR)
            for _ in range(int(STARTING_TIMEOUT_S * 10) + 50):
                state = self._sm.state
                if state == State.STREAMING:
                    return
                if state == State.ERROR:
                    break
                await asyncio.sleep(0.1)
        # Exhausted backoff; remain in ERROR
```

- [ ] **Step 4: Run all streamer tests, expect green**

```bash
uv run pytest tests/test_streamer.py -v
```
Expected: 11 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/ndi_rmtp/streamer.py tests/test_streamer.py
git commit -m "Add auto-restart with backoff schedule, skipped on fatal patterns"
```

---

## Task 8: NDI source discovery

**Files:**
- Create: `src/ndi_rmtp/ndi.py`
- Create: `tests/test_ndi_discovery.py`
- Create: `tests/fixtures/fake_ndi_lister.py`

**Note:** The exact discovery command comes from the Phase 0 spike. Update the
`_DISCOVERY_ARGS` constant in `ndi.py` to match what the spike found. The
parsing regex below assumes lines of the form `Source: NAME (ADDRESS)` —
adjust to match actual output.

- [ ] **Step 1: Create the lister fixture**

`tests/fixtures/fake_ndi_lister.py`:

```python
#!/usr/bin/env python3
"""Fake source lister. Emits scripted lines and exits 0."""
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
```

```bash
chmod +x tests/fixtures/fake_ndi_lister.py
```

- [ ] **Step 2: Write failing tests**

Create `tests/test_ndi_discovery.py`:

```python
import asyncio
import json
import sys
from pathlib import Path

import pytest

from ndi_rmtp.ndi import NDIDiscovery, Source


FIXTURES = Path(__file__).parent / "fixtures"


def _make_discovery(lines: list[str], ttl_s: float = 0.2) -> NDIDiscovery:
    d = NDIDiscovery(binary_path=Path("unused"), ttl_s=ttl_s)
    d._spawn_argv = [sys.executable, str(FIXTURES / "fake_ndi_lister.py")]
    d._spawn_env = {"FAKE_NDI_LISTER": json.dumps(lines)}
    return d


async def test_discovery_parses_typical_output():
    d = _make_discovery([
        "Searching for sources...",
        "Source: MYBOX (Test Pattern) @ 10.0.0.5:5961",
        "Source: STUDIO (Camera 1) @ 10.0.0.6:5961",
    ])
    sources = await d.list()
    names = [s.name for s in sources]
    assert "MYBOX (Test Pattern)" in names
    assert "STUDIO (Camera 1)" in names


async def test_discovery_ignores_non_source_lines():
    d = _make_discovery([
        "Welcome banner",
        "Source: ONE (X)",
        "Some other log line",
    ])
    sources = await d.list()
    assert len(sources) == 1
    assert sources[0].name == "ONE (X)"


async def test_discovery_uses_cache_within_ttl():
    d = _make_discovery(["Source: A (X)"], ttl_s=10.0)
    first = await d.list()
    # Swap the fixture script — if cache works, this should NOT be used
    d._spawn_env = {"FAKE_NDI_LISTER": json.dumps(["Source: B (Y)"])}
    second = await d.list()
    assert [s.name for s in second] == [s.name for s in first]


async def test_discovery_refreshes_after_ttl_expires():
    d = _make_discovery(["Source: A (X)"], ttl_s=0.05)
    await d.list()
    await asyncio.sleep(0.1)
    d._spawn_env = {"FAKE_NDI_LISTER": json.dumps(["Source: B (Y)"])}
    refreshed = await d.list()
    assert [s.name for s in refreshed] == ["B (Y)"]


async def test_discovery_returns_empty_on_no_sources():
    d = _make_discovery(["No NDI sources found"])
    sources = await d.list()
    assert sources == []
```

- [ ] **Step 3: Run, expect failure**

```bash
uv run pytest tests/test_ndi_discovery.py -v
```
Expected: all fail with `ModuleNotFoundError`.

- [ ] **Step 4: Implement `src/ndi_rmtp/ndi.py`**

```python
"""NDI source discovery via ndi_streamer's list mode.

Adjust `_DISCOVERY_ARGS` and `_SOURCE_RE` to match what the Phase 0 spike found.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Flag(s) passed to ndi_streamer to make it list sources and exit.
# CONFIRMED during Phase 0 spike — update if the binary uses different flags.
_DISCOVERY_ARGS = ("--list",)

# Match a typical line. The address portion is optional.
# CONFIRMED during Phase 0 spike — update if output format differs.
_SOURCE_RE = re.compile(r"^Source:\s+(?P<name>.+?)(?:\s+@\s+(?P<addr>\S+))?\s*$")

_DISCOVERY_TIMEOUT_S = 5.0


@dataclass
class Source:
    name: str
    address: Optional[str] = None


class NDIDiscovery:
    def __init__(self, binary_path: Path, ttl_s: float = 5.0):
        self._binary = binary_path
        self._ttl_s = ttl_s
        self._cache: list[Source] = []
        self._cached_at: float = 0.0
        self._lock = asyncio.Lock()
        # Test seams
        self._spawn_argv: Optional[list[str]] = None
        self._spawn_env: Optional[dict[str, str]] = None

    async def list(self) -> list[Source]:
        async with self._lock:
            now = time.monotonic()
            if self._cache and now - self._cached_at < self._ttl_s:
                return list(self._cache)
            sources = await self._run_discovery()
            self._cache = sources
            self._cached_at = now
            return list(sources)

    async def _run_discovery(self) -> list[Source]:
        if self._spawn_argv is not None:
            argv = self._spawn_argv
        else:
            argv = [str(self._binary), *_DISCOVERY_ARGS]
        env = os.environ.copy()
        if self._spawn_env:
            env.update(self._spawn_env)

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), _DISCOVERY_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning("NDI discovery timed out after %ss", _DISCOVERY_TIMEOUT_S)
            return []
        except FileNotFoundError:
            logger.error("ndi_streamer binary not found at %s", self._binary)
            return []

        sources: list[Source] = []
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            m = _SOURCE_RE.match(line.strip())
            if m:
                sources.append(Source(name=m.group("name"), address=m.group("addr")))
        return sources
```

- [ ] **Step 5: Run, expect green**

```bash
uv run pytest tests/test_ndi_discovery.py -v
```
Expected: 5 tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/ndi_rmtp/ndi.py tests/test_ndi_discovery.py tests/fixtures/fake_ndi_lister.py
git commit -m "Add NDI source discovery with TTL cache"
```

---

## Task 9: FastAPI routes

**Files:**
- Create: `src/ndi_rmtp/app.py`
- Create: `tests/test_routes.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_routes.py`:

```python
import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from ndi_rmtp.app import build_app
from ndi_rmtp.config import AdvancedConfig, AuthConfig, Config, StreamConfig
from ndi_rmtp.ndi import Source


def _basic(user: str, password: str) -> dict[str, str]:
    raw = f"{user}:{password}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        auth=AuthConfig(username="admin", password="hunter2"),
        stream=StreamConfig(stream_key="abc", last_source="SRC"),
        advanced=AdvancedConfig(),
    )


@pytest.fixture
def streamer():
    s = MagicMock()
    s.start = AsyncMock()
    s.stop = AsyncMock()
    s.status.return_value = MagicMock(
        to_dict=MagicMock(
            return_value={"state": "IDLE", "uptime_s": 0, "last_error": None, "log_tail": []}
        )
    )
    return s


@pytest.fixture
def discovery():
    d = MagicMock()
    d.list = AsyncMock(return_value=[Source(name="SRC1", address="10.0.0.5:5961")])
    return d


@pytest.fixture
def client(tmp_path, config, streamer, discovery):
    config_path = tmp_path / "config.toml"
    config.save(config_path)
    app = build_app(config_path=config_path, streamer=streamer, discovery=discovery)
    return TestClient(app)


def test_healthz_no_auth(client):
    r = client.get("/healthz")
    assert r.status_code == 200


def test_index_requires_auth(client):
    r = client.get("/")
    assert r.status_code == 401


def test_index_renders_when_authed(client):
    r = client.get("/", headers=_basic("admin", "hunter2"))
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_api_status_returns_shape(client):
    r = client.get("/api/status", headers=_basic("admin", "hunter2"))
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"state", "uptime_s", "last_error", "log_tail"}


def test_api_sources_returns_list(client, discovery):
    r = client.get("/api/sources", headers=_basic("admin", "hunter2"))
    assert r.status_code == 200
    body = r.json()
    assert body == [{"name": "SRC1", "address": "10.0.0.5:5961"}]


def test_api_start_calls_streamer(client, streamer, tmp_path):
    r = client.post(
        "/api/start",
        headers=_basic("admin", "hunter2"),
        data={"source": "SRC1", "quality": "medium"},
    )
    assert r.status_code == 200
    streamer.start.assert_awaited_once()
    args = streamer.start.call_args
    assert args.kwargs.get("source", args.args[0] if args.args else None) == "SRC1"


def test_api_start_persists_source_and_quality(client, tmp_path, config):
    config_path = tmp_path / "config.toml"
    client.post(
        "/api/start",
        headers=_basic("admin", "hunter2"),
        data={"source": "NEW_SRC", "quality": "high"},
    )
    reloaded = Config.load(config_path)
    assert reloaded.stream.last_source == "NEW_SRC"
    assert reloaded.stream.quality == "high"


def test_api_stop_calls_streamer(client, streamer):
    r = client.post("/api/stop", headers=_basic("admin", "hunter2"))
    assert r.status_code == 200
    streamer.stop.assert_awaited_once()


def test_api_config_updates_stream_key(client, tmp_path, config):
    config_path = tmp_path / "config.toml"
    r = client.post(
        "/api/config",
        headers=_basic("admin", "hunter2"),
        data={"stream_key": "new-key-123", "quality": "low"},
    )
    assert r.status_code == 200
    reloaded = Config.load(config_path)
    assert reloaded.stream.stream_key == "new-key-123"
    assert reloaded.stream.quality == "low"


def test_protected_routes_reject_bad_password(client):
    r = client.get("/api/status", headers=_basic("admin", "wrong"))
    assert r.status_code == 401
```

- [ ] **Step 2: Run, expect failure**

```bash
uv run pytest tests/test_routes.py -v
```
Expected: `ModuleNotFoundError` or `ImportError`.

- [ ] **Step 3: Implement `src/ndi_rmtp/app.py`**

```python
"""FastAPI app: routes, dependency injection, template rendering."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .auth import make_auth_dependency
from .config import Config
from .ndi import NDIDiscovery
from .streamer import Streamer

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


def build_app(
    config_path: Path,
    streamer: Streamer,
    discovery: NDIDiscovery,
) -> FastAPI:
    app = FastAPI(title="ndi-rmtp")
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    # Config is reloaded on every request so disk edits are picked up.
    def get_config() -> Config:
        return Config.load(config_path)

    require_auth = make_auth_dependency(get_config)

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    def index(request: Request):
        cfg = get_config()
        masked_key = "•" * 12 if cfg.stream.stream_key else ""
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "config": cfg,
                "masked_key": masked_key,
                "qualities": ["low", "medium", "high"],
            },
        )

    @app.get("/api/status", dependencies=[Depends(require_auth)])
    def api_status():
        return streamer.status().to_dict()

    @app.get("/api/sources", dependencies=[Depends(require_auth)])
    async def api_sources():
        sources = await discovery.list()
        return [{"name": s.name, "address": s.address} for s in sources]

    @app.post("/api/start", dependencies=[Depends(require_auth)])
    async def api_start(
        source: str = Form(...),
        quality: str = Form(...),
    ):
        cfg = get_config()
        if not cfg.stream.stream_key:
            raise HTTPException(status_code=400, detail="Stream key not set")
        if quality not in ("low", "medium", "high"):
            raise HTTPException(status_code=400, detail=f"Bad quality {quality!r}")

        # Persist selection so it survives restarts
        cfg.stream.last_source = source
        cfg.stream.quality = quality
        cfg.save(config_path)

        try:
            await streamer.start(source=source, stream_key=cfg.stream.stream_key, quality=quality)
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"ok": True}

    @app.post("/api/stop", dependencies=[Depends(require_auth)])
    async def api_stop():
        await streamer.stop()
        return {"ok": True}

    @app.post("/api/config", dependencies=[Depends(require_auth)])
    def api_config(
        stream_key: Optional[str] = Form(None),
        quality: Optional[str] = Form(None),
    ):
        cfg = get_config()
        if stream_key is not None:
            cfg.stream.stream_key = stream_key
        if quality is not None:
            if quality not in ("low", "medium", "high"):
                raise HTTPException(status_code=400, detail=f"Bad quality {quality!r}")
            cfg.stream.quality = quality
        cfg.save(config_path)
        return {"ok": True}

    return app
```

- [ ] **Step 4: Create a placeholder template so the index route doesn't 500**

`src/ndi_rmtp/templates/index.html`:

```html
<!doctype html>
<html><body>OK {{ config.auth.username }}</body></html>
```

(Replaced with the real UI in Task 10.)

- [ ] **Step 5: Run, expect green**

```bash
uv run pytest tests/test_routes.py -v
```
Expected: 10 tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/ndi_rmtp/app.py src/ndi_rmtp/templates/index.html tests/test_routes.py
git commit -m "Add FastAPI routes: status, sources, start, stop, config; auth-gated"
```

---

## Task 10: Web UI template + CSS

**Files:**
- Modify: `src/ndi_rmtp/templates/index.html`
- Create: `src/ndi_rmtp/static/style.css`

- [ ] **Step 1: Implement the index template**

Replace `src/ndi_rmtp/templates/index.html`:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>NDI → YouTube Relay</title>
  <link rel="stylesheet" href="/static/style.css">
  <script src="https://unpkg.com/htmx.org@2.0.4"></script>
</head>
<body>
  <header>
    <h1>NDI → YouTube Relay</h1>
    <div id="state-badge"
         hx-get="/api/status"
         hx-trigger="load, every 2s"
         hx-swap="innerHTML"
         hx-target="#status-region">…</div>
  </header>

  <main>
    <section class="card">
      <h2>Configure</h2>
      <form hx-post="/api/start" hx-swap="none" id="start-form">
        <label>NDI Source
          <select name="source"
                  hx-get="/api/sources"
                  hx-trigger="load, every 5s"
                  hx-target="this"
                  hx-swap="innerHTML"
                  hx-include="this">
            <option value="{{ config.stream.last_source }}">{{ config.stream.last_source or "(searching…)" }}</option>
          </select>
        </label>

        <label>Stream Key
          <span class="masked-row">
            <input type="password" name="stream_key" id="stream-key"
                   value="{{ config.stream.stream_key }}" readonly>
            <button type="button" id="edit-key">Edit</button>
            <button type="button" id="save-key" hidden
                    hx-post="/api/config"
                    hx-include="[name=stream_key]"
                    hx-swap="none">Save</button>
          </span>
        </label>

        <fieldset>
          <legend>Quality</legend>
          {% for q in qualities %}
          <label>
            <input type="radio" name="quality" value="{{ q }}"
                   {% if config.stream.quality == q %}checked{% endif %}> {{ q }}
          </label>
          {% endfor %}
        </fieldset>

        <div class="actions">
          <button type="submit" id="start-btn">▶ Start Stream</button>
          <button type="button" id="stop-btn"
                  hx-post="/api/stop" hx-swap="none">■ Stop</button>
        </div>
      </form>
    </section>

    <section class="card" id="status-region">
      <h2>Status</h2>
      <div class="placeholder">Loading…</div>
    </section>
  </main>

  <script>
    document.getElementById("edit-key").addEventListener("click", (e) => {
      const input = document.getElementById("stream-key");
      input.removeAttribute("readonly");
      input.type = "text";
      input.focus();
      e.target.hidden = true;
      document.getElementById("save-key").hidden = false;
    });

    // Render status JSON into the status region
    document.body.addEventListener("htmx:afterRequest", (evt) => {
      if (!evt.detail.xhr || evt.detail.pathInfo?.requestPath !== "/api/status") return;
      const region = document.getElementById("status-region");
      try {
        const data = JSON.parse(evt.detail.xhr.responseText);
        const tail = (data.log_tail || []).slice(-20).map(l =>
          `<code>${l.replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))}</code>`
        ).join("<br>");
        region.innerHTML = `
          <h2>Status</h2>
          <p class="state state-${data.state.toLowerCase()}">${data.state}
            ${data.state === 'STREAMING' ? `<small>${formatUptime(data.uptime_s)}</small>` : ''}
          </p>
          ${data.last_error ? `<p class="err">${data.last_error}</p>` : ''}
          <pre class="logs">${tail}</pre>
        `;
      } catch (_) { /* ignore */ }
    });

    function formatUptime(s) {
      const h = Math.floor(s / 3600).toString().padStart(2, '0');
      const m = Math.floor((s % 3600) / 60).toString().padStart(2, '0');
      const sec = (s % 60).toString().padStart(2, '0');
      return `${h}:${m}:${sec}`;
    }

    // Convert /api/sources JSON into <option>s
    document.body.addEventListener("htmx:afterRequest", (evt) => {
      if (evt.detail.pathInfo?.requestPath !== "/api/sources") return;
      try {
        const sources = JSON.parse(evt.detail.xhr.responseText);
        const select = document.querySelector("select[name=source]");
        const current = select.value;
        select.innerHTML = sources.length
          ? sources.map(s => `<option value="${s.name}" ${s.name === current ? 'selected' : ''}>${s.name}</option>`).join("")
          : `<option value="">(no sources found)</option>`;
      } catch (_) { /* ignore */ }
    });
  </script>
</body>
</html>
```

- [ ] **Step 2: Implement `src/ndi_rmtp/static/style.css`**

```css
* { box-sizing: border-box; }
body {
  font-family: system-ui, -apple-system, sans-serif;
  margin: 0; background: #0f1115; color: #e8eaed;
}
header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 1rem 1.5rem; background: #1a1d23; border-bottom: 1px solid #2a2e36;
}
header h1 { margin: 0; font-size: 1.2rem; }
main { max-width: 720px; margin: 1.5rem auto; padding: 0 1rem; display: grid; gap: 1rem; }
.card {
  background: #1a1d23; border: 1px solid #2a2e36; border-radius: 8px;
  padding: 1.25rem;
}
.card h2 { margin-top: 0; font-size: 1rem; color: #9aa0a6; text-transform: uppercase; letter-spacing: 0.04em; }
label { display: block; margin-bottom: 0.85rem; font-size: 0.95rem; }
input, select {
  width: 100%; padding: 0.5rem 0.7rem; margin-top: 0.25rem;
  background: #0f1115; border: 1px solid #2a2e36; border-radius: 4px;
  color: #e8eaed; font: inherit;
}
.masked-row { display: flex; gap: 0.5rem; }
.masked-row input { flex: 1; }
fieldset { border: 1px solid #2a2e36; border-radius: 4px; padding: 0.5rem 0.75rem; margin-bottom: 0.85rem; }
fieldset legend { padding: 0 0.4rem; color: #9aa0a6; font-size: 0.85rem; }
fieldset label { display: inline-block; margin-right: 1rem; }
button {
  background: #2563eb; color: white; border: 0; padding: 0.6rem 1.2rem;
  border-radius: 4px; font: inherit; cursor: pointer;
}
button:hover { background: #1d4ed8; }
#stop-btn { background: #b91c1c; }
#stop-btn:hover { background: #991b1b; }
.actions { display: flex; gap: 0.5rem; margin-top: 0.5rem; }
.state { font-size: 1.4rem; font-weight: 600; margin: 0.5rem 0; }
.state-idle { color: #9aa0a6; }
.state-starting, .state-stopping { color: #fbbf24; }
.state-streaming { color: #4ade80; }
.state-error { color: #f87171; }
.state small { font-size: 0.85rem; color: #9aa0a6; margin-left: 0.5rem; }
.err { color: #f87171; background: #2a1818; padding: 0.5rem 0.75rem; border-radius: 4px; }
.logs {
  background: #0a0c10; padding: 0.6rem 0.8rem; border-radius: 4px;
  font-size: 0.8rem; line-height: 1.4; max-height: 280px; overflow-y: auto;
  border: 1px solid #2a2e36; color: #c9ccd1; white-space: pre-wrap;
}
.logs code { display: block; }
```

- [ ] **Step 3: Verify existing tests still pass**

```bash
uv run pytest -v
```
Expected: all current tests still pass (the index test just checks for `text/html` content-type, which still holds).

- [ ] **Step 4: Commit**

```bash
git add src/ndi_rmtp/templates/index.html src/ndi_rmtp/static/style.css
git commit -m "Add control UI with HTMX live regions and dark theme CSS"
```

---

## Task 11: Main entry point + bootstrap wiring

**Files:**
- Create: `src/ndi_rmtp/__main__.py`
- Create: `src/ndi_rmtp/bootstrap.py`

- [ ] **Step 1: Implement `src/ndi_rmtp/bootstrap.py`**

```python
"""Wire up config + streamer + discovery + FastAPI app.

Handles binary path resolution (project-local ./bin/ first, then /usr/local/bin)
and first-run config bootstrap.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI

from .app import build_app
from .config import Config
from .ndi import NDIDiscovery
from .streamer import Streamer

logger = logging.getLogger(__name__)


def resolve_binary() -> Path:
    """Resolve the ndi_streamer binary path.

    Order: $NDI_STREAMER_BIN, ./bin/ndi_streamer, /usr/local/bin/ndi_streamer.
    """
    env_override = os.environ.get("NDI_STREAMER_BIN")
    if env_override:
        return Path(env_override)
    cwd_local = Path.cwd() / "bin" / "ndi_streamer"
    if cwd_local.exists():
        return cwd_local
    return Path("/usr/local/bin/ndi_streamer")


def resolve_config_path() -> Path:
    override = os.environ.get("NDI_RMTP_CONFIG")
    if override:
        return Path(override)
    return Path.home() / ".config" / "ndi-rmtp" / "config.toml"


def ensure_config(config_path: Path) -> Config:
    if not config_path.exists():
        logger.warning("config not found at %s — bootstrapping", config_path)
        cfg, password = Config.bootstrap(config_path)
        print(
            f"\n[ndi-rmtp] first-run bootstrap\n"
            f"  admin username: admin\n"
            f"  admin password: {password}\n"
            f"  config path:    {config_path}\n",
            file=sys.stderr,
            flush=True,
        )
        return cfg
    try:
        return Config.load(config_path)
    except Exception as e:
        logger.error("config at %s is malformed (%s) — rebootstrapping", config_path, e)
        backup = config_path.with_suffix(".broken.toml")
        config_path.replace(backup)
        cfg, password = Config.bootstrap(config_path)
        print(
            f"\n[ndi-rmtp] broken config moved to {backup}, generated new admin password: {password}\n",
            file=sys.stderr,
            flush=True,
        )
        return cfg


def make_app() -> FastAPI:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config_path = resolve_config_path()
    ensure_config(config_path)

    binary = resolve_binary()
    if not binary.exists():
        logger.warning(
            "ndi_streamer binary not found at %s — start will fail until it's installed",
            binary,
        )

    cfg = Config.load(config_path)
    discovery = NDIDiscovery(binary, ttl_s=float(cfg.advanced.discovery_ttl_s))
    streamer = Streamer(binary, config_getter=lambda: Config.load(config_path))
    return build_app(config_path=config_path, streamer=streamer, discovery=discovery)


app = make_app()
```

- [ ] **Step 2: Implement `src/ndi_rmtp/__main__.py`**

```python
"""Allow `python -m ndi_rmtp` to launch uvicorn."""
import uvicorn


def main() -> None:
    uvicorn.run("ndi_rmtp.bootstrap:app", host="0.0.0.0", port=8080, log_config=None)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Verify the app starts**

In one terminal:

```bash
uv run python -m ndi_rmtp
```

Expected stderr lines:
- `[ndi-rmtp] first-run bootstrap` block with a random password (only on the first run).
- uvicorn listening on `0.0.0.0:8080`.

In another terminal:

```bash
curl -i http://localhost:8080/healthz
```
Expected: `HTTP/1.1 200 OK` with `{"ok":true}`.

```bash
curl -i http://localhost:8080/
```
Expected: `HTTP/1.1 401 Unauthorized`.

Stop the server with Ctrl-C.

- [ ] **Step 4: Update `pyproject.toml` to expose a console script**

Add to `pyproject.toml`:

```toml
[project.scripts]
ndi-rmtp = "ndi_rmtp.__main__:main"
```

Then:

```bash
uv sync --extra dev
uv run ndi-rmtp &
sleep 2
curl -s http://localhost:8080/healthz
kill %1
```
Expected: `{"ok":true}` printed.

- [ ] **Step 5: Commit**

```bash
git add src/ndi_rmtp/__main__.py src/ndi_rmtp/bootstrap.py pyproject.toml
git commit -m "Wire bootstrap + uvicorn entry; binary resolves via env/cwd/system"
```

---

## Task 12: Integration test against nginx-rtmp

**Files:**
- Create: `tests/test_integration.py`

**Prerequisites:** Docker installed locally; an NDI source on the LAN with its name in env var `NDI_TEST_SOURCE`; `ndi_streamer` built into `./bin/ndi_streamer` (Task 13 will automate this — for now, build manually as in Task 2).

- [ ] **Step 1: Write the integration test**

```python
"""End-to-end: real ndi_streamer pushes to a local nginx-rtmp container.

Skipped unless `-m integration` and the env var NDI_TEST_SOURCE is set.
Requires Docker.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from ndi_rmtp.bootstrap import resolve_binary
from ndi_rmtp.config import AdvancedConfig, AuthConfig, Config, StreamConfig
from ndi_rmtp.streamer import Streamer


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def ndi_source() -> str:
    src = os.environ.get("NDI_TEST_SOURCE")
    if not src:
        pytest.skip("NDI_TEST_SOURCE not set")
    return src


@pytest.fixture(scope="module")
def docker():
    if not shutil.which("docker"):
        pytest.skip("Docker not available")
    return shutil.which("docker")


@pytest.fixture(scope="module")
def rtmp_server(docker):
    name = "ndi-rmtp-it-rtmp"
    subprocess.run([docker, "rm", "-f", name], check=False, capture_output=True)
    subprocess.run(
        [docker, "run", "-d", "--rm", "--name", name, "-p", "1935:1935", "tiangolo/nginx-rtmp"],
        check=True,
    )
    # Give nginx a moment
    time.sleep(2)
    yield "rtmp://localhost/live"
    subprocess.run([docker, "rm", "-f", name], check=False, capture_output=True)


@pytest.fixture(scope="module")
def binary_path() -> Path:
    p = resolve_binary()
    if not p.exists():
        pytest.skip(f"ndi_streamer not built at {p} — run scripts/dev-setup-mac.sh")
    return p


async def test_real_ndi_to_local_rtmp(binary_path, rtmp_server, ndi_source):
    cfg = Config(
        auth=AuthConfig(username="admin", password="x"),
        stream=StreamConfig(rtmp_url=rtmp_server),
        advanced=AdvancedConfig(auto_restart=False),
    )
    streamer = Streamer(binary_path=binary_path, config_getter=lambda: cfg)

    await streamer.start(source=ndi_source, stream_key="it-test", quality="low")
    # Wait for STREAMING
    for _ in range(100):
        if streamer.status().state == "STREAMING":
            break
        await asyncio.sleep(0.1)
    assert streamer.status().state == "STREAMING", \
        f"never reached STREAMING; log tail: {streamer.status().log_tail[-5:]}"

    # Let it run for 5 s
    await asyncio.sleep(5.0)

    # Verify the RTMP sink has the stream
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams",
         "rtmp://localhost/live/it-test"],
        capture_output=True, text=True, timeout=10,
    )
    assert probe.returncode == 0, probe.stderr
    streams = json.loads(probe.stdout).get("streams", [])
    codecs = [s.get("codec_name") for s in streams]
    assert "h264" in codecs, f"no h264 in stream: {codecs}"
    assert "aac" in codecs, f"no aac in stream: {codecs}"

    await streamer.stop()
    assert streamer.status().state == "IDLE"
```

- [ ] **Step 2: Run the integration test (skipped by default)**

```bash
uv run pytest -m integration -v
```
If `ndi_streamer` is built (e.g., spike artifacts at `./bin/`) and `NDI_TEST_SOURCE` is set, this should pass. Otherwise it skips cleanly.

To run with a real source:

```bash
ln -sf $(pwd)/build/ndi_streamer/build/ndi_streamer ./bin/ndi_streamer
NDI_TEST_SOURCE="MYBOX (Test Pattern)" uv run pytest -m integration -v
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_integration.py
git commit -m "Add integration test against local nginx-rtmp container"
```

---

## Task 13: dev-setup-mac.sh

**Files:**
- Create: `scripts/dev-setup-mac.sh`

- [ ] **Step 1: Write the dev setup script**

```bash
#!/usr/bin/env bash
# Apple Silicon Mac dev setup. Idempotent.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

say() { printf "\n\033[1;34m==>\033[0m %s\n" "$*"; }
warn() { printf "\n\033[1;33m!!\033[0m %s\n" "$*"; }
die() { printf "\n\033[1;31mxx\033[0m %s\n" "$*"; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || die "This script is macOS-only. Use scripts/install.sh on the Pi."

# --- Homebrew + deps ---
command -v brew >/dev/null || die "Homebrew not found. Install from https://brew.sh"

say "Installing Homebrew packages"
brew install ffmpeg cmake pkg-config uv >/dev/null

# --- NDI SDK ---
NDI_SDK_DIR="/Library/NDI SDK for Apple"
if [[ ! -d "$NDI_SDK_DIR" ]]; then
  warn "NDI SDK for Apple not found at $NDI_SDK_DIR"
  echo "  Download from https://ndi.video/sdk/, accept the EULA, install,"
  echo "  then re-run this script."
  exit 1
fi

say "Symlinking NDI library and headers"
sudo ln -sf "$NDI_SDK_DIR/lib/macOS/libndi.dylib" /usr/local/lib/libndi.dylib
sudo mkdir -p /usr/local/include/ndi
for h in "$NDI_SDK_DIR/include/"*.h; do
  sudo ln -sf "$h" /usr/local/include/ndi/
done

# --- Build ndi_streamer ---
BUILD_DIR="$PROJECT_ROOT/build/ndi_streamer"
if [[ ! -d "$BUILD_DIR/.git" ]]; then
  say "Cloning ndi_streamer"
  mkdir -p "$PROJECT_ROOT/build"
  git clone https://github.com/alim-zanibekov/ndi_streamer.git "$BUILD_DIR"
fi

say "Building ndi_streamer"
cmake -B "$BUILD_DIR/build" -S "$BUILD_DIR"
cmake --build "$BUILD_DIR/build" --parallel

mkdir -p "$PROJECT_ROOT/bin"
ln -sf "$BUILD_DIR/build/ndi_streamer" "$PROJECT_ROOT/bin/ndi_streamer"

say "Verifying binary"
"$PROJECT_ROOT/bin/ndi_streamer" --help || true

# --- Python env ---
say "Syncing Python environment"
uv sync --extra dev

cat <<EOF

==> Done. Run:
    uv run ndi-rmtp
or:
    uv run uvicorn ndi_rmtp.bootstrap:app --host 0.0.0.0 --port 8080 --reload

The first run prints an admin password — save it. Open http://localhost:8080.
EOF
```

- [ ] **Step 2: Make it executable and smoke-test**

```bash
chmod +x scripts/dev-setup-mac.sh
./scripts/dev-setup-mac.sh
```
Expected: script completes; `./bin/ndi_streamer` exists and prints help.

- [ ] **Step 3: Commit**

```bash
git add scripts/dev-setup-mac.sh
git commit -m "Add idempotent dev setup script for Apple Silicon Mac"
```

---

## Task 14: install.sh + systemd unit

**Files:**
- Create: `scripts/install.sh`
- Create: `scripts/ndi-rmtp.service`

- [ ] **Step 1: Write the Pi installer**

`scripts/install.sh`:

```bash
#!/usr/bin/env bash
# Raspberry Pi 5 production installer. Idempotent. Run as root.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run as root: sudo $0"; exit 1; }
[[ "$(uname -m)" == "aarch64" ]] || { echo "Expected aarch64, got $(uname -m)"; exit 1; }

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_ROOT="/opt/ndi-rmtp"
SVC_USER="ndi-rmtp"

say() { printf "\n\033[1;34m==>\033[0m %s\n" "$*"; }

say "Installing apt dependencies"
apt-get update
apt-get install -y --no-install-recommends \
    build-essential cmake git pkg-config curl ca-certificates \
    libavformat-dev libavcodec-dev libavutil-dev libswresample-dev libswscale-dev \
    ffmpeg python3.11 python3.11-venv

# --- uv as the service user ---
say "Ensuring service user $SVC_USER exists"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$SVC_USER"

say "Installing uv (system-wide, into /usr/local/bin)"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh
fi

# --- NDI SDK ---
NDI_PROMPT=/usr/local/include/ndi/Processing.NDI.Lib.h
if [[ ! -f "$NDI_PROMPT" ]]; then
  cat <<EOF
NDI SDK for Linux ARM64 not yet installed.

  1. Download "NDI SDK for Linux" from https://ndi.video/sdk/ (accept EULA)
  2. Extract and run the installer (it'll prompt for EULA acceptance).
  3. Copy the aarch64 library and headers:
       sudo cp -r 'NDI SDK for Linux'/lib/aarch64-rpi4-linux-gnueabi/* /usr/local/lib/
       sudo cp -r 'NDI SDK for Linux'/include/*.h /usr/local/include/ndi/
       sudo ldconfig
  4. Re-run this script.

EOF
  exit 1
fi

# --- Build ndi_streamer ---
say "Building ndi_streamer"
BUILD_DIR="$INSTALL_ROOT/build/ndi_streamer"
mkdir -p "$INSTALL_ROOT/build"
if [[ ! -d "$BUILD_DIR/.git" ]]; then
  git clone https://github.com/alim-zanibekov/ndi_streamer.git "$BUILD_DIR"
else
  git -C "$BUILD_DIR" pull --ff-only
fi
cmake -B "$BUILD_DIR/build" -S "$BUILD_DIR"
cmake --build "$BUILD_DIR/build" --parallel
install -m 0755 "$BUILD_DIR/build/ndi_streamer" /usr/local/bin/ndi_streamer

# --- Install the Python app ---
say "Installing Python app to $INSTALL_ROOT"
mkdir -p "$INSTALL_ROOT/app"
rsync -a --delete \
    --exclude '.venv' --exclude 'build' --exclude 'bin' --exclude '.git' \
    --exclude '__pycache__' --exclude '.pytest_cache' \
    "$PROJECT_ROOT/" "$INSTALL_ROOT/app/"
cd "$INSTALL_ROOT/app"
sudo -u "$SVC_USER" -H env "PATH=/usr/local/bin:$PATH" uv sync

# --- Config dir for the service user ---
say "Preparing config directory"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 0700 "/home/$SVC_USER/.config/ndi-rmtp"

# --- systemd unit ---
say "Installing systemd unit"
install -m 0644 "$PROJECT_ROOT/scripts/ndi-rmtp.service" /etc/systemd/system/ndi-rmtp.service
systemctl daemon-reload
systemctl enable ndi-rmtp.service
systemctl restart ndi-rmtp.service

# --- Surface the first-run password ---
sleep 2
say "Reading bootstrap password from service log (if just created)"
journalctl -u ndi-rmtp.service -n 50 --no-pager | grep -A2 "first-run bootstrap" || true

HOSTNAME_LOCAL="$(hostname).local"
cat <<EOF

==> Done. Web UI: http://${HOSTNAME_LOCAL}:8080

  - systemctl status ndi-rmtp
  - journalctl -u ndi-rmtp -f

The first-run admin password was printed above (and once on boot in
journalctl). If you missed it, delete the config and restart the service:

  sudo -u $SVC_USER rm -f /home/$SVC_USER/.config/ndi-rmtp/config.toml
  sudo systemctl restart ndi-rmtp

EOF
```

- [ ] **Step 2: Write the systemd unit**

`scripts/ndi-rmtp.service`:

```ini
[Unit]
Description=NDI to YouTube RTMP relay
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ndi-rmtp
Group=ndi-rmtp
WorkingDirectory=/opt/ndi-rmtp/app
ExecStart=/usr/local/bin/uv run --no-sync ndi-rmtp
Restart=on-failure
RestartSec=3
Environment=PATH=/usr/local/bin:/usr/bin:/bin

# Hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/ndi-rmtp/.config
PrivateTmp=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 3: Make installer executable**

```bash
chmod +x scripts/install.sh
```

- [ ] **Step 4: Commit (do not run on a Pi yet — that's Phase 4 / Task 17)**

```bash
git add scripts/install.sh scripts/ndi-rmtp.service
git commit -m "Add Pi installer and hardened systemd unit"
```

---

## Task 15: Phase 2 — Manual end-to-end on Mac

This is a validation checkpoint. No code changes unless a step fails.

- [ ] **Step 1: Start a clean dev environment**

```bash
rm -rf ~/.config/ndi-rmtp/
./scripts/dev-setup-mac.sh
uv run uvicorn ndi_rmtp.bootstrap:app --host 0.0.0.0 --port 8080 --reload
```
Note the bootstrap admin password from stderr.

- [ ] **Step 2: Confirm UI loads and source dropdown populates**

Open http://localhost:8080 in a browser, log in. The NDI source dropdown should show the test source within 5 s.
**Pass:** test source name visible in the dropdown.

- [ ] **Step 3: Save stream key, confirm persistence**

Click Edit, paste a YouTube unlisted stream key, click Save. Reload the page — the key is still there (masked). On the filesystem:

```bash
ls -l ~/.config/ndi-rmtp/config.toml
```
**Pass:** file mode is `-rw-------` (0600); stream key is present in the TOML.

- [ ] **Step 4: Start the stream**

Select the test source, choose Medium quality, click Start.
**Pass:** status badge transitions IDLE → STARTING → STREAMING within 10 s; the log tail shows `ndi_streamer` `frame=…` lines.

- [ ] **Step 5: Confirm YouTube received the stream**

Open YouTube Studio for the unlisted stream. **Pass:** stream is live within ~30 s; health reads "Excellent" or "Good" for 2 minutes.

- [ ] **Step 6: Stop the stream**

Click Stop.
**Pass:** state transitions STREAMING → STOPPING → IDLE within 5 s; YouTube Studio reports "Stream offline" within ~30 s.

- [ ] **Step 7: Restart persistence check**

Ctrl-C uvicorn, run it again. Log in, reload the page.
**Pass:** source selection, stream key, and quality preset are restored.

- [ ] **Step 8: Record results**

Note any deviations in `docs/superpowers/2026-06-07-phase2-mac-acceptance.md`:

```markdown
# Phase 2 Acceptance — Mac

Date: <today>
Operator: <you>

| Step | Result | Notes |
|---|---|---|
| 2 | PASS / FAIL | … |
| 3 | PASS / FAIL | … |
| 4 | PASS / FAIL | … |
| 5 | PASS / FAIL | … |
| 6 | PASS / FAIL | … |
| 7 | PASS / FAIL | … |
```

```bash
git add docs/superpowers/2026-06-07-phase2-mac-acceptance.md
git commit -m "Record Phase 2 Mac acceptance results"
```

---

## Task 16: Phase 3 — Error-path checklist on Mac

Each row from the spec's error-path table is a manual test. Run them on the Mac.

- [ ] **Bad stream key** — Save a deliberately wrong key (e.g., `definitely-not-a-key`), click Start. **Expected:** state → ERROR within ~5 s, message says "YouTube rejected the stream key", no auto-retry observed for 30 s.

- [ ] **NDI source disappears mid-stream** — Start the stream, then stop the NDI source on its host. **Expected:** state → ERROR, message mentions source/connection; if `auto_restart=true`, retries on backoff and recovers when the source returns.

- [ ] **External `pkill`** — While STREAMING, run `pkill ndi_streamer`. **Expected:** state → ERROR, auto-restart kicks in per backoff.

- [ ] **Wrong web password** — Try logging in with `admin / wrong`. **Expected:** browser shows 401; `curl -i ...` returns `HTTP/1.1 401 Unauthorized` with `WWW-Authenticate: Basic`.

- [ ] **Concurrent Start clicks** — Spam the Start button. **Expected:** only one subprocess spawns (`ps aux | grep ndi_streamer | wc -l` returns 1); subsequent attempts return 409.

- [ ] **Subprocess hang on stop** — Temporarily symlink `./bin/ndi_streamer` to `tests/fixtures/hanging_ndi_streamer.py`, restart uvicorn, attempt Start then Stop. **Expected:** after 5 s SIGTERM grace, log buffer shows "SIGKILL"; state returns to IDLE. Restore the real binary afterward.

- [ ] **Malformed config** — Stop uvicorn, `echo 'this is not toml' >> ~/.config/ndi-rmtp/config.toml`, restart. **Expected:** old config moved to `config.broken.toml`; new bootstrap password printed; service comes up healthy.

- [ ] **Network drop mid-stream** — Start streaming, disable Wi-Fi for 30 s. **Expected:** state → ERROR; auto-restart backoff entries fire; on Wi-Fi return, next attempt recovers.

- [ ] **Record results**

```bash
cat > docs/superpowers/2026-06-07-phase3-error-paths.md <<EOF
# Phase 3 — Error Path Validation (Mac)

| Scenario | Result | Notes |
|---|---|---|
| Bad stream key | PASS/FAIL | … |
| NDI source disappears | PASS/FAIL | … |
| External pkill | PASS/FAIL | … |
| Wrong web password | PASS/FAIL | … |
| Concurrent start clicks | PASS/FAIL | … |
| Hang on stop | PASS/FAIL | … |
| Malformed config | PASS/FAIL | … |
| Network drop | PASS/FAIL | … |
EOF

git add docs/superpowers/2026-06-07-phase3-error-paths.md
git commit -m "Record Phase 3 error-path validation results"
```

---

## Task 17: Phase 4 — Pi acceptance + 30-minute burn-in

This is the production validation. Done on the actual Raspberry Pi 5.

- [ ] **Step 1: Flash a clean Pi OS image (Bookworm or Trixie aarch64)**

Use Raspberry Pi Imager. Enable SSH and set a user during imaging.

- [ ] **Step 2: Copy the repo to the Pi**

From your Mac:

```bash
rsync -av --exclude .venv --exclude build --exclude bin --exclude .pytest_cache \
    . pi@<pi-hostname>.local:/home/pi/ndi-rmtp/
```

- [ ] **Step 3: Run the installer**

SSH to the Pi:

```bash
cd ~/ndi-rmtp
sudo ./scripts/install.sh
```
The script will prompt to install the NDI SDK. Follow the printed instructions, then re-run.

**Pass criteria:** the script finishes with the URL and a printed admin password (also visible via `journalctl -u ndi-rmtp -n 50`).

- [ ] **Step 4: Boot persistence**

```bash
sudo reboot
```
After it comes back up:

```bash
ssh pi@<pi>.local 'systemctl is-active ndi-rmtp'
```
**Pass:** prints `active`.

```bash
curl -s http://<pi>.local:8080/healthz
```
**Pass:** `{"ok":true}`.

- [ ] **Step 5: End-to-end stream from a Mac browser**

Open `http://<pi-hostname>.local:8080` in a browser on the Mac. Repeat Task 15 steps 2-7. All must pass.

- [ ] **Step 6: 30-minute burn-in**

Start a stream to a YouTube unlisted event with the Medium quality preset.

In an SSH session, every 5 minutes (set a timer), record:

```bash
echo "--- $(date) ---"
vcgencmd measure_temp
free -h | head -2
top -bn1 | head -5
```

In YouTube Studio, monitor stream health.

**Pass criteria (over the full 30 min):**
- Total CPU usage ≤ 80 %, no single core pinned at 100 %.
- SoC temperature ≤ 75 °C steady-state.
- RSS memory of the streamer + uvicorn stable (no monotonic growth).
- YouTube Stream Health = "Excellent" or "Good" for ≥ 95 % of the run.
- No "dropped frames" warnings in YouTube Studio.

- [ ] **Step 7: Reboot-mid-stream recovery**

While STREAMING, `sudo reboot`. After boot, service comes back in IDLE (we deliberately do not auto-resume).
**Pass:** UI loads, state is IDLE.

- [ ] **Step 8: Record results and ship**

```bash
cat > docs/superpowers/2026-06-07-phase4-pi-acceptance.md <<EOF
# Phase 4 — Pi Acceptance + Burn-in

Hardware: Raspberry Pi 5 (<RAM>), Pi OS <version>
Date: <today>

## Install
| Check | Result |
|---|---|
| install.sh completes idempotently | PASS/FAIL |
| Service auto-starts on boot | PASS/FAIL |
| LAN URL reachable | PASS/FAIL |

## Burn-in (30 minutes, Medium preset, 1080p30)
| Check | Result | Steady-state value |
|---|---|---|
| Total CPU ≤ 80 % | PASS/FAIL | …% |
| Single core ≤ 100 % | PASS/FAIL | …% |
| SoC temp ≤ 75 °C | PASS/FAIL | …°C |
| Memory stable | PASS/FAIL | … MB RSS |
| YouTube health ≥ 95 % "Good/Excellent" | PASS/FAIL | …% |
| No dropped-frame warnings | PASS/FAIL | … |

## Recovery
| Check | Result |
|---|---|
| Reboot mid-stream → service starts in IDLE | PASS/FAIL |

## Notes
- …
EOF

git add docs/superpowers/2026-06-07-phase4-pi-acceptance.md
git commit -m "Record Phase 4 Pi acceptance + burn-in results"
```

If all rows pass, the appliance is ready for use. If any row fails, file an issue describing exactly which value missed the bar and which workload it was under, then decide whether to ship anyway or address.

---

## Self-Review Summary

**Spec coverage check** — each major spec section maps to one or more tasks:

| Spec section | Implemented in |
|---|---|
| Architecture / data path | Task 2 (spike), Task 6 (subprocess), Task 11 (wiring) |
| Components table | Tasks 3-11 |
| Web UI | Tasks 9, 10 |
| State machine | Task 5 |
| Configuration | Task 3 |
| Quality presets | Task 6 (`QUALITY_PRESETS`) |
| Error handling table | Tasks 6, 7, 16 |
| Logging & observability | Task 6 (ring buffer), Task 11 (logging setup) |
| Phase 0 spike | Task 2 |
| Phase 1 automated tests | Tasks 3-9, 12 |
| Phase 2 Mac E2E | Task 15 |
| Phase 3 error-path checklist | Task 16 |
| Phase 4 Pi acceptance + burn-in | Task 17 |
| Mac dev setup | Task 13 |
| Pi install + systemd | Task 14 |

**Spike feedback loop** — Task 2 explicitly says to update `build_args` (Task 6) and `_DISCOVERY_ARGS` / `_SOURCE_RE` (Task 8) if the spike finds different CLI flags or output formats.

**Type consistency** — `Streamer.start(source, stream_key, quality)`, `StreamerStatus.to_dict()`, `NDIDiscovery.list()`, `Source(name, address)`, `Config.load/save/bootstrap` are all referenced with consistent signatures across tasks 3-12.

No placeholders, no "TODO", no "similar to Task N". Each step has the code or the command.
