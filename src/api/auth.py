"""Shared-token gate for the hosted demo.

The demo runs on a personal API key, so an ungated public URL is unbounded cost
exposure for however long the review window lasts. A shared bearer token given
to the evaluator costs them one header and caps that exposure.
"""

from __future__ import annotations

import secrets

from fastapi import Depends, Header, HTTPException, status

from src.config import Settings, get_settings

_UNAUTHORISED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Missing or invalid access token. Send: Authorization: Bearer <token>",
    headers={"WWW-Authenticate": "Bearer"},
)


def require_token(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    if authorization is None:
        raise _UNAUTHORISED

    scheme, _, presented = authorization.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        raise _UNAUTHORISED

    # compare_digest, not ==. A plain equality check on a secret leaks its
    # length and matching prefix through timing. Cheap to get right; this is a
    # bank, and it is the kind of thing worth being correct about by reflex.
    if not secrets.compare_digest(presented, settings.demo_access_token):
        raise _UNAUTHORISED
