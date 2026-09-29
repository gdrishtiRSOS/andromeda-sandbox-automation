"""Andromeda access tokens, minted rather than pasted.

Andromeda signs in through Google, so there is no password endpoint. But the
OIDC exchange that completes that sign-in returns a **refresh token** as well
as an access token:

    POST https://api-sandbox.rapidsos.com/v1/scorpius/openid-connect/google
         {"code": ..., "state": ...}
      -> {"token": <access, ~4h>, "refresh_token": <refresh, ~24h>}

and that refresh token can be traded for new access tokens without a browser:

    POST https://api-sandbox.rapidsos.com/v1/scorpius/user/api-token-auth/refresh
         {"refresh_token": ...}
      -> {"token": <access, ~4h>}

Verified against a real session: the minted token is accepted by Andromeda and
carries the same identity and permissions as the pasted one.

So the shape is: sign in with Google once, keep the refresh token, and the
tooling mints its own access tokens until the refresh token expires -- about a
day later. That turns "paste a token every few hours" into "sign in each
morning".

Design notes
------------
* **The refresh token does not rotate.** The refresh response contains only
  `token`, no new `refresh_token`, and the original stays valid. So there is
  nothing to write back after each refresh, and a missed refresh costs
  nothing.

* **The refresh token is the credential now.** It lives longer than an access
  token and buys the same access, so the cache file holds it and must not be
  committed, shared or logged. `TokenCache` writes it 0600 where the platform
  allows.

* Nothing here performs the Google sign-in. That needs a browser, and the
  authorization code it produces is single-use.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from logic.tokens import read_claims

log = logging.getLogger(__name__)

__all__ = [
    "AuthError",
    "DEFAULT_CACHE_PATH",
    "REFRESH_PATH",
    "SCORPIUS_BASE",
    "Session",
    "TokenCache",
    "refresh_access_token",
    "refresh_token_from_har",
]

SCORPIUS_BASE = "https://api-sandbox.rapidsos.com"
REFRESH_PATH = "/v1/scorpius/user/api-token-auth/refresh"
OIDC_PATH = "/v1/scorpius/openid-connect/google"

#: Where the refresh token is kept. Gitignore this.
DEFAULT_CACHE_PATH = Path(".andromeda-session.json")

#: Mint a new access token when this little of its life is left.
REFRESH_WHEN_UNDER = dt.timedelta(minutes=10)


class AuthError(Exception):
    """Base for this module."""


@dataclass(frozen=True)
class Session:
    token: str
    refresh_token: str | None = None
    username: str | None = None
    subject: str | None = None
    expires_at: dt.datetime | None = None

    @classmethod
    def from_api(cls, data: Mapping[str, Any], *, refresh_token: str | None = None) -> "Session":
        token = data.get("token") or data.get("access_token")
        if not token:
            raise AuthError(f"no token in the response; keys were {sorted(data)}")
        claims = read_claims(token)
        expires = None
        if isinstance(claims.get("exp"), (int, float)):
            expires = dt.datetime.fromtimestamp(claims["exp"], tz=dt.timezone.utc)
        return cls(
            token=token,
            # the refresh response returns no new refresh token; keep the one we used
            refresh_token=data.get("refresh_token") or refresh_token,
            username=claims.get("username") or claims.get("email"),
            subject=str(claims["sub"]) if claims.get("sub") is not None else None,
            expires_at=expires,
        )

    def seconds_left(self) -> float | None:
        if self.expires_at is None:
            return None
        return (self.expires_at - dt.datetime.now(dt.timezone.utc)).total_seconds()

    def expiring(self, within: dt.timedelta = REFRESH_WHEN_UNDER) -> bool:
        left = self.seconds_left()
        return left is not None and left < within.total_seconds()

    def __repr__(self) -> str:              # never print either token
        left = self.seconds_left()
        when = f", {left / 60:.0f} min left" if left is not None else ""
        return f"Session({self.username or 'unknown'}{when})"


@runtime_checkable
class AuthClient(Protocol):
    """An unauthenticated client pointed at SCORPIUS_BASE."""

    def post(self, path: str, json: Mapping[str, Any]) -> Any: ...


def refresh_access_token(client: AuthClient, refresh_token: str) -> Session:
    """Trade a refresh token for a fresh Andromeda access token."""
    data = client.post(REFRESH_PATH, {"refresh_token": refresh_token})
    if not isinstance(data, Mapping):
        raise AuthError(f"refresh returned {type(data).__name__}, expected an object")
    session = Session.from_api(data, refresh_token=refresh_token)
    left = session.seconds_left()
    log.info(
        "minted an access token for %s%s",
        session.username or "the session",
        f", valid for {left / 60:.0f} min" if left else "",
    )
    return session


def refresh_token_from_har(path: str | Path) -> str:
    """Pull the refresh token out of a captured Google sign-in.

    Convenient because the sign-in HAR is how you get one in the first place:
    it is in the response to POST .../openid-connect/google.
    """
    har = json.loads(Path(path).read_text(encoding="utf-8"))
    for entry in har.get("log", {}).get("entries", []):
        request = entry.get("request", {})
        if request.get("method") != "POST" or OIDC_PATH not in request.get("url", ""):
            continue
        text = entry.get("response", {}).get("content", {}).get("text")
        if not text:
            raise AuthError(
                "found the sign-in call but the HAR has no response body. "
                "Re-export with 'Save all as HAR with content'."
            )
        token = json.loads(text).get("refresh_token")
        if not token:
            raise AuthError("the sign-in response carried no refresh_token")
        return token
    raise AuthError(
        f"no Google sign-in call found in {path}. Capture one with DevTools "
        "recording, in a clean window, and sign in."
    )


class TokenCache:
    """The refresh token on disk, and an access token minted from it on demand.

    Holding the refresh token means one Google sign-in lasts until it expires,
    roughly a day, rather than a paste every few hours.
    """

    def __init__(self, path: str | Path = DEFAULT_CACHE_PATH):
        self.path = Path(path)

    # ------------------------------------------------------------ storage

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            log.warning("%s is not readable JSON; ignoring it", self.path)
            return {}

    def _write(self, data: Mapping[str, Any]) -> None:
        self.path.write_text(json.dumps(dict(data), indent=2), encoding="utf-8")
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)   # 0600
        except OSError:
            pass                                               # Windows may refuse

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    # ------------------------------------------------------------- tokens

    @property
    def refresh_token(self) -> str | None:
        return self._read().get("refresh_token") or None

    def save_refresh_token(self, refresh_token: str) -> Session:
        """Store a refresh token, discarding any cached access token."""
        claims = read_claims(refresh_token)
        if claims.get("type") not in (None, "refresh"):
            raise AuthError(
                f"that is a {claims['type']!r} token, not a refresh token. "
                "The refresh token comes from the sign-in response, alongside "
                "the access token."
            )
        self._write({"refresh_token": refresh_token})
        expires = None
        if isinstance(claims.get("exp"), (int, float)):
            expires = dt.datetime.fromtimestamp(claims["exp"], tz=dt.timezone.utc)
        session = Session(token="", refresh_token=refresh_token,
                          username=claims.get("username"), expires_at=expires)
        log.info("stored a refresh token in %s", self.path)
        return session

    def cached_session(self) -> Session | None:
        data = self._read()
        if not data.get("token"):
            return None
        return Session.from_api(data, refresh_token=data.get("refresh_token"))

    def access_token(self, client: AuthClient, *, force: bool = False) -> Session:
        """A usable access token, minted if the cached one is old or absent.

        Raises
        ------
        AuthError
            No refresh token is stored, or it has expired and a new sign-in
            is needed.
        """
        data = self._read()
        refresh_token = data.get("refresh_token")
        if not refresh_token:
            raise AuthError(
                f"no refresh token stored in {self.path}. Sign in to Andromeda "
                "with DevTools recording, save the HAR, and store the refresh "
                "token from the sign-in response."
            )

        if not force:
            cached = self.cached_session()
            if cached and not cached.expiring():
                return cached

        session = refresh_access_token(client, refresh_token)
        self._write({"refresh_token": session.refresh_token or refresh_token,
                     "token": session.token})
        return session
