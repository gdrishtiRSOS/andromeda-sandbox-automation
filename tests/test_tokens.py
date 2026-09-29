"""Tests for reading a pasted token's claims."""

from __future__ import annotations

import base64
import datetime as dt
import json

import pytest

from logic.tokens import bearer, decode_claims, decode_token, read_claims

NOW = dt.datetime(2026, 9, 24, 12, 0, tzinfo=dt.timezone.utc)


def make_token(**claims):
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'RS256'})}.{seg(claims)}.c2lnbmF0dXJl"


def test_reads_user_and_expiry():
    exp = int((NOW + dt.timedelta(hours=2)).timestamp())
    info = decode_token(make_token(username="ada@rapidsos.com", sub=42, exp=exp))
    assert info.ok
    assert info.user == "ada@rapidsos.com"
    assert info.subject == "42"
    assert info.seconds_left(NOW) == 7200
    assert not info.expired(NOW)


def test_bearer_prefix_is_tolerated():
    token = make_token(username="ada", exp=0)
    assert decode_token(f"Bearer {token}").ok
    assert bearer(f"  bearer {token} ") == f"Bearer {token}"
    assert bearer(token) == f"Bearer {token}"


def test_expired_is_reported_but_still_decodes():
    exp = int((NOW - dt.timedelta(minutes=5)).timestamp())
    info = decode_token(make_token(exp=exp))
    assert info.ok
    assert info.expired(NOW)


def test_email_claim_is_used_when_there_is_no_username():
    assert decode_token(make_token(email="b@x.com")).user == "b@x.com"


def test_empty():
    assert decode_token(None).problem == "not set"
    assert decode_token("   ").problem == "not set"


def test_shell_quoting_is_caught():
    info = decode_token(f"'{make_token(exp=1)}'")
    assert not info.ok
    assert "quotes" in info.problem


def test_truncated():
    info = decode_token(make_token(exp=1).rsplit(".", 1)[0])
    assert not info.ok
    assert "2 part(s)" in info.problem


def test_illegal_characters():
    assert not decode_token("abc.d%ef.ghi").ok


def test_garbage_payload():
    assert not decode_token("abc.!!!!.ghi").ok
    assert not decode_token("abc.def.ghi").ok


def test_the_token_never_appears_in_a_problem_message():
    token = make_token(exp=1) + "%"
    info = decode_token(token)
    assert token not in (info.problem or "")


def test_decode_claims_returns_the_payload():
    assert decode_claims(make_token(sub=42, exp=1)) == {"sub": 42, "exp": 1}


def test_decode_claims_refuses_an_unreadable_payload():
    with pytest.raises(ValueError, match="could not be decoded"):
        decode_claims("abc.!!!!.ghi")
    with pytest.raises(ValueError, match="could not be decoded"):
        decode_claims("no-dots")


def test_decode_claims_refuses_a_payload_that_is_not_an_object():
    seg = base64.urlsafe_b64encode(b"[1, 2]").rstrip(b"=").decode()
    with pytest.raises(ValueError, match="not an object"):
        decode_claims(f"abc.{seg}.ghi")


def test_read_claims_never_raises():
    assert read_claims(make_token(sub=1)) == {"sub": 1}
    assert read_claims("abc.!!!!.ghi") == {}
    assert read_claims("") == {}
    assert read_claims(None) == {}
