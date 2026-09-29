"""Tests for minting Andromeda tokens from a refresh token."""

from __future__ import annotations

import base64
import datetime as dt
import json
import time

import pytest

from logic.auth import (
    AuthError,
    Session,
    TokenCache,
    refresh_access_token,
    refresh_token_from_har,
)


def jwt(*, username="gdrishti@rapidsos.com", sub="42081", type_="access", exp_in=14400):
    head = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({
        "iss": "RapidSOS", "sub": sub, "username": username,
        "type": type_, "exp": int(time.time()) + exp_in,
    }).encode()).decode().rstrip("=")
    return f"{head}.{payload}.signature"


class FakeClient:
    """The refresh endpoint: returns only `token`, never a new refresh token."""

    def __init__(self, *, token=None, fail=None):
        self.token = token or jwt()
        self.fail = fail
        self.posts = []

    def post(self, path, json):
        self.posts.append((path, json))
        if self.fail:
            raise RuntimeError(self.fail)
        return {"token": self.token}


# ---------------------------------------------------------------- session


def test_refreshing_mints_an_access_token():
    client = FakeClient()
    session = refresh_access_token(client, "refresh-abc")

    assert client.posts == [
        ("/v1/scorpius/user/api-token-auth/refresh", {"refresh_token": "refresh-abc"}),
    ]
    assert session.token == client.token
    assert session.username == "gdrishti@rapidsos.com"
    assert session.subject == "42081"
    assert session.seconds_left() > 0


def test_the_refresh_token_is_carried_forward():
    """The response has no refresh_token; the one we used stays valid."""
    session = refresh_access_token(FakeClient(), "refresh-abc")
    assert session.refresh_token == "refresh-abc"


def test_neither_token_appears_in_the_repr():
    session = refresh_access_token(FakeClient(), "refresh-abc")
    assert session.token not in repr(session)
    assert "refresh-abc" not in repr(session)
    assert "gdrishti@rapidsos.com" in repr(session)


def test_a_response_without_a_token_is_refused():
    class Empty(FakeClient):
        def post(self, path, json):
            return {"detail": "ok"}

    with pytest.raises(AuthError, match="no token"):
        refresh_access_token(Empty(), "refresh-abc")


def test_an_almost_expired_session_is_flagged():
    soon = Session.from_api({"token": jwt(exp_in=120)})
    later = Session.from_api({"token": jwt(exp_in=7200)})
    assert soon.expiring()
    assert not later.expiring()


def test_a_token_without_an_expiry_is_never_flagged():
    assert Session.from_api({"token": "opaque"}).expiring() is False


# ------------------------------------------------------------------ cache


def test_a_refresh_token_can_be_stored_and_read_back(tmp_path):
    cache = TokenCache(tmp_path / "session.json")
    token = jwt(type_="refresh", exp_in=86400)
    cache.save_refresh_token(token)
    assert cache.refresh_token == token


def test_an_access_token_is_refused_where_a_refresh_token_belongs(tmp_path):
    cache = TokenCache(tmp_path / "session.json")
    with pytest.raises(AuthError, match="not a refresh token"):
        cache.save_refresh_token(jwt(type_="access"))


def test_an_opaque_token_is_accepted(tmp_path):
    """Claims are a convenience; a non-JWT refresh token must still store."""
    cache = TokenCache(tmp_path / "session.json")
    cache.save_refresh_token("opaque-value")
    assert cache.refresh_token == "opaque-value"


def test_the_first_call_mints_and_stores(tmp_path):
    cache = TokenCache(tmp_path / "session.json")
    cache.save_refresh_token(jwt(type_="refresh", exp_in=86400))
    client = FakeClient()

    session = cache.access_token(client)
    assert len(client.posts) == 1
    assert cache.cached_session().token == session.token


def test_a_fresh_cached_token_is_reused(tmp_path):
    cache = TokenCache(tmp_path / "session.json")
    cache.save_refresh_token(jwt(type_="refresh", exp_in=86400))
    client = FakeClient()

    cache.access_token(client)
    cache.access_token(client)
    assert len(client.posts) == 1          # no second refresh


def test_an_expiring_cached_token_is_replaced(tmp_path):
    cache = TokenCache(tmp_path / "session.json")
    cache.save_refresh_token(jwt(type_="refresh", exp_in=86400))
    client = FakeClient(token=jwt(exp_in=60))

    cache.access_token(client)
    client.token = jwt(exp_in=14400)
    cache.access_token(client)
    assert len(client.posts) == 2


def test_a_refresh_can_be_forced(tmp_path):
    cache = TokenCache(tmp_path / "session.json")
    cache.save_refresh_token(jwt(type_="refresh", exp_in=86400))
    client = FakeClient()
    cache.access_token(client)
    cache.access_token(client, force=True)
    assert len(client.posts) == 2


def test_no_stored_refresh_token_is_a_clear_error(tmp_path):
    cache = TokenCache(tmp_path / "nothing.json")
    with pytest.raises(AuthError, match="no refresh token stored"):
        cache.access_token(FakeClient())


def test_an_unreadable_cache_is_ignored_not_fatal(tmp_path):
    path = tmp_path / "session.json"
    path.write_text("{not json")
    with pytest.raises(AuthError, match="no refresh token stored"):
        TokenCache(path).access_token(FakeClient())


def test_clearing_removes_the_file(tmp_path):
    cache = TokenCache(tmp_path / "session.json")
    cache.save_refresh_token("opaque")
    cache.clear()
    assert cache.refresh_token is None
    cache.clear()                           # twice is fine


def test_a_dead_refresh_token_surfaces_the_error(tmp_path):
    cache = TokenCache(tmp_path / "session.json")
    cache.save_refresh_token(jwt(type_="refresh", exp_in=86400))
    with pytest.raises(RuntimeError, match="401"):
        cache.access_token(FakeClient(fail="401 on POST .../refresh: expired"))


# -------------------------------------------------------------- from HAR


def har_with(response_body, url="https://api-sandbox.rapidsos.com/v1/scorpius/openid-connect/google"):
    return {"log": {"entries": [
        {"request": {"method": "GET", "url": "https://example.com/login"},
         "response": {"content": {"text": "{}"}}},
        {"request": {"method": "POST", "url": url},
         "response": {"content": {"text": json.dumps(response_body)}}},
    ]}}


def test_the_refresh_token_is_found_in_a_sign_in_capture(tmp_path):
    path = tmp_path / "login.har"
    path.write_text(json.dumps(har_with({"token": "a", "refresh_token": "r-123"})))
    assert refresh_token_from_har(path) == "r-123"


def test_a_capture_without_the_sign_in_is_a_clear_error(tmp_path):
    path = tmp_path / "other.har"
    path.write_text(json.dumps({"log": {"entries": []}}))
    with pytest.raises(AuthError, match="no Google sign-in call"):
        refresh_token_from_har(path)


def test_a_capture_saved_without_bodies_says_so(tmp_path):
    path = tmp_path / "login.har"
    har = har_with({})
    har["log"]["entries"][1]["response"]["content"].pop("text")
    path.write_text(json.dumps(har))
    with pytest.raises(AuthError, match="no response body"):
        refresh_token_from_har(path)


def test_a_sign_in_without_a_refresh_token_is_reported(tmp_path):
    path = tmp_path / "login.har"
    path.write_text(json.dumps(har_with({"token": "a"})))
    with pytest.raises(AuthError, match="no refresh_token"):
        refresh_token_from_har(path)
