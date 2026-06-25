"""Bearer-token auth. Every play request carries ``Authorization: Bearer <token>``;
the token is the single credential a user is handed.

Tokens are opaque random strings, issued out of band by ``scripts/issue-token.py``
and stored only as a hash (see :mod:`arena.store`). There is no login flow and no
session: the token *is* the identity.

Uses FastAPI's ``HTTPBearer`` security scheme so the OpenAPI document declares the
bearer requirement: Swagger shows an "Authorize" button and marks which endpoints
need a token.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from arena.store import Store, _Identity

# Set once at startup by server.py so the dependency can reach the store.
_STORE: Optional[Store] = None

# auto_error=False so a missing header yields our own 401 (not FastAPI's 403).
_bearer = HTTPBearer(auto_error=False, description="Your access token.")


def set_store(store: Store) -> None:
    global _STORE
    _STORE = store


def _resolve(token: Optional[str]) -> _Identity:
    if not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    assert _STORE is not None, "store not configured"
    identity = _STORE.resolve_token(token)
    if identity is None:
        raise HTTPException(status_code=401, detail="invalid or revoked token")
    return identity


def require_identity(creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> _Identity:
    """FastAPI dependency: resolve the bearer token to an identity, or raise 401."""
    return _resolve(creds.credentials if creds else None)
