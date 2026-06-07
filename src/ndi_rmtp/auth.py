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
