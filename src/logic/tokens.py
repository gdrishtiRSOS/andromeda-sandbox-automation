"""What a pasted bearer token says about itself.

Both tokens this tooling uses are JWTs copied out of DevTools. A JWT's payload
is base64, not encrypted, so its expiry and owner can be read without the
signing key -- which makes it possible to say "valid for 2h 14m, belongs to
you@rapidsos.com" before sending it anywhere.

The checks are the ones `smoke_test.inspect_token` prints; this returns them
instead, for callers that are not a terminal. Nothing here makes a request,
and nothing here logs the token.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
from dataclasses import dataclass
from typing import Any

__all__ = ["TokenInfo", "bearer", "decode_claims", "decode_token", "read_claims"]

_JWT_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=."
)


@dataclass(frozen=True)
class TokenInfo:
    ok: bool                         # well-formed and decodable (not: unexpired)
    problem: str | None = None       # why it is not, in words
    length: int = 0
    user: str | None = None
    subject: str | None = None
    expires_at: dt.datetime | None = None   # UTC

    def seconds_left(self, now: dt.datetime | None = None) -> float | None:
        if self.expires_at is None:
            return None
        now = now or dt.datetime.now(dt.timezone.utc)
        return (self.expires_at - now).total_seconds()

    def expired(self, now: dt.datetime | None = None) -> bool:
        left = self.seconds_left(now)
        return left is not None and left <= 0


def _strip(token: str) -> str:
    raw = token.strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    return raw


def bearer(token: str) -> str:
    """The Authorization header value, whether or not "Bearer " was pasted."""
    return f"Bearer {_strip(token)}"


def decode_claims(token: str) -> dict[str, Any]:
    """A JWT's payload. Raises ValueError, with a reason in words, when the
    payload cannot be decoded or is not an object. Checks nothing else; for a
    token a person pasted, `decode_token` says more about what is wrong."""
    try:
        segment = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except Exception:
        raise ValueError("the payload could not be decoded; the token is corrupt") from None
    if not isinstance(claims, dict):
        raise ValueError("the payload is not an object")
    return claims


def read_claims(token: str | None) -> dict[str, Any]:
    """A JWT's payload, or {} if it cannot be read. Never raises."""
    try:
        return decode_claims(token)
    except ValueError:
        return {}


def decode_token(token: str | None) -> TokenInfo:
    """Read a token's claims. Never raises; a bad token gives ok=False."""
    if not token or not token.strip():
        return TokenInfo(ok=False, problem="not set")

    raw = _strip(token)

    bad = [c for c in ('"', "^", " ", "'") if c in raw]
    if bad:
        return TokenInfo(ok=False, length=len(raw),
                         problem=f"contains {''.join(bad)!r} -- shell escaping or "
                                 f"quotes were copied along with it")

    illegal = sorted({c for c in raw if c not in _JWT_ALPHABET})
    if illegal:
        return TokenInfo(ok=False, length=len(raw),
                         problem="contains characters a token cannot hold; it was "
                                 "mangled or truncated in the copy -- copy it again")

    parts = raw.split(".")
    if len(parts) != 3:
        return TokenInfo(ok=False, length=len(raw),
                         problem=f"has {len(parts)} part(s), not 3 -- probably cut "
                                 f"short; copy the whole value")

    try:
        claims = decode_claims(raw)
    except ValueError as exc:
        return TokenInfo(ok=False, length=len(raw), problem=str(exc))

    user = next((str(claims[k]) for k in ("username", "email", "preferred_username")
                 if claims.get(k)), None)
    expires = None
    if isinstance(claims.get("exp"), (int, float)):
        expires = dt.datetime.fromtimestamp(claims["exp"], tz=dt.timezone.utc)

    return TokenInfo(
        ok=True,
        length=len(raw),
        user=user,
        subject=str(claims["sub"]) if claims.get("sub") is not None else None,
        expires_at=expires,
    )
