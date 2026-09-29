"""Tests for browser-driven sign-in.

Playwright is not needed: everything here exercises the pure parts -- which
response carries the tokens, how they are read out, and what happens when it
is missing. Driving a real browser is not something a unit test should do.
"""

from __future__ import annotations

import pytest

from logic.browser_auth import (
    ANDROMEDA_LOGIN,
    BrowserAuthError,
    SignInResult,
    is_exchange_response,
    start_url,
    tokens_from,
)

EXCHANGE = "https://api-sandbox.rapidsos.com/v1/scorpius/openid-connect/google"


# ------------------------------------------------------------ the start


def test_the_start_url_begins_the_flow():
    url = start_url()
    assert url.startswith("https://api-sandbox.rapidsos.com/v1/scorpius/openid-connect/google")
    assert ANDROMEDA_LOGIN in url


def test_the_redirect_can_be_pointed_elsewhere():
    assert "https://example.test/x" in start_url("https://example.test/x")


# --------------------------------------------------- spotting the tokens


def test_the_post_carries_the_tokens():
    assert is_exchange_response(EXCHANGE, "POST")


def test_the_get_that_starts_the_flow_does_not():
    """Same path, different method -- only the POST returns tokens."""
    assert not is_exchange_response(f"{EXCHANGE}?redirect_uri=x", "GET")


def test_the_method_is_matched_case_insensitively():
    assert is_exchange_response(EXCHANGE, "post")


@pytest.mark.parametrize("url", [
    "https://api-sandbox.rapidsos.com/v1/scorpius/user",
    "https://accounts.google.com/o/oauth2/v2/auth",
    "https://andromeda.sandbox.rapidsos.com/login",
])
def test_other_calls_are_ignored(url):
    assert not is_exchange_response(url, "POST")


# ------------------------------------------------------ reading them out


def test_both_tokens_are_read():
    token, refresh = tokens_from({"token": "a", "refresh_token": "r"})
    assert (token, refresh) == ("a", "r")


def test_access_token_is_accepted_as_an_alias():
    assert tokens_from({"access_token": "a", "refresh_token": "r"})[0] == "a"


@pytest.mark.parametrize("payload", [
    {"token": "a"},                     # no refresh token
    {"refresh_token": "r"},             # no access token
    {},                                 # neither
    {"detail": "nope"},
])
def test_a_response_without_the_pair_is_refused(payload):
    with pytest.raises(BrowserAuthError, match="no token pair"):
        tokens_from(payload)


# ------------------------------------------------------------- reporting


def test_neither_token_appears_in_the_repr():
    result = SignInResult(token="secret-a", refresh_token="secret-r", interactive=False)
    text = repr(result)
    assert "secret-a" not in text and "secret-r" not in text
    assert "reused the saved session" in text


def test_a_first_run_says_so():
    assert "by hand" in repr(SignInResult(token="a", refresh_token="r", interactive=True))


# -------------------------------------------------------------- absence


def test_a_missing_playwright_explains_how_to_install_it(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kw):
        if name.startswith("playwright"):
            raise ImportError("no playwright")
        return real_import(name, *args, **kw)

    monkeypatch.setattr(builtins, "__import__", refuse)

    from logic.browser_auth import PlaywrightMissing, sign_in

    with pytest.raises(PlaywrightMissing) as exc:
        sign_in()
    assert "pip install playwright" in str(exc.value)
    assert "playwright install chromium" in str(exc.value)


# ------------------------------------------------------------- waiting


GOOGLE_CHOOSER = "https://accounts.google.com/v3/signin/accountchooser?client_id=x"
ANDROMEDA_PAGE = "https://andromeda.sandbox.rapidsos.com/login?code=abc"


class FakeExchange:
    """The POST whose response carries the tokens."""
    url = EXCHANGE
    request = type("Request", (), {"method": "POST"})()

    @staticmethod
    def json():
        return {"token": "a", "refresh_token": "r"}


class FakePage:
    """A page whose URL follows a script, [(seconds, url), ...], on a clock
    that only moves when the code under test waits. At `signs_in_at` seconds
    the token exchange arrives: through the response handler when one is
    registered, as sign_in does, or straight into `captured` otherwise."""

    def __init__(self, urls, *, signs_in_at=None, captured=None):
        self.urls = urls
        self.signs_in_at = signs_in_at
        self.captured = captured if captured is not None else {}
        self.handler = None
        self.clock = 0.0

    @property
    def url(self):
        return [u for t, u in self.urls if t <= self.clock][-1]

    def on(self, event, handler):
        assert event == "response"
        self.handler = handler

    def goto(self, url, wait_until):
        pass

    def wait_for_timeout(self, ms):
        self.clock += ms / 1000
        if self.signs_in_at is not None and self.clock >= self.signs_in_at:
            self.signs_in_at = None
            if self.handler:
                self.handler(FakeExchange())
            else:
                self.captured["token"] = "a"


def wait(page, **kw):
    from logic.browser_auth import wait_for_tokens
    said = []
    kw.setdefault("timeout", 20)
    outcome = wait_for_tokens(page, page.captured, say=said.append,
                              waiting_for="the saved sign-in", **kw)
    return outcome, said, page.clock


def test_a_working_session_signs_in_even_passing_through_google():
    from logic.browser_auth import SIGNED_IN
    page = FakePage([(0, "https://api-sandbox.rapidsos.com/x"),
                     (0.5, "https://accounts.google.com/o/oauth2/auth"),
                     (1.5, ANDROMEDA_PAGE)], signs_in_at=2)
    outcome, said, _ = wait(page, person_needed_after=3)
    assert outcome == SIGNED_IN
    assert said == []


def test_a_google_page_that_stays_needs_a_person():
    """The reported hang: headless, parked on Google's account chooser."""
    from logic.browser_auth import NEEDS_PERSON
    page = FakePage([(0, "https://api-sandbox.rapidsos.com/x"), (1, GOOGLE_CHOOSER)])
    outcome, _, clock = wait(page, person_needed_after=3)
    assert outcome == NEEDS_PERSON
    # 1s to get there, 3s on it -- to within one 0.25s poll
    assert clock == pytest.approx(4, abs=0.25)


def test_leaving_google_resets_the_count():
    from logic.browser_auth import SIGNED_IN
    page = FakePage([(0, GOOGLE_CHOOSER), (2, ANDROMEDA_PAGE), (3, GOOGLE_CHOOSER),
                     (5, ANDROMEDA_PAGE)], signs_in_at=6)
    outcome, _, _ = wait(page, person_needed_after=3)
    assert outcome == SIGNED_IN


def test_without_the_google_check_a_google_page_is_waited_on():
    """The visible window: the person is on that page, signing in."""
    from logic.browser_auth import SIGNED_IN
    page = FakePage([(0, GOOGLE_CHOOSER)], signs_in_at=30)
    outcome, _, _ = wait(page, timeout=300)
    assert outcome == SIGNED_IN


def test_a_wait_that_goes_nowhere_times_out():
    from logic.browser_auth import TIMED_OUT
    page = FakePage([(0, ANDROMEDA_PAGE)])
    outcome, _, clock = wait(page, timeout=20, person_needed_after=3)
    assert outcome == TIMED_OUT
    assert clock == pytest.approx(20)


def test_a_long_wait_reports_progress():
    page = FakePage([(0, GOOGLE_CHOOSER)], signs_in_at=35)
    _, said, _ = wait(page, timeout=300, progress_every=10)
    assert said == ["still waiting for the saved sign-in (10s)",
                    "still waiting for the saved sign-in (20s)",
                    "still waiting for the saved sign-in (30s)"]


@pytest.mark.parametrize("url,google", [
    (GOOGLE_CHOOSER, True),
    ("https://google.com/", True),
    ("https://consent.google.com/x", True),
    (ANDROMEDA_PAGE, False),
    ("https://notgoogle.com/", False),
    ("about:blank", False),
])
def test_google_pages_are_recognised(url, google):
    from logic.browser_auth import is_google_page
    assert is_google_page(url) is google


# ------------------------------------------- the whole sign-in, stubbed


class FakePlaywright:
    """Stands in for playwright.sync_api. Each browser launch plays the next
    script in `scripts`: (urls, signs_in_at), as FakePage takes them."""

    def __init__(self):
        self.scripts = []
        self.launches = []

    def sync_playwright(self):
        fake = self

        class Context:
            def __init__(self, page):
                self.pages = [page]

            def close(self):
                pass

        class Chromium:
            def launch_persistent_context(self, path, headless):
                urls, signs_in_at = fake.scripts.pop(0)
                fake.launches.append("headless" if headless else "visible")
                return Context(FakePage(urls, signs_in_at=signs_in_at))

        class Play:
            chromium = Chromium()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return Play()


@pytest.fixture
def playwright(monkeypatch, tmp_path):
    import sys
    import types

    fake = FakePlaywright()
    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = fake.sync_playwright
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    fake.profile = tmp_path / "profile"
    fake.profile.mkdir()                        # a returning user
    return fake


def signing_in(playwright, **kw):
    from logic.browser_auth import sign_in
    said = []
    result = sign_in(profile_dir=kw.pop("profile_dir", playwright.profile),
                     on_status=said.append, **kw)
    return result, said


def test_a_returning_sign_in_that_completes_stays_headless(playwright):
    playwright.scripts = [([(0, ANDROMEDA_PAGE)], 2)]
    result, said = signing_in(playwright)
    assert playwright.launches == ["headless"]
    assert (result.token, result.refresh_token) == ("a", "r")
    assert said == ["reusing the saved sign-in", "signed in"]


def test_an_account_chooser_opens_a_window_within_seconds(playwright):
    playwright.scripts = [([(0, GOOGLE_CHOOSER)], None),    # headless: parked
                          ([(0, GOOGLE_CHOOSER)], 8)]       # visible: a click
    _, said = signing_in(playwright)
    assert playwright.launches == ["headless", "visible"]
    assert said == ["reusing the saved sign-in",
                    "Google wants you to choose or confirm your account; opening a window",
                    "signed in"]


def test_a_stalled_headless_attempt_gives_up_after_the_quiet_timeout(playwright):
    playwright.scripts = [([(0, ANDROMEDA_PAGE)], None), ([(0, ANDROMEDA_PAGE)], 1)]
    _, said = signing_in(playwright, quiet_timeout=20)
    assert playwright.launches == ["headless", "visible"]
    assert "the saved session did not complete on its own; opening a window" in said
    assert "still waiting for the saved sign-in (10s)" in said


def test_a_window_nobody_finishes_is_an_error(playwright):
    playwright.scripts = [([(0, GOOGLE_CHOOSER)], None), ([(0, GOOGLE_CHOOSER)], None)]
    with pytest.raises(BrowserAuthError, match="did not finish in time"):
        signing_in(playwright, timeout=30)
    assert playwright.launches == ["headless", "visible"]


def test_a_first_run_goes_straight_to_a_window(playwright, tmp_path):
    playwright.scripts = [([(0, GOOGLE_CHOOSER)], 5)]
    _, said = signing_in(playwright, profile_dir=tmp_path / "new")
    assert playwright.launches == ["visible"]
    assert said[0] == "first run: a window will open for you to sign in with Google"
