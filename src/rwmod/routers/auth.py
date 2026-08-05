"""Auth router — login, token refresh, and protected status check."""

from __future__ import annotations

import hmac
import time
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Request

from rwmod.auth import create_token, get_current_user

router = APIRouter(prefix="/api", tags=["auth"])

# ── Login rate limiting ──────────────────────────────────────────────
# Brute-force guard for the shared-secret login. Per-client failure counts
# within a sliding window; excessive failures trigger a temporary ban.
_FAIL_WINDOW_SECS = 300  # 5 分钟窗口
_MAX_FAILS = 10  # 窗口内允许的最大失败次数
_fail_log: dict[str, list[float]] = defaultdict(list)  # client ip -> [fail timestamps]


def _login_banned(client_ip: str) -> bool:
    now = time.monotonic()
    fails = [t for t in _fail_log.get(client_ip, []) if now - t < _FAIL_WINDOW_SECS]
    if fails:
        _fail_log[client_ip] = fails
    return len(fails) >= _MAX_FAILS


def _record_fail(client_ip: str) -> None:
    _fail_log[client_ip].append(time.monotonic())
    # Keep the table bounded — drop the whole log if it grows huge.
    if len(_fail_log) > 10_000:
        _fail_log.clear()


@router.post("/auth/login")
def login(payload: dict, request: Request):
    """Login with shared secret to obtain a JWT token.

    Body: {"password": "your-secret"}
    Returns: {"token": "jwt-string", "expires_in": 604800}
    """
    from rwmod.auth import get_secret

    client_ip = request.client.host if request.client else "unknown"
    if _login_banned(client_ip):
        raise HTTPException(429, "尝试次数过多，请 5 分钟后再试")

    password = payload.get("password", "")
    secret = get_secret()
    # Timing-safe comparison + rate limiting against offline brute force.
    if not password or not hmac.compare_digest(password.encode(), secret.encode()):
        _record_fail(client_ip)
        raise HTTPException(403, "密码错误")

    _fail_log.pop(client_ip, None)
    token = create_token()
    return {"token": token, "expires_in": 7 * 24 * 3600}


@router.get("/auth/verify")
def verify(user: str = Depends(get_current_user)):
    """Verify current token is valid. Returns username if valid."""
    return {"user": user, "valid": True}
