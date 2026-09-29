"""The Andromeda credential, held by the app instead of pasted into the page.

Where a token comes from, in order:

1. `ANDROMEDA_TOKEN` in the environment -- an override for anyone who prefers
   pasting. It cannot be renewed: when it runs out, set a fresh one and
   restart the app.
2. The stored session (`.andromeda-session.json`): the refresh token from a
   sign-in, from which `TokenCache` mints access tokens as they are needed.

A session is started by `browser_auth.sign_in` -- a real browser window the
person signs in through -- or, when Playwright is not installed, by uploading
a saved sign-in capture (a HAR).

The session file and the browser profile are credentials. Nothing here sends
either to the page, puts them in an event or a log record, or offers a way to
copy them anywhere. What the page learns is who is signed in and for how long.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from logic import auth
from logic.browser_auth import INSTALL_HINT, BrowserAuthError, PlaywrightMissing
from logic.tokens import decode_token
from logic.client import ApiError
from webapp.runs import Run, scrub

log = logging.getLogger(__name__)

__all__ = ["AndromedaAuth", "HarRejected", "NoSession", "SIGN_IN_QUIET",
           "SignInBusy", "SignInRefused", "playwright_installed"]

#: Statuses of a sign-in job in which nothing is running.
SIGN_IN_QUIET = frozenset({"signed_in", "sign_in_failed"})

STOPPED = ("The sign-in stopped before it finished ({name}). If you closed the "
           "browser window, press Sign in again. If this is the first sign-in on "
           "this computer, the browser Playwright drives may not be installed yet:\n"
           "    python -m playwright install chromium")


def playwright_installed() -> bool:
    """Whether sign-in can open a browser. Checked without importing it."""
    return importlib.util.find_spec("playwright") is not None


class NoSession(Exception):
    """No usable Andromeda token. The message says what to do about it."""

    def __init__(self, message: str, state: str):
        super().__init__(message)
        self.state = state               # "signed_out" | "expired" | "failed"


class SignInRefused(Exception):
    """Sign-in is not possible right now; the message says why."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


class SignInBusy(Exception):
    def __init__(self, job_id: str):
        super().__init__("a sign-in is already running")
        self.job_id = job_id


class HarRejected(Exception):
    """The uploaded capture gave no usable session; the message says why."""


class AndromedaAuth:
    """Where the Andromeda token comes from, and the sign-in that starts one.

    `scorpius` builds an unauthenticated client for `auth.SCORPIUS_BASE`;
    `sign_in` is `browser_auth.sign_in` or a stand-in for it.
    """

    def __init__(self, *, cache: auth.TokenCache, scorpius: Callable[[], Any],
                 sign_in: Callable[..., Any], profile_dir: Path,
                 env_token: Optional[str] = None,
                 can_open_browser: Callable[[], bool] = playwright_installed):
        self.cache = cache
        self.scorpius = scorpius
        self.profile_dir = profile_dir
        self.env_token = (env_token or "").strip() or None
        self.can_open_browser = can_open_browser
        self._sign_in = sign_in
        self._lock = threading.Lock()
        self._jobs: Dict[str, Run] = {}
        self._running: Optional[str] = None
        # its own worker: a first sign-in can wait minutes for a person, and
        # must not hold up the workers that runs use
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sign-in")

    # ------------------------------------------------------------ tokens

    @property
    def source(self) -> str:
        if self.env_token:
            return "env"
        return "session" if self.cache.refresh_token else "none"

    def token(self) -> str:
        """A usable access token. Raises NoSession, saying what to do."""
        if self.env_token:
            info = decode_token(self.env_token)
            if not info.ok:
                raise NoSession(f"The token in ANDROMEDA_TOKEN {info.problem}.", "failed")
            if info.expired():
                raise NoSession("The token in ANDROMEDA_TOKEN has expired. Set a fresh "
                                "one and restart the app, or unset it to use Sign in.",
                                "expired")
            return self.env_token

        if not self.cache.refresh_token:
            raise NoSession("Not signed in to Andromeda. Press Sign in to Andromeda at "
                            "the top of the page.", "signed_out")
        try:
            return self.cache.access_token(self.scorpius()).token
        except ApiError as exc:
            if 400 <= exc.status < 500:
                raise NoSession("Your Andromeda sign-in has run out (it lasts about a "
                                "day). Press Sign in again at the top of the page.",
                                "expired") from None
            raise NoSession(f"The sign-in service answered {exc.status}. Try again in "
                            f"a minute.", "failed") from None
        except auth.AuthError:
            raise NoSession("The stored sign-in could not be used. Press Sign in again "
                            "at the top of the page.", "failed") from None
        except Exception as exc:
            raise NoSession(f"Could not reach the sign-in service "
                            f"({type(exc).__name__}).", "failed") from None

    def token_or_none(self) -> Optional[str]:
        try:
            return self.token()
        except NoSession:
            return None

    def status(self) -> Dict[str, Any]:
        """Who is signed in and for how long. Never a token, never a path."""
        source = self.source
        out: Dict[str, Any] = {
            "source": source, "state": "signed_out", "user": None,
            "expires_at": None, "seconds_left": None, "problem": None,
            "sign_in": {"available": source != "env" and self.can_open_browser(),
                        "running": self._running, "install": INSTALL_HINT},
        }
        if source == "none":
            return out

        # how long the sign-in lasts: the env token itself, or the refresh
        # token -- the access tokens minted from it renew themselves
        info = decode_token(self.env_token if source == "env" else self.cache.refresh_token)
        out["user"] = info.user
        if info.expires_at:
            out["expires_at"] = info.expires_at.isoformat()
            out["seconds_left"] = info.seconds_left()
        try:
            token = self.token()
        except NoSession as exc:
            out["state"], out["problem"] = exc.state, str(exc)
            return out
        out["user"] = out["user"] or decode_token(token).user
        out["state"] = "signed_in"
        return out

    def sign_out(self) -> None:
        """Forget the stored session. The browser profile stays, so the next
        sign-in completes on its own."""
        self.cache.clear()

    # ----------------------------------------------------------- sign-in

    def job(self, job_id: str) -> Optional[Run]:
        return self._jobs.get(job_id)

    def start_sign_in(self) -> Run:
        """Start a browser sign-in in the background; progress is on the job."""
        if self.env_token:
            raise SignInRefused("ANDROMEDA_TOKEN is set, so the app uses that token. "
                                "Restart the app without it to sign in.", "env")
        if not self.can_open_browser():
            raise SignInRefused(INSTALL_HINT, "playwright_missing")
        with self._lock:
            if self._running:
                raise SignInBusy(self._running)
            job = Run(id=uuid.uuid4().hex[:12], kind="sign_in", status="signing_in")
            self._jobs[job.id] = job
            self._running = job.id
        job.set_status("signing_in")
        self._worker.submit(self._run_sign_in, job)
        return job

    def _run_sign_in(self, job: Run) -> None:
        def say(message: str) -> None:
            job.emit("note", step="sign_in", message=scrub(str(message)))

        try:
            try:
                result = self._sign_in(profile_dir=self.profile_dir, on_status=say)
                self.cache.save_refresh_token(result.refresh_token)
                del result
            except PlaywrightMissing:
                job.error = {"kind": "playwright_missing", "message": INSTALL_HINT}
            except (BrowserAuthError, auth.AuthError) as exc:
                job.error = {"kind": "failed", "message": scrub(str(exc))}
            except Exception as exc:
                # a Playwright error: the window was closed, or its browser is
                # missing. Its text can carry sign-in URLs, so only the type.
                log.warning("sign-in stopped: %s", type(exc).__name__)
                job.error = {"kind": "stopped",
                             "message": STOPPED.format(name=type(exc).__name__)}

            if job.error:
                job.emit("failed", **job.error)
                job.set_status("sign_in_failed")
            else:
                job.emit("done", step="sign_in")
                job.set_status("signed_in")
        finally:
            with self._lock:
                self._running = None

    # --------------------------------------------------- the HAR fallback

    def from_har(self, raw: bytes) -> None:
        """Start a session from a saved sign-in capture. Raises HarRejected.

        The capture holds live tokens, so the copy written for the parser is
        removed whatever happens -- including when it does not parse.
        """
        if self.env_token:
            raise SignInRefused("ANDROMEDA_TOKEN is set, so the app uses that token. "
                                "Restart the app without it to sign in.", "env")
        handle = tempfile.NamedTemporaryFile(suffix=".har", delete=False)
        try:
            with handle:
                handle.write(raw)
            try:
                refresh = auth.refresh_token_from_har(handle.name)
            except auth.AuthError as exc:
                raise HarRejected(str(exc).replace(handle.name, "the file")) from None
            except (ValueError, UnicodeDecodeError, AttributeError, TypeError):
                raise HarRejected("That file is not a HAR capture. In DevTools, "
                                  "right-click the list of requests and choose Save all "
                                  "as HAR with content.") from None
        finally:
            os.unlink(handle.name)

        # check it before storing it, so a stale capture cannot replace a
        # session that works
        try:
            auth.refresh_access_token(self.scorpius(), refresh)
        except ApiError as exc:
            if 400 <= exc.status < 500:
                raise HarRejected("The sign-in in that file has run out (a sign-in lasts "
                                  "about a day). Capture a fresh one.") from None
            raise HarRejected(f"The sign-in service answered {exc.status}. Try again in "
                              f"a minute.") from None
        except auth.AuthError:
            raise HarRejected("The sign-in service did not accept the session in that "
                              "file. Capture a fresh one.") from None
        except Exception as exc:
            raise HarRejected(f"Could not reach the sign-in service "
                              f"({type(exc).__name__}).") from None
        try:
            self.cache.save_refresh_token(refresh)
        except auth.AuthError as exc:
            raise HarRejected(str(exc)) from None
