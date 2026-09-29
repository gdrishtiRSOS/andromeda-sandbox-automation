"""Tests for the shared HTTP client."""

from __future__ import annotations

import json

import pytest
import requests

from logic.client import ApiError, AuthError, HttpClient


@pytest.fixture
def sent(monkeypatch):
    """Answer every request with `sent.reply`; record what was sent."""
    class Recorder(list):
        reply = (200, {"ok": True})

    calls = Recorder()

    def request(self, method, url, **kw):
        calls.append({"method": method, "url": url, "headers": dict(self.headers), **kw})
        status, body = calls.reply
        response = requests.Response()
        response.status_code = status
        response._content = body if isinstance(body, bytes) else json.dumps(body).encode()
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    return calls


def test_error_text_keeps_the_shape_the_modules_parse(sent):
    sent.reply = (500, b"Internal Server Error")
    with pytest.raises(ApiError) as exc:
        HttpClient("https://h/").post("/v1/capstone/psaps/", {})
    assert str(exc.value) == "500 on POST /v1/capstone/psaps/: Internal Server Error"
    assert (exc.value.status, exc.value.method, exc.value.path) == \
        (500, "POST", "/v1/capstone/psaps/")


def test_error_body_is_truncated_in_the_message_but_not_on_the_error(sent):
    sent.reply = (409, b"x" * 1000)
    with pytest.raises(ApiError) as exc:
        HttpClient("https://h").get("/p")
    assert len(str(exc.value)) == len("409 on GET /p: ") + 300
    assert len(exc.value.text) == 1000


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_token_is_an_auth_error(sent, status):
    sent.reply = (status, b"expired")
    with pytest.raises(AuthError) as exc:
        HttpClient("https://h", token="abc").get("/p")
    assert isinstance(exc.value, ApiError)
    assert exc.value.token_sent is True


def test_auth_error_says_when_no_token_was_sent(sent):
    sent.reply = (401, b"")
    with pytest.raises(AuthError) as exc:
        HttpClient("https://h").get("/p")
    assert exc.value.token_sent is False


def test_on_auth_error_is_called_before_raising(sent):
    sent.reply = (401, b"")
    seen = []
    client = HttpClient("https://h", on_auth_error=seen.append)
    with pytest.raises(AuthError):
        client.get("/p")
    assert [e.status for e in seen] == [401]


def test_on_auth_error_is_not_called_for_other_errors(sent):
    sent.reply = (404, b"")
    seen = []
    with pytest.raises(ApiError):
        HttpClient("https://h", on_auth_error=seen.append).get("/p")
    assert seen == []


def test_headers(sent):
    HttpClient("https://h/", token="Bearer abc", org="RapidSOS Admin",
               cookie="c=1", headers={"X-A": "b"}).get("/p")
    call = sent[0]
    assert call["url"] == "https://h/p"
    assert call["timeout"] == 60
    headers = call["headers"]
    assert headers["Authorization"] == "Bearer abc"
    assert headers["x-rapidsos-org"] == "RapidSOS Admin"
    assert headers["Cookie"] == "c=1"
    assert headers["X-A"] == "b"
    assert headers["Accept"] == "application/json"


def test_a_bare_token_gets_the_bearer_prefix(sent):
    HttpClient("https://h", token="  abc  ").get("/p")
    assert sent[0]["headers"]["Authorization"] == "Bearer abc"


def test_no_credential_means_no_auth_headers(sent):
    HttpClient("https://h").get("/p")
    assert "Authorization" not in sent[0]["headers"]
    assert "x-rapidsos-org" not in sent[0]["headers"]


def test_empty_body_is_none(sent):
    sent.reply = (204, b"")
    assert HttpClient("https://h").put("/p", {}) is None


def test_repr_never_shows_the_token():
    assert "abc" not in repr(HttpClient("https://h", token="abc"))
