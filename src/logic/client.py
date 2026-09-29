"""The HTTP client the modules talk to, for the CLI and the web app alike.

Raises on every non-2xx. Several modules depend on that: they catch the
exception and read its text to decide whether to retry, fall back or skip
(`create_psap` looks for a 5xx, `create_revision` for 409,
`update_account_info` for "Cannot update"). So the message keeps the shape

    <status> on <METHOD> <path>: <body>

and the same facts are on the exception as attributes.

A 401 or 403 raises `AuthError` -- almost always an expired token. A caller
that would rather stop outright passes `on_auth_error`, which is called with
the error before it is raised; the CLI uses it to exit with advice, which
also keeps a dead token from being caught by a module's retry logic.

The token lives only in the session's headers. It is never part of a message,
a repr, or a log record.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

import requests

from logic.tokens import bearer

__all__ = ["ApiError", "AuthError", "HttpClient"]


class ApiError(RuntimeError):
    def __init__(self, status: int, text: str, method: str, path: str):
        self.status = status
        self.text = text
        self.method = method
        self.path = path
        super().__init__(f"{status} on {method} {path}: {text[:300]}")


class AuthError(ApiError):
    """The server refused the credential -- almost always because it expired."""

    def __init__(self, status: int, text: str, method: str, path: str, *,
                 token_sent: Optional[bool] = None):
        super().__init__(status, text, method, path)
        self.token_sent = token_sent


class HttpClient:
    def __init__(self, base_url: str, *, token: Optional[str] = None,
                 org: Optional[str] = None, cookie: Optional[str] = None,
                 headers: Optional[Mapping[str, str]] = None, timeout: float = 60,
                 on_auth_error: Optional[Callable[[AuthError], None]] = None):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self._on_auth_error = on_auth_error
        self._session = requests.Session()
        self._session.headers["Accept"] = "application/json"
        if org:
            self._session.headers["x-rapidsos-org"] = org
        if token:
            self._session.headers["Authorization"] = bearer(token)
        if cookie:
            self._session.headers["Cookie"] = cookie
        for name, value in (headers or {}).items():
            self._session.headers[name] = value

    def __repr__(self) -> str:
        authed = "Authorization" in self._session.headers
        return f"HttpClient({self.base!r}, {'with' if authed else 'no'} token)"

    def _call(self, method: str, path: str, **kw: Any) -> Any:
        r = self._session.request(method, f"{self.base}{path}", timeout=self.timeout, **kw)
        if r.status_code in (401, 403):
            exc = AuthError(r.status_code, r.text, method, path,
                            token_sent="Authorization" in self._session.headers)
            if self._on_auth_error:
                self._on_auth_error(exc)
            raise exc
        if not r.ok:
            raise ApiError(r.status_code, r.text, method, path)
        return r.json() if r.content else None

    def get(self, path: str) -> Any:
        return self._call("GET", path)

    def post(self, path: str, json: Any = None) -> Any:
        return self._call("POST", path, json=json)

    def patch(self, path: str, json: Any) -> Any:
        return self._call("PATCH", path, json=json)

    def put(self, path: str, json: Any) -> Any:
        return self._call("PUT", path, json=json)
