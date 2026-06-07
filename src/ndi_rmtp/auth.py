"""HTTP Basic Auth dependency factory."""
from __future__ import annotations

import secrets
from typing import Callable, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .config import Config

# auto_error=False so missing-header reaches our _check, which honors
# config.auth.enabled. With auto_error=True, FastAPI would always 401 on
# missing creds before we got a chance to bypass when auth is disabled.
security = HTTPBasic(auto_error=False)


def make_auth_dependency(get_config: Callable[[], Config]):
    """Returns a FastAPI dependency that enforces HTTP Basic Auth against config.

    When `config.auth.enabled` is False, the dependency is a no-op — every
    request is allowed through without checking credentials. This is intended
    for trusted single-user LAN setups where the operator has decided the
    appliance is the only thing on its network slot.
    """

    def _check(credentials: Optional[HTTPBasicCredentials] = Depends(security)) -> None:
        config = get_config()
        if not config.auth.enabled:
            return
        if credentials is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Basic"},
            )
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
