"""Sign in to Andromeda through a browser the tool drives.

Andromeda's Google OAuth client only accepts its own redirect URI, so the
authorization code can never land anywhere this tool could catch it. The way
round that is not to intercept the redirect but to *be* the browser: drive a
real one, let the normal sign-in happen inside it, and read the tokens out of
the response.

    https://api-sandbox.rapidsos.com/v1/scorpius/openid-connect/google
        ?redirect_uri=https://andromeda.sandbox.rapidsos.com/login

Opening that starts the flow. Google redirects back with a code, the Andromeda
page exchanges it, and the exchange response carries `{token, refresh_token}`
-- which is what we want.

Design notes
------------
* **The Google sign-in itself is never automated.** The first run opens a
  window and a person signs in by hand. After that the profile holds Google's
  session cookies, which last weeks, so later runs complete without
  interaction. That matters: automating a Google sign-in fights bot defences
  and breaks without warning; reusing a session does not.

* **A repeat sign-in runs out of sight, but never waits there for long.** If
  Google shows a page instead of completing -- an account chooser, a
  re-authentication -- nobody can click it in a headless browser. So the quiet
  attempt gives up after a few seconds on a Google page, or `QUIET_TIMEOUT`
  overall, and a visible window takes over. Every wait reports progress.

* **The tokens are read from the network, not from storage.** Local storage
  key names are undocumented and could change; the exchange response shape is
  something we have captured and rely on elsewhere.

* **The profile is per person.** Everyone signs in as themselves with their
  own permissions, and nothing is shared. The profile directory holds live
  Google session cookies -- it is as sensitive as being logged in, and must
  not be committed or copied between people.

* Playwright is imported only when a sign-in actually runs, so the rest of the
  package has no browser dependency.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_PROFILE_DIR",
    "BrowserAuthError",
    "PlaywrightMissing",
    "SignInResult",
    "is_exchange_response",
    "is_google_page",
    "sign_in",
    "start_url",
    "wait_for_tokens",
]

SCORPIUS = "https://api-sandbox.rapidsos.com"
OIDC_PATH = "/v1/scorpius/openid-connect/google"
ANDROMEDA_LOGIN = "https://andromeda.sandbox.rapidsos.com/login"

#: Where the browser profile lives. Gitignore this -- it holds live sessions.
DEFAULT_PROFILE_DIR = Path(".andromeda-browser")

#: Seconds the invisible attempt gets. A working saved session finishes in
#: two or three; anything longer is waiting for something that will not come.
QUIET_TIMEOUT = 20

#: Seconds on a Google page before the invisible attempt gives up on it. A
#: working session only passes through Google on redirects; one that stays
#: there is showing a page for a person. Guessing wrong costs little: the
#: visible window that opens instead completes on its own.
GOOGLE_GRACE = 3

#: Seconds between "still waiting" messages, so a wait never looks frozen.
PROGRESS_EVERY = 10

# how a wait ended
SIGNED_IN = "signed in"
TIMED_OUT = "timed out"
NEEDS_PERSON = "needs a person"

INSTALL_HINT = (
    "Browser sign-in needs Playwright. Install it once:\n"
    "    python -m pip install playwright\n"
    "    python -m playwright install chromium"
)


class BrowserAuthError(Exception):
    """Base for this module."""


class PlaywrightMissing(BrowserAuthError):
    def __init__(self) -> None:
        super().__init__(INSTALL_HINT)


@dataclass(frozen=True)
class SignInResult:
    token: str
    refresh_token: str
    interactive: bool          # did a person have to do something?

    def __repr__(self) -> str:                    # never print either token
        how = "signed in by hand" if self.interactive else "reused the saved session"
        return f"SignInResult({how})"


def start_url(redirect_uri: str = ANDROMEDA_LOGIN) -> str:
    """The URL that begins the sign-in. Opening it forces the whole flow,
    so a still-valid Google session completes it without interaction."""
    return f"{SCORPIUS}{OIDC_PATH}?redirect_uri={redirect_uri}"


def is_exchange_response(url: str, method: str) -> bool:
    """Is this the call that returns the tokens?

    The same path serves a GET that starts the flow and a POST that finishes
    it; only the POST carries tokens.
    """
    return method.upper() == "POST" and OIDC_PATH in url


def tokens_from(payload: Mapping[str, Any]) -> tuple[str, str]:
    """Pull the pair out of an exchange response."""
    token = payload.get("token") or payload.get("access_token")
    refresh = payload.get("refresh_token")
    if not token or not refresh:
        raise BrowserAuthError(
            f"the sign-in response carried no token pair; keys were {sorted(payload)}"
        )
    return token, refresh


def is_google_page(url: str) -> bool:
    """Is the browser showing one of Google's own pages -- a sign-in, an
    account chooser, a consent screen? Those wait for a person."""
    host = (urlsplit(url).hostname or "").lower()
    return host == "google.com" or host.endswith(".google.com")


def wait_for_tokens(
    page: Any,
    captured: Mapping[str, Any],
    *,
    timeout: float,
    say: Callable[[str], None],
    waiting_for: str,
    person_needed_after: float | None = None,
    step: float = 0.25,
    progress_every: float = PROGRESS_EVERY,
) -> str:
    """Wait until `captured` fills with the token exchange.

    Returns SIGNED_IN, TIMED_OUT, or -- when `person_needed_after` is given and
    the page has sat on a Google page that long -- NEEDS_PERSON. A browser
    nobody can see must not wait for a click that cannot come.

    Polls rather than waiting for an event: the exchange may already have
    happened by the time a listener would attach. Time is counted in steps
    waited, not wall-clock time.
    """
    waited = 0.0
    on_google = 0.0
    next_progress = progress_every
    while not captured:
        if waited >= timeout:
            return TIMED_OUT
        if waited >= next_progress:
            say(f"still waiting for {waiting_for} ({waited:.0f}s)")
            next_progress += progress_every
        page.wait_for_timeout(step * 1000)
        waited += step
        if person_needed_after is not None and not captured:
            on_google = on_google + step if is_google_page(page.url) else 0.0
            if on_google >= person_needed_after:
                return NEEDS_PERSON
    return SIGNED_IN


def sign_in(
    *,
    profile_dir: str | Path = DEFAULT_PROFILE_DIR,
    timeout: float = 300,
    quiet_timeout: float = QUIET_TIMEOUT,
    headless: bool | None = None,
    on_status: Callable[[str], None] | None = None,
) -> SignInResult:
    """Open a browser, complete the Andromeda sign-in, return the tokens.

    The first run needs a person: a window opens at Google's sign-in and waits.
    Once the profile holds a Google session, later runs finish on their own,
    usually in a couple of seconds.

    `headless` defaults to "headless only if the profile already exists",
    since a first sign-in cannot be done without a window. Google is also more
    likely to challenge a headless browser, so the fallback is to reopen a
    visible one rather than fail. The headless attempt gets `quiet_timeout`
    seconds, and gives up sooner if it lands on a Google page that stays --
    an account chooser, say -- since nobody can click on it. The visible
    attempt waits up to `timeout` for the person.

    Raises
    ------
    PlaywrightMissing
        Playwright is not installed.
    BrowserAuthError
        The sign-in did not complete within `timeout`.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PlaywrightMissing() from exc

    profile = Path(profile_dir)
    returning = profile.exists()
    if headless is None:
        headless = returning

    say = on_status or (lambda message: log.info("%s", message))
    say("reusing the saved sign-in" if returning
        else "first run: a window will open for you to sign in with Google")

    captured: dict[str, Any] = {}

    def watch(response) -> None:
        if captured or not is_exchange_response(response.url, response.request.method):
            return
        try:
            captured.update(response.json())
        except Exception:                       # not JSON, or already consumed
            pass

    def attempt(visible: bool) -> str:
        captured.clear()
        with sync_playwright() as play:
            context = play.chromium.launch_persistent_context(
                str(profile), headless=not visible
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.on("response", watch)
                page.goto(start_url(), wait_until="commit")
                if visible:
                    return wait_for_tokens(
                        page, captured, timeout=timeout, say=say,
                        waiting_for="you to finish signing in in the browser window",
                    )
                return wait_for_tokens(
                    page, captured, timeout=quiet_timeout, say=say,
                    waiting_for="the saved sign-in", person_needed_after=GOOGLE_GRACE,
                )
            finally:
                context.close()

    outcome = attempt(visible=not headless)
    if outcome != SIGNED_IN and headless:
        say("Google wants you to choose or confirm your account; opening a window"
            if outcome == NEEDS_PERSON else
            "the saved session did not complete on its own; opening a window")
        outcome = attempt(visible=True)

    if outcome != SIGNED_IN:
        raise BrowserAuthError(
            "the sign-in did not finish in time. If the window was waiting for "
            "you, run it again and complete the Google sign-in."
        )

    token, refresh = tokens_from(captured)
    say("signed in")
    return SignInResult(token=token, refresh_token=refresh, interactive=not returning)
