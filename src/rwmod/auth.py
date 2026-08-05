"""JWT authentication for rwmod.

Simple token-based auth: login with a shared secret to get a JWT bearer token.
All protected endpoints verify the token via FastAPI dependency.

Config:
    Set RWMOD_SECRET env var for the signing key.
    If unset, a random key is generated once, persisted to ~/.rwmod.secret and
    printed to the startup log — tokens survive restarts, and there is no
    public default secret that everyone's installation shares (the old
    "rwmod-dev-secret" default is refused at runtime).

Usage in routers:
    from rwmod.auth import get_current_user
    @router.get("/protected")
    def protected_endpoint(user: str = Depends(get_current_user)):
        ...
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from pathlib import Path

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

__all__ = [
    "create_token",
    "verify_token",
    "get_current_user",
    "get_secret",
    "is_secure_secret",
    "is_env_secret",
    "is_local_request",
]

_log = logging.getLogger(__name__)

_security = HTTPBearer(auto_error=False)
_TOKEN_TTL = 7 * 24 * 3600  # 7 days

# Hard-coded default → refuse at runtime. The only acceptable values come from
# RWMOD_SECRET or an auto-generated random key (never a known constant).
_FORBIDDEN_SECRETS = frozenset({"rwmod-dev-secret", "secret", "password", ""})

# Persisted auto-generated key (survives restarts; printed to the log once).
_SECRET_FILE = Path.home() / ".rwmod.secret"

# Fallback only when the secret file cannot be written — in-memory random key.
_auto_secret: str | None = None


def is_secure_secret(secret: str | None) -> bool:
    """True if the secret is set from env and not a known-forbidden constant."""
    return bool(secret and secret not in _FORBIDDEN_SECRETS and len(secret) >= 16)


def is_env_secret() -> bool:
    """True if the operator explicitly configured a strong RWMOD_SECRET.

    When a strong env secret is set, ALL requests (including localhost) are
    authenticated. Without it, localhost requests are trusted (this is a
    local desktop tool) and only LAN clients need the persisted key.
    """
    return is_secure_secret(os.environ.get("RWMOD_SECRET"))


def is_local_request(request: Request) -> bool:
    """True if the request comes from this machine (loopback)."""
    host = (request.client.host if request.client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost")


def get_secret() -> str:
    """Get the signing secret: RWMOD_SECRET env var, or a persisted random key.

    Security: if RWMOD_SECRET is absent we generate a cryptographically random
    key ONCE and persist it to ~/.rwmod.secret, so tokens survive restarts and
    the operator can find the key in the startup log to log in. We NEVER fall
    back to a public hard-coded secret that everyone's installation shares.
    """
    env = os.environ.get("RWMOD_SECRET")
    if is_secure_secret(env):
        return env  # type: ignore[return-value]
    if env is not None:
        # Env var present but weak (e.g. old default): refuse to use it.
        _log.warning("RWMOD_SECRET 过弱（被禁止的默认值），改用自动生成的持久密钥")
    return _load_or_create_secret()


def _load_or_create_secret() -> str:
    """Load the persisted secret from ~/.rwmod.secret, creating it on first use."""
    global _auto_secret
    try:
        if _SECRET_FILE.exists():
            s = _SECRET_FILE.read_text(encoding="utf-8").strip()
            if is_secure_secret(s):
                return s
        s = secrets.token_urlsafe(48)
        _SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SECRET_FILE.write_text(s + "\n", encoding="utf-8")
        _log.warning(
            "未设置 RWMOD_SECRET：已生成访问密钥并写入 %s（登录时使用该密钥）",
            _SECRET_FILE,
        )
        return s
    except OSError:
        # Secret file not writable (e.g. read-only home dir) — fall back to an
        # in-memory key. Old tokens die on restart; still better than a shared
        # public default.
        if _auto_secret is None:
            _auto_secret = secrets.token_urlsafe(48)
        return _auto_secret


def create_token(username: str = "admin") -> str:
    """Create a signed JWT for the given username."""
    now = int(time.time())
    payload = {"sub": username, "iat": now, "exp": now + _TOKEN_TTL}
    token = jwt.encode(payload, get_secret(), algorithm="HS256")
    return str(token)


def verify_token(token: str) -> str | None:
    """Verify a token and return the username if valid. Returns None if invalid."""
    try:
        payload = jwt.decode(token, get_secret(), algorithms=["HS256"])
        sub = payload.get("sub")
        return sub if isinstance(sub, str) else None
    except jwt.PyJWTError:
        return None


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),  # noqa: B008
) -> str:
    """FastAPI dependency: extract and verify JWT from Authorization header.

    Localhost requests are trusted WITHOUT a token when no strong RWMOD_SECRET
    is configured — this is a local desktop tool (double-click to launch), and
    a process on this machine can already read/write the Mods directory
    directly. Requiring a token here would just force the operator to hunt for
    a hidden key for no real security gain.

    With a strong RWMOD_SECRET set (explicit opt-in to strict mode), or for
    LAN clients, a valid JWT is always required.
    """
    if is_local_request(request) and not is_env_secret():
        return "admin"

    if credentials is None:
        raise HTTPException(401, "需要认证", headers={"WWW-Authenticate": "Bearer"})

    user = verify_token(credentials.credentials)
    if user is None:
        raise HTTPException(401, "Token 无效或已过期", headers={"WWW-Authenticate": "Bearer"})
    return user
