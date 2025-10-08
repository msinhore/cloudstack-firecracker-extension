"""Authentication helpers for the Firecracker agent."""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

logger = logging.getLogger("fc-agent")

try:  # pragma: no cover - optional dependency
    import pamela
except ImportError:  # pragma: no cover - fallback to python-pam if available
    pamela = None

try:  # pragma: no cover - optional dependency
    import pam
except ImportError:  # pragma: no cover - fallback only
    pam = None


class PamError(RuntimeError):
    """Raised when PAM authentication fails or backend is not available."""


class PamAuthenticator:
    """Wrapper around pamela/python-pam to authenticate users."""

    def __init__(self, service: str) -> None:
        self.service = service
        if pamela is None and pam is None:
            raise PamError("No PAM backend available. Install pamela or python-pam.")

    def authenticate(self, username: str, password: str) -> bool:
        if not username:
            raise PamError("Username is required for PAM authentication")
        if pamela is not None:
            try:
                pamela.authenticate(username, password, service=self.service)  # type: ignore[arg-type]
                return True
            except pamela.PAMError as exc:  # type: ignore[attr-defined]
                raise PamError(str(exc)) from exc
        if pam is not None:
            try:
                pam.pam().authenticate(username, password, service=self.service)
                return True
            except pam.pam.error as exc:  # type: ignore[attr-defined]
                raise PamError(str(exc)) from exc
        raise PamError("No PAM backend available")


def _enabled_value(value: Any) -> bool:
    """Normalize truthy/falsey configuration values."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def build_auth_dependency(config: Optional[dict]) -> Optional[Callable[[HTTPBasicCredentials], str]]:
    """Construct a FastAPI dependency that enforces HTTP Basic + PAM."""
    if not config or not isinstance(config, dict):
        logger.info("Auth section not configured; running without authentication")
        return None
    if not _enabled_value(config.get("enabled")):
        logger.info("Auth section disabled explicitly; running without authentication")
        return None
    service = config.get("service", "firecracker-agent")
    try:
        authenticator = PamAuthenticator(service=service)
    except PamError as exc:
        logger.error("Authentication disabled: %s", exc)
        raise
    basic = HTTPBasic(auto_error=False)

    def dependency(credentials: Optional[HTTPBasicCredentials] = Depends(basic)) -> str:
        if credentials is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing credentials",
                headers={"WWW-Authenticate": "Basic"},
            )
        try:
            authenticator.authenticate(credentials.username, credentials.password)
        except PamError as exc:
            logger.warning("Authentication failed for user %s: %s", credentials.username, exc)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
                headers={"WWW-Authenticate": "Basic"},
            ) from exc
        return credentials.username

    logger.info("Authentication enabled using PAM service '%s'", service)
    return dependency
