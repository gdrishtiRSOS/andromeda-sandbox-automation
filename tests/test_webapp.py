"""Tests for the web layer: routes, the phase gate, streaming, and sign-in.

Nothing is pasted. The Andromeda token is minted from a stored session (or
taken from ANDROMEDA_TOKEN), and the portal side logs in as the account
itself, so the fakes answer the refresh and login calls as well.

The business logic has its own tests; these check that the page's routes
drive it correctly -- and that no token ever leaks into state, events or logs.
Fake clients stand in for every host, and a fake stands in for the browser;
nothing touches the network.
"""

from __future__ import annotations

import base64
import copy
import datetime as dt
import json
import logging
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from logic.auth import OIDC_PATH, REFRESH_PATH, SCORPIUS_BASE, TokenCache  # noqa: E402
from logic.browser_auth import PlaywrightMissing, SignInResult  # noqa: E402
from logic.places import AmbiguousPlaceError, ResolvedPlace, UnusableBoundaryError  # noqa: E402
from logic.signup import (  # noqa: E402
    CONFIRM_PATH, DEFAULT_PASSWORD, LOGIN_PATH, PSAP_PATH, REGISTER_PATH)
from test_configure_account import Andromeda, Portal, authority_record  # noqa: E402
from webapp import session as session_module  # noqa: E402
from webapp.app import create_app  # noqa: E402
from logic.client import ApiError, AuthError  # noqa: E402
from webapp.runs import QUIET  # noqa: E402


def make_token(user, minutes=120, **claims):
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    exp = int((dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)).timestamp())
    body = {"username": user, "sub": 1, "exp": exp, **claims}
    return f"{seg({'alg': 'RS256'})}.{seg(body)}.U0VOVElORUw"


USER = "andromeda-user@rapidsos.com"
TOKEN_A = make_token(USER)                                   # what a refresh mints
REFRESH = make_token(USER, minutes=24 * 60, type="refresh")  # what a sign-in stores
PAGE = {"X-Sandbox-Page": "1"}                               # sent by the page on writes


class Scorpius:
    """The sign-in service: trades a refresh token for an access token."""

    def __init__(self):
        self.revoked = set()
        self.refreshes = 0

    def post(self, path, json=None):
        assert path == REFRESH_PATH, path
        if json["refresh_token"] in self.revoked:
            raise ApiError(401, '{"detail": "Token is invalid or expired"}', "POST", path)
        self.refreshes += 1
        return {"token": TOKEN_A}


class FakeBrowser:
    """Stands in for browser_auth.sign_in."""

    def __init__(self, refresh=REFRESH, fail=None, hold=None):
        self.refresh, self.fail, self.hold = refresh, fail, hold
        self.calls = []

    def __call__(self, *, profile_dir, on_status=None, **kw):
        self.calls.append(Path(profile_dir))
        on_status("reusing the saved sign-in")
        if self.hold:
            self.hold.wait(5)
        if self.fail:
            raise self.fail
        on_status("signed in")
        return SignInResult(token=TOKEN_A, refresh_token=self.refresh, interactive=False)


class SignupPortal(Portal):
    """The portal host: sign-up plus roles. Registering creates the authority
    in Andromeda the way the real PSAP call does -- answering 500 as it does."""

    def __init__(self, andromeda, **kw):
        super().__init__(**kw)
        self.andromeda = andromeda
        self.registered = []
        self.logins = []             # addresses logged in as
        self.issued = []             # tokens handed out by the login

    def post(self, path, json=None):
        self.calls.append(("POST", path))
        if path == REGISTER_PATH:
            if json["email"] in self.registered:
                raise ApiError(400, '{"email": ["already in use"]}', "POST", path)
            self.registered.append(json["email"])
            return {"id": 77, "organizations": [{"id": 15516}]}
        if path == LOGIN_PATH:
            if json["email"] not in self.registered or json["password"] != DEFAULT_PASSWORD:
                raise ApiError(400, '{"non_field_errors": ["Unable to log in"]}', "POST", path)
            self.logins.append(json["email"])
            token = make_token(json["email"], minutes=60)
            self.issued.append(token)
            return {"token": token, "refresh_token": "R-" + json["email"]}
        if path == PSAP_PATH:
            record = authority_record(name=json["name"])
            record["attributes"] = {"contact_email": json["contact_email"]}
            self.andromeda.authorities.append(record)
            raise ApiError(500, "Internal Server Error", "POST", path)
        if path == CONFIRM_PATH:
            return None
        raise AssertionError(f"unexpected POST {path}")


class Rejecting:
    def _no(self, *a, **k):
        raise AuthError(401, "token expired", "GET", "/x")
    get = post = patch = put = _no


class Hosts:
    """The client factory: one fake per host, recording what it was handed."""

    def __init__(self, andromeda=None):
        self.andromeda = andromeda or Andromeda()
        if andromeda is None:
            self.andromeda.authorities = []
        self.portal = SignupPortal(self.andromeda)
        self.scorpius = Scorpius()
        self.rejected = set()
        self.made = []
        self.portal_tokens = []      # tokens the portal client was built with
        self.andromeda_tokens = []   # ... and the Andromeda client

    def __call__(self, base, token=None, org=None):
        if base == SCORPIUS_BASE:
            assert token is None                     # the refresh is unauthenticated
            return self.scorpius
        self.made.append((base, token is not None, org))
        if token is not None:
            if "andromeda" in base:
                self.andromeda_tokens.append(token)
            else:
                self.portal_tokens.append(token)
        if token in self.rejected:
            return Rejecting()
        return self.andromeda if "andromeda" in base else self.portal


class FakePlaces:
    state, problem = "ready", None

    def __init__(self, outcome):
        self.outcome = outcome

    def warm(self):
        pass

    def resolve(self, query, allow_unverified=False):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def lancaster():
    return ResolvedPlace(
        query="Lincoln, NE", geoid="31109", county_name="Lancaster", state="NE",
        matched_as="city", matched_name="Lincoln",
        result={
            "county": {"name": "Lancaster", "state": "NE", "geoid": "31109",
                       "boundary_type": "county_footprint"},
            "geometry": {"type": "MultiPolygon", "coordinates": [[[
                [-96.9, 40.5], [-96.4, 40.5], [-96.4, 41.0], [-96.9, 41.0], [-96.9, 40.5]]]]},
            "jurisdiction_scope": {"status": "ok", "eccs": [
                {"name": "Lincoln Emergency Communications", "fcc_psap_id": "1234"}]},
        },
    )


def make_app(hosts, tmp_path, *, places=None, signed_in=True, env=None,
             browser=None, can_open_browser=True):
    """The app with fakes for every host and the browser, its session file in
    tmp_path -- never the real one -- and signed in unless told otherwise."""
    cache = TokenCache(tmp_path / "session.json")
    if signed_in:
        cache.save_refresh_token(REFRESH)
    else:
        cache.clear()
    return create_app(make_client=hosts, places=places or FakePlaces(lancaster()),
                      snapshot_dir=tmp_path / "snapshots", sleep=lambda _: None,
                      warm_places=False, session_cache=cache, env=env or {},
                      sign_in=browser or FakeBrowser(),
                      can_open_browser=lambda: can_open_browser)


def page_client(app):
    """A TestClient that sends what the page sends."""
    client = TestClient(app, headers=PAGE)
    client.app_state = app.state
    return client


@pytest.fixture
def hosts():
    return Hosts()


@pytest.fixture
def web(hosts, tmp_path):
    with page_client(make_app(hosts, tmp_path)) as client:
        yield client


@pytest.fixture
def signed_out(hosts, tmp_path):
    with page_client(make_app(hosts, tmp_path, signed_in=False)) as client:
        yield client


def wait(web, run_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = web.get(f"/api/runs/{run_id}").json()
        if state["status"] in QUIET:
            return state
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} still {state['status']}")


FORM = {"email": "ada+lancaster@rapidsos.com", "first_name": "Ada",
        "last_name": "Lovelace", "agency_name": "Lancaster NE",
        "account_id": "SAND_31109", "country": "USA", "state": "NE"}


def preview(web, **overrides):
    boundary = web.get("/api/place", params={"q": "Lincoln, NE"}).json()
    body = {**FORM, "boundary_id": boundary["boundary_id"], **overrides}
    return web.post("/api/runs", json=body)


def signed_up(web):
    run_id = preview(web).json()["run_id"]
    assert web.post(f"/api/runs/{run_id}/signup").status_code == 202
    state = wait(web, run_id)
    assert state["status"] == "awaiting_email", state["error"]
    return run_id


# ------------------------------------------------------------------ happy path


def test_the_whole_journey(web, hosts, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)

    # the session shows who and for how long -- the sign-in's day, not the
    # access token's hours -- without echoing either token
    a = web.get("/api/session").json()
    assert a["state"] == "signed_in" and a["live"] == "ok" and a["source"] == "session"
    assert a["user"] == USER
    assert 23.9 * 3600 < a["seconds_left"] <= 24 * 3600

    # the place resolves, with county, ECCs, bbox and derived fields
    place = web.get("/api/place", params={"q": "Lincoln, NE"}).json()
    assert place["label"] == "Lancaster County, NE (31109) via city 'Lincoln'"
    assert place["eccs"][0]["name"] == "Lincoln Emergency Communications"
    assert place["fields"] == {"account_id": "SAND_31109", "country": "USA", "state": "NE"}
    assert "polygon" not in place

    # preview writes nothing
    shown = preview(web).json()
    assert shown["can_create"]
    assert {c["id"]: c["status"] for c in shown["checks"]} == {"name": "ok", "batch": "ok"}
    assert shown["plan"]["signup"]["agency_name"] == "Lancaster NE"
    assert hosts.andromeda.writes() == [] and hosts.portal.writes() == []
    run_id = shown["run_id"]

    # phase 1: no token needed; the PSAP 500 is explained, not alarming
    assert web.post(f"/api/runs/{run_id}/signup").status_code == 202
    state = wait(web, run_id)
    assert state["status"] == "awaiting_email"
    assert state["signup"]["organization_id"] == "15516"
    notes = [e["message"] for e in state["events"] if e["type"] == "note"]
    assert any("expected" in n for n in notes)

    # the gate: paste the link
    link = "https://sandbox.rapidsosportal.com/confirmation-email/?token=eyJx.eyJ5.sig"
    assert web.post(f"/api/runs/{run_id}/confirm", json={"link": link}).json() == {"confirmed": True}

    # phase 2: only the Andromeda token; the portal is logged into as the account
    r = web.post(f"/api/runs/{run_id}/continue", json={})
    assert r.status_code == 202, r.json()
    assert r.json()["portal_login"]["user"] == "ada+lancaster@rapidsos.com"
    assert "token" not in json.dumps(r.json()).lower().replace("portal_login", "")
    assert hosts.portal.logins[0] == "ada+lancaster@rapidsos.com"
    state = wait(web, run_id)
    assert state["status"] == "done", state["error"]
    result = state["result"]
    assert result["integration"]["consumer_secret"] == "SECRET"
    assert result["jurisdiction"] == {"id": "3799", "status": "Active"}
    assert state["snapshots"] == ["capabilities", "roles"]
    assert len(list((tmp_path / "snapshots").glob("*.json"))) == 2

    # the stream replays everything, steps in order
    with web.stream("GET", f"/api/runs/{run_id}/events") as stream:
        body = "".join(stream.iter_text())
    events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
    done_steps = [e["step"] for e in events if e["type"] == "step" and e["status"] == "done"]
    assert done_steps == ["signup", "account", "account_info", "boundary", "revision",
                          "integration", "capabilities", "roles"]
    assert events[-1] == {"type": "status", "status": "done", "id": events[-1]["id"]}

    # ... and resumes from where a reconnecting page left off
    last = events[-3]["id"]
    with web.stream("GET", f"/api/runs/{run_id}/events",
                    headers={"Last-Event-ID": str(last)}) as stream:
        tail = [json.loads(l[6:]) for l in "".join(stream.iter_text()).splitlines()
                if l.startswith("data: ")]
    assert [e["id"] for e in tail] == [last + 1, last + 2]

    # no token anywhere: state, events, logs, snapshots, the session report
    everything = json.dumps(web.get(f"/api/runs/{run_id}").json()) + body + caplog.text
    everything += "".join(p.read_text() for p in (tmp_path / "snapshots").glob("*"))
    everything += json.dumps(web.get("/api/session").json())
    assert set(hosts.portal_tokens) <= set(hosts.portal.issued)   # roles used the login
    assert set(hosts.andromeda_tokens) == {TOKEN_A}                # minted from the session
    assert "session.json" not in everything
    for token in (TOKEN_A, REFRESH, *hosts.portal.issued):
        assert token not in everything
        assert token.split(".")[1] not in everything


def test_phase_one_needs_no_token(signed_out, hosts):
    web = signed_out
    shown = preview(web).json()                        # not signed in to anything
    assert shown["can_create"]
    assert "Sign in to Andromeda" in shown["checks"][0]["message"]
    assert shown["checks"][0]["status"] == "skipped"
    web.post(f"/api/runs/{shown['run_id']}/signup")
    assert wait(web, shown["run_id"])["status"] == "awaiting_email"
    assert all(not authed for _, authed, _ in hosts.made)


def test_preview_is_what_create_sends(web, hosts):
    run_id = preview(web).json()["run_id"]
    web.post(f"/api/runs/{run_id}/signup")
    wait(web, run_id)
    assert hosts.portal.registered == ["ada+lancaster@rapidsos.com"]
    # the same preview cannot create twice
    assert web.post(f"/api/runs/{run_id}/signup").status_code == 409


# ------------------------------------------------------------------ refusals


def test_a_taken_agency_name_blocks_create(web, hosts):
    hosts.andromeda.authorities = [authority_record(name="Lancaster NE")]
    shown = preview(web).json()
    assert not shown["can_create"]
    assert [c for c in shown["checks"] if c["status"] == "blocked"][0]["id"] == "name"
    assert web.post(f"/api/runs/{shown['run_id']}/signup").status_code == 409
    assert hosts.portal.writes() == []


def test_a_reused_address_is_explained(signed_out):
    # signed out, so Preview cannot see the agency name is taken and it is
    # the portal that refuses the address
    web = signed_out
    signed_up(web)
    run_id = preview(web).json()["run_id"]
    web.post(f"/api/runs/{run_id}/signup")
    state = wait(web, run_id)
    assert state["status"] == "signup_failed"
    assert state["error"]["refused"]
    assert "+tag" in state["error"]["advice"]


def test_missing_fields_are_named(web):
    r = web.post("/api/runs", json={"email": "nope"})
    assert r.status_code == 422
    assert set(r.json()["errors"]) == {"email", "first_name", "last_name", "agency_name",
                                       "boundary", "country", "state"}


def test_continue_needs_the_email_gate(web):
    run_id = signed_up(web)
    r = web.post(f"/api/runs/{run_id}/continue", json={})
    assert r.status_code == 409
    assert r.json()["kind"] == "gate"
    assert "ada+lancaster@rapidsos.com" in r.json()["error"]
    r = web.post(f"/api/runs/{run_id}/continue", json={"clicked_link": True})
    assert r.status_code == 202


def test_continue_needs_only_an_andromeda_sign_in(signed_out, hosts):
    web = signed_out
    run_id = signed_up(web)
    r = web.post(f"/api/runs/{run_id}/continue", json={"clicked_link": True})
    assert r.status_code == 400
    assert r.json()["kind"] == "session" and r.json()["state"] == "signed_out"
    assert "Sign in to Andromeda" in r.json()["error"]
    assert hosts.portal.logins == []                   # nothing tried before the sign-in

    sign_in(web)
    r = web.post(f"/api/runs/{run_id}/continue", json={"clicked_link": True})
    assert r.status_code == 202
    assert wait(web, run_id)["status"] == "done"


def test_a_failed_portal_login_is_explained_before_anything_runs(web, hosts):
    run_id = signed_up(web)
    hosts.portal.registered.clear()                    # the login will be refused
    r = web.post(f"/api/runs/{run_id}/continue", json={"clicked_link": True})
    assert r.status_code == 400
    assert r.json()["kind"] == "login"
    assert "ada+lancaster@rapidsos.com" in r.json()["error"]
    assert "password" in r.json()["error"]
    assert hosts.andromeda.writes() == []
    assert web.get(f"/api/runs/{run_id}").json()["status"] == "awaiting_email"


def test_a_sign_in_that_has_run_out_is_refused_before_anything_runs(web, hosts):
    run_id = signed_up(web)
    hosts.scorpius.revoked.add(REFRESH)
    web.app_state.andromeda_auth.cache.save_refresh_token(REFRESH)   # drops the minted one
    assert web.get("/api/session").json()["state"] == "expired"
    r = web.post(f"/api/runs/{run_id}/continue", json={"clicked_link": True})
    assert r.status_code == 400
    assert r.json()["state"] == "expired" and "Sign in again" in r.json()["error"]
    assert hosts.andromeda.writes() == []


def test_a_rejected_token_fails_in_a_second(web, hosts):
    run_id = signed_up(web)
    hosts.rejected.add(TOKEN_A)
    assert web.get("/api/session").json()["state"] == "expired"
    r = web.post(f"/api/runs/{run_id}/continue", json={"clicked_link": True})
    assert r.status_code == 400
    assert r.json()["kind"] == "session" and "rejected" in r.json()["error"]


def test_an_expired_override_token_is_refused_before_anything_runs(hosts, tmp_path):
    stale = make_token("x", minutes=-5)
    with page_client(make_app(hosts, tmp_path, env={"ANDROMEDA_TOKEN": stale})) as web:
        run_id = signed_up(web)
        r = web.post(f"/api/runs/{run_id}/continue", json={"clicked_link": True})
    assert r.status_code == 400
    assert "ANDROMEDA_TOKEN has expired" in r.json()["error"]
    assert hosts.andromeda.writes() == []


def test_an_override_token_about_to_expire_is_warned_about(hosts, tmp_path):
    soon = make_token("x", minutes=5)
    with page_client(make_app(hosts, tmp_path, env={"ANDROMEDA_TOKEN": soon})) as web:
        run_id = signed_up(web)
        r = web.post(f"/api/runs/{run_id}/continue", json={"clicked_link": True})
        assert r.status_code == 409
        assert r.json()["kind"] == "expiring"
        assert set(r.json()["seconds_left"]) == {"andromeda"}
        r = web.post(f"/api/runs/{run_id}/continue",
                     json={"clicked_link": True, "acknowledge_expiry": True})
        assert r.status_code == 202


def test_one_run_at_a_time(web):
    first = preview(web).json()["run_id"]
    second = preview(web, email="ada+two@rapidsos.com").json()["run_id"]
    runs = web.app_state.runs
    runs.start(runs.get(first))
    try:
        r = web.post(f"/api/runs/{second}/signup")
        assert r.status_code == 409
        assert r.json()["active_run"] == first
    finally:
        runs.finish()


def test_someone_elses_pending_work_is_refused_by_name(web, hosts):
    run_id = signed_up(web)
    hosts.andromeda.authorities.append(authority_record(777, name="Someone Else", org=1))
    hosts.andromeda.batch.append({"id": 11, "authority_id": 777, "ingress_status": 2})

    web.post(f"/api/runs/{run_id}/continue", json={"clicked_link": True})
    state = wait(web, run_id)
    error = state["error"]
    assert state["status"] == "failed"
    assert error["refused"]
    assert "Someone Else (id 777)" in error["message"]
    assert "environment-wide" in error["advice"]
    assert hosts.andromeda.writes() == []


def test_a_partial_failure_says_what_exists_and_can_resume(web, hosts):
    run_id = signed_up(web)
    hosts.andromeda.fail_capabilities = True
    web.post(f"/api/runs/{run_id}/continue", json={"clicked_link": True})
    state = wait(web, run_id)
    error = state["error"]
    assert error["step"] == "capabilities"
    assert not error["refused"]
    assert any("jurisdiction 3799" in e for e in error["exists"])
    assert error["not_done"] == ["Capabilities", "Roles"]
    assert error["partial"]["integration"]["consumer_secret"] == "SECRET"

    hosts.andromeda.fail_capabilities = False
    assert web.post(f"/api/runs/{run_id}/continue", json={}).status_code == 202
    assert wait(web, run_id)["status"] == "done"
    assert hosts.andromeda.writes().count(
        "POST /v1/andromeda/authorities/4958/integrations") == 1


# ----------------------------------------------------------- place and upload


def test_an_ambiguous_place_offers_the_candidates(hosts, tmp_path):
    ambiguous = AmbiguousPlaceError("Lincoln, NE is both", [
        "Lincoln County, NE (31111) -- the county named 'Lincoln'",
        "Lancaster County, NE (31109) -- contains the city of Lincoln"])
    with page_client(make_app(hosts, tmp_path, places=FakePlaces(ambiguous))) as web:
        r = web.get("/api/place", params={"q": "Lincoln, NE"})
    assert r.status_code == 409
    assert [c["geoid"] for c in r.json()["candidates"]] == ["31111", "31109"]


def test_an_unverified_place_is_refused_not_hidden(hosts, tmp_path):
    unverified = FakePlaces(UnusableBoundaryError("31109", "no_entries"))
    with page_client(make_app(hosts, tmp_path, places=unverified)) as web:
        r = web.get("/api/place", params={"q": "31109"})
    assert r.status_code == 422
    assert r.json()["status"] == "no_entries"


def test_a_geojson_upload_is_validated(web):
    good = {"type": "Polygon", "coordinates": [[[-6.3, 53.3], [-6.2, 53.3], [-6.2, 53.4],
                                                [-6.3, 53.3]]]}
    r = web.post("/api/boundary", files={"file": ("dublin.geojson", json.dumps(good))})
    assert r.status_code == 200
    assert r.json()["features"] == 1
    assert r.json()["bbox"] == [-6.3, 53.3, -6.2, 53.4]

    projected = {"type": "Polygon", "coordinates": [[[500000, 4000000], [510000, 4000000],
                                                     [510000, 4010000], [500000, 4000000]]]}
    r = web.post("/api/boundary", files={"file": ("bad.geojson", json.dumps(projected))})
    assert r.status_code == 422
    assert "projected" in r.json()["error"]

    r = web.post("/api/boundary", files={"file": ("x.geojson", "not json")})
    assert r.status_code == 422


# -------------------------------------------------------------- resume, undo


def test_resume_refuses_an_ambiguous_name_then_accepts_an_id(tmp_path):
    mine = authority_record(4958)
    mine["attributes"] = {"contact_email": "ada+old@rapidsos.com"}
    andromeda = Andromeda(authorities=[mine, authority_record(4001, org=9)])
    hosts = Hosts(andromeda)
    hosts.portal.registered.append("ada+old@rapidsos.com")
    with page_client(make_app(hosts, tmp_path)) as web:
        boundary_id = web.get("/api/place", params={"q": "x"}).json()["boundary_id"]
        r = web.post("/api/resume", json={"authority": "Lancaster NE"})
        assert r.status_code == 409
        assert sorted(c["id"] for c in r.json()["candidates"]) == [4001, 4958]

        r = web.post("/api/resume", json={"authority": "4958", "boundary_id": boundary_id})
        assert r.json()["email"] == "ada+old@rapidsos.com"      # from the authority
        run_id = r.json()["run_id"]
        assert web.post(f"/api/runs/{run_id}/continue", json={}).status_code == 202
        state = wait(web, run_id)
    assert state["status"] == "done", state["error"]
    assert state["result"]["authority"]["id"] == "4958"
    assert hosts.portal.logins == ["ada+old@rapidsos.com"]


def test_resume_without_a_contact_email_asks_for_one(tmp_path):
    hosts = Hosts(Andromeda())                         # its authority has no contact email
    hosts.portal.registered.append("ada+typed@rapidsos.com")
    with page_client(make_app(hosts, tmp_path)) as web:
        boundary_id = web.get("/api/place", params={"q": "x"}).json()["boundary_id"]
        run_id = web.post("/api/resume", json={"authority": "4958", "boundary_id": boundary_id}).json()["run_id"]
        r = web.post(f"/api/runs/{run_id}/continue", json={})
        assert r.status_code == 400 and r.json()["kind"] == "login"
        assert "email address" in r.json()["error"]

        r = web.post(f"/api/runs/{run_id}/continue", json={"email": "ada+typed@rapidsos.com"})
        assert r.status_code == 202, r.json()
        assert wait(web, run_id)["status"] == "done"
    assert hosts.portal.logins == ["ada+typed@rapidsos.com"]


def test_roles_can_be_undone_after_a_preview_of_the_undo(web, hosts):
    run_id = signed_up(web)
    web.post(f"/api/runs/{run_id}/continue", json={"clicked_link": True})
    assert wait(web, run_id)["status"] == "done"
    assert hosts.portal.roles[0]["permissions"]

    planned = web.post(f"/api/runs/{run_id}/restore", json={"kind": "roles"}).json()
    assert not planned["applied"]
    assert planned["changes"]
    assert hosts.portal.roles[0]["permissions"]          # still granted

    done = web.post(f"/api/runs/{run_id}/restore", json={"kind": "roles", "apply": True}).json()
    assert done["applied"]
    assert [r["permissions"] for r in hosts.portal.roles] == [[], []]
    # undo logs in again rather than keeping a session between requests
    assert hosts.portal.logins == ["ada+lancaster@rapidsos.com"] * 3


def test_capabilities_can_be_undone(web, hosts):
    run_id = signed_up(web)
    web.post(f"/api/runs/{run_id}/continue", json={"clicked_link": True})
    wait(web, run_id)
    planned = web.post(f"/api/runs/{run_id}/restore", json={"kind": "capabilities"}).json()
    assert len(planned["changes"]) == 2
    done = web.post(f"/api/runs/{run_id}/restore", json={"kind": "capabilities", "apply": True}).json()
    assert done["applied"]
    assert not any(c["authority_enabled"] for c in hosts.andromeda.capabilities)


# ---------------------------------------------------------------- hardening


def test_other_host_names_are_refused(web):
    assert web.get("/api/status", headers={"host": "evil.example"}).status_code == 400


def test_the_page_is_served(web):
    r = web.get("/")
    assert r.status_code == 200
    assert "<title>" in r.text


def test_unknown_run_is_a_clear_404(web):
    r = web.get("/api/runs/nope")
    assert r.status_code == 404
    assert "Resume" in r.json()["error"]


# ------------------------------------------------ copying another account's set


def cap(name, category, on, rsos=None):
    return {"authority_enabled": on, "rsos_enabled": on if rsos is None else rsos,
            "capability_type": {"name": name, "category": category}}


class WithSource(Andromeda):
    """Andromeda plus a source account whose integrations have their own sets."""

    def __init__(self, integrations, **kw):
        super().__init__(**kw)
        self.authorities = [authority_record(900, name="Old Lancaster", org=4)]
        self.source_integrations = integrations       # {id: (app_name, product, caps)}

    def get(self, path):
        base = "/v1/andromeda/authorities/900/integrations"
        if path == base:
            self.calls.append(("GET", path))
            return [{"id": iid, "app_name": name, "product": product}
                    for iid, (name, product, _) in self.source_integrations.items()]
        if path.startswith(base + "/") and path.endswith("/capabilities"):
            self.calls.append(("GET", path))
            iid = int(path.split("/")[6])
            return {"capabilities": copy.deepcopy(self.source_integrations[iid][2])}
        return super().get(path)


# the fake target's catalog is jurisdiction_view(0) and alerts(2); both are
# also in the packaged sandbox set, which Preview plans against
MATCHING = [cap("jurisdiction_view", 0, True), cap("alerts", 2, False)]
PRODUCTION = [cap("jurisdiction_view", 0, True), cap("lyft", 2, True),
              cap("onstar", 2, True), cap("alerts", 2, False)]


@pytest.fixture
def source_hosts():
    return Hosts(WithSource({
        61: ("Old Lancaster Sandbox RSP", "RapidSOS Portal", MATCHING),
        62: ("Old Lancaster Prod copy", "RapidSOS Portal", PRODUCTION),
    }))


@pytest.fixture
def source_web(source_hosts, tmp_path):
    with page_client(make_app(source_hosts, tmp_path)) as client:
        yield client


def sources(web, name="Old Lancaster"):
    return web.get("/api/capability-sources", params={"authority": name})


def test_reading_a_source_needs_a_sign_in_and_says_so(source_web, source_hosts):
    source_web.post("/api/session/sign-out")
    r = sources(source_web)
    assert r.status_code == 400
    assert r.json()["kind"] == "session"
    assert "capabilities needs Andromeda" in r.json()["error"]
    assert source_hosts.andromeda.calls == []


def test_a_source_lists_every_integration_and_picks_none(source_web, source_hosts):
    r = sources(source_web)
    assert r.status_code == 200
    listed = r.json()["integrations"]
    assert [(i["id"], i["enabled"], i["total"]) for i in listed] == [("61", 1, 2), ("62", 3, 4)]
    assert source_hosts.andromeda.writes() == []

    # choosing to copy without choosing an integration is refused, not defaulted
    r = preview(source_web, capabilities_source="copy")
    assert r.status_code == 422
    assert "choose one of its integrations" in r.json()["errors"]["capabilities"]


def test_a_source_name_that_is_ambiguous_offers_the_candidates(source_web, source_hosts):
    source_hosts.andromeda.authorities.append(authority_record(901, name="Old Lancaster", org=5))
    r = sources(source_web)
    assert r.status_code == 409
    assert sorted(c["id"] for c in r.json()["candidates"]) == [900, 901]


def test_a_copied_set_is_previewed_then_applied(source_web, source_hosts):
    capture = sources(source_web).json()["integrations"][0]["capture_id"]
    shown = preview(source_web, capabilities_source="copy",
                    capabilities_capture_id=capture).json()
    caps = shown["plan"]["capabilities"]
    assert caps["source"] == "copy"
    assert caps["from"]["integration"]["app_name"] == "Old Lancaster Sandbox RSP"
    assert caps["unknown"] == [] and not caps["needs_ack"]
    # the diff against the standard set: alerts is off in the source, on in the standard
    assert caps["changed"] == ["alerts(cat 2): authority True->False, rsos True->False"]

    run_id = shown["run_id"]
    assert source_web.post(f"/api/runs/{run_id}/signup").status_code == 202
    assert wait(source_web, run_id)["status"] == "awaiting_email"
    r = source_web.post(f"/api/runs/{run_id}/continue", json={"clicked_link": True})
    assert r.status_code == 202, r.json()
    state = wait(source_web, run_id)
    assert state["status"] == "done", state["error"]
    applied = {c["capability_type"]["name"]: c["authority_enabled"]
               for c in source_hosts.andromeda.capabilities}
    assert applied == {"jurisdiction_view": True, "alerts": False}     # the copy, not the standard
    notes = [e["message"] for e in state["events"] if e["type"] == "note"]
    assert any("copied from Old Lancaster (id 900)" in n for n in notes)


def test_a_mostly_unknown_copy_is_warned_about_and_needs_the_tick(source_web, source_hosts):
    capture = sources(source_web).json()["integrations"][1]["capture_id"]
    shown = preview(source_web, capabilities_source="copy",
                    capabilities_capture_id=capture).json()
    caps = shown["plan"]["capabilities"]
    assert caps["unknown"] == ["lyft(cat 2)", "onstar(cat 2)"]
    assert caps["unknown_share"] == 0.5 and caps["needs_ack"]
    assert "lyft_sandbox" in caps["warning"]                 # explains why
    assert shown["can_create"]                               # a warning, not a block

    run_id = shown["run_id"]
    r = source_web.post(f"/api/runs/{run_id}/signup")
    assert r.status_code == 409 and r.json()["kind"] == "capabilities"
    assert source_hosts.portal.registered == []              # nothing was created
    r = source_web.post(f"/api/runs/{run_id}/signup", json={"acknowledge_capabilities": True})
    assert r.status_code == 202
    assert wait(source_web, run_id)["status"] == "awaiting_email"

    source_web.post(f"/api/runs/{run_id}/continue", json={"clicked_link": True})
    state = wait(source_web, run_id)
    assert state["status"] == "done", state["error"]
    assert state["result"]["capabilities"]["unknown"] == ["lyft(cat 2)", "onstar(cat 2)"]
    assert any("left alone" in n and "lyft(cat 2)" in n for n in state["result"]["notes"])


def test_resume_with_a_copied_set_asks_for_the_tick_at_continue(source_web, source_hosts):
    target = authority_record(4958)
    target["attributes"] = {"contact_email": "ada+old@rapidsos.com"}
    source_hosts.andromeda.authorities.append(target)
    source_hosts.portal.registered.append("ada+old@rapidsos.com")
    capture = sources(source_web).json()["integrations"][1]["capture_id"]
    boundary_id = source_web.get("/api/place", params={"q": "x"}).json()["boundary_id"]

    r = source_web.post("/api/resume", json={"authority": "4958", "boundary_id": boundary_id,
                                             "capabilities_capture_id": capture})
    assert r.status_code == 200
    assert r.json()["capabilities"]["needs_ack"]
    run_id = r.json()["run_id"]

    r = source_web.post(f"/api/runs/{run_id}/continue", json={})
    assert r.status_code == 409 and r.json()["kind"] == "capabilities"
    assert source_hosts.andromeda.writes() == []
    r = source_web.post(f"/api/runs/{run_id}/continue",
                        json={"acknowledge_capabilities": True})
    assert r.status_code == 202
    assert wait(source_web, run_id)["status"] == "done"


def test_an_unknown_capture_is_refused_not_replaced_by_the_default(source_web):
    r = preview(source_web, capabilities_source="copy",
                capabilities_capture_id="nope")
    assert r.status_code == 422
    assert "capabilities" in r.json()["errors"]


# ---------------------------------------------- copying another account's boundary


SOURCE_POLYGON = {"type": "FeatureCollection", "features": [{
    "type": "Feature", "properties": {"name": "Old Lancaster"},
    "geometry": {"type": "Polygon", "coordinates": [[
        [-97.0, 40.0], [-96.0, 40.0], [-96.0, 41.0], [-97.0, 41.0], [-97.0, 40.0]]]}}]}


def jurisdiction(jid, status, polygon=SOURCE_POLYGON, listed_with_geometry=True):
    record = {"id": jid, "authority_id": 900, "ingress_status": status,
              "egress_status": 3, "shapes": [{}], "exact_polygon": polygon}
    return record, listed_with_geometry


class WithBoundarySource(Andromeda):
    """Andromeda plus a source account 900 with its own jurisdictions."""

    def __init__(self, source_jurisdictions, **kw):
        super().__init__(**kw)
        source = authority_record(900, name="Old Lancaster", org=4)
        source["attributes"] = {"country": "USA", "state": "NE"}
        self.authorities = [source]
        self.source = source_jurisdictions          # [(record, listed_with_geometry)]
        self.posted_boundaries = []

    def get(self, path):
        base = "/v1/andromeda/authorities/900/jurisdictions"
        if path == base:
            self.calls.append(("GET", path))
            return [dict(r) if full else {k: v for k, v in r.items() if k != "exact_polygon"}
                    for r, full in self.source]
        if path.startswith(base + "/"):
            self.calls.append(("GET", path))
            jid = int(path.rsplit("/", 1)[1])
            return copy.deepcopy(next(r for r, _ in self.source if r["id"] == jid))
        return super().get(path)

    def post(self, path, json=None):
        if path.endswith("/jurisdictions"):
            self.posted_boundaries.append(copy.deepcopy(json["exact_polygon"]))
        return super().post(path, json)


def boundary_web(tmp_path, *source, signed_in=True):
    hosts = Hosts(WithBoundarySource(list(source)))
    return hosts, page_client(make_app(hosts, tmp_path, signed_in=signed_in))


def boundary_sources(web, name="Old Lancaster"):
    return web.get("/api/boundary-sources", params={"authority": name})


def test_reading_a_source_boundary_needs_a_sign_in_and_says_so(tmp_path):
    hosts, web = boundary_web(tmp_path, jurisdiction(3799, 3), signed_in=False)
    with web:
        r = boundary_sources(web)
    assert r.status_code == 400 and r.json()["kind"] == "session"
    assert "Sign in to Andromeda" in r.json()["error"]
    assert hosts.andromeda.calls == []


def test_the_one_active_boundary_is_copied_and_the_run_posts_it_unchanged(tmp_path):
    # a pending one alongside is never offered
    hosts, web = boundary_web(tmp_path, jurisdiction(3799, 3, listed_with_geometry=False),
                              jurisdiction(3812, 2))
    with web:
        r = boundary_sources(web)
        assert r.status_code == 200, r.json()
        b = r.json()["boundary"]
        assert b["source"] == "account"
        assert b["from"]["jurisdiction"] == {"id": "3799", "status": "Active", "shapes": 1}
        assert b["features"] == 1 and b["bbox"] == [-97.0, 40.0, -96.0, 41.0]
        assert b["bytes"] == len(json.dumps(SOURCE_POLYGON))
        assert b["fields"] == {"country": "USA", "state": "NE"}     # filled in, not account_id
        assert r.json()["not_offered"] == [{"id": "3812", "status": "Pending", "shapes": 1}]
        assert hosts.andromeda.writes() == []

        # from here the run is the same as for a place or a file
        body = {**FORM, "boundary_id": b["boundary_id"]}
        shown = web.post("/api/runs", json=body).json()
        assert shown["plan"]["boundary"]["features"] == 1
        run_id = shown["run_id"]
        web.post(f"/api/runs/{run_id}/signup")
        assert wait(web, run_id)["status"] == "awaiting_email"
        web.post(f"/api/runs/{run_id}/continue", json={"clicked_link": True})
        state = wait(web, run_id)
    assert state["status"] == "done", state["error"]
    assert hosts.andromeda.posted_boundaries == [SOURCE_POLYGON]


def test_only_pending_jurisdictions_are_refused(tmp_path):
    hosts, web = boundary_web(tmp_path, jurisdiction(3812, 2), jurisdiction(3813, 1))
    with web:
        r = boundary_sources(web)
    assert r.status_code == 409 and r.json()["kind"] == "not_active"
    assert "3812 (Pending)" in r.json()["message"] and "3813 (Verified)" in r.json()["message"]
    assert "boundary" not in r.json()


def test_an_account_without_a_jurisdiction_says_so(tmp_path):
    _, web = boundary_web(tmp_path)
    with web:
        r = boundary_sources(web)
    assert r.status_code == 404 and r.json()["kind"] == "no_jurisdiction"
    assert "no boundary to copy" in r.json()["message"]


def test_several_active_boundaries_are_a_choice_and_pending_is_refused(tmp_path):
    other = {"type": "Polygon", "coordinates": [[[-95.0, 39.0], [-94.0, 39.0], [-94.0, 40.0],
                                                 [-95.0, 39.0]]]}
    hosts, web = boundary_web(tmp_path, jurisdiction(3799, 3), jurisdiction(3800, 3, other),
                              jurisdiction(3812, 2))
    with web:
        r = boundary_sources(web)
        assert r.status_code == 200
        assert "boundary" not in r.json()                       # nothing chosen for them
        assert [j["id"] for j in r.json()["jurisdictions"]] == ["3799", "3800"]

        pick = web.post("/api/boundary/from-authority",
                        json={"authority_id": "900", "jurisdiction_id": "3800"})
        assert pick.status_code == 200
        assert pick.json()["boundary"]["bbox"] == [-95.0, 39.0, -94.0, 40.0]

        pending = web.post("/api/boundary/from-authority",
                           json={"authority_id": "900", "jurisdiction_id": "3812"})
        assert pending.status_code == 409 and pending.json()["kind"] == "not_active"


def test_an_unusable_stored_boundary_is_refused_not_repaired(tmp_path):
    projected = {"type": "Polygon", "coordinates": [[[500000, 4000000], [510000, 4000000],
                                                     [510000, 4010000], [500000, 4000000]]]}
    _, web = boundary_web(tmp_path, jurisdiction(3799, 3, projected))
    with web:
        r = boundary_sources(web)
    assert r.status_code == 422 and r.json()["kind"] == "unusable"


# ------------------------------------------------------------------ sign-in


def sign_in(web):
    """Press Sign in and wait for it; returns the job's events."""
    r = web.post("/api/session/sign-in")
    assert r.status_code == 202, r.json()
    job_id = r.json()["job_id"]
    with web.stream("GET", f"/api/session/sign-in/{job_id}/events") as stream:
        body = "".join(stream.iter_text())
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


def har(refresh=REFRESH):
    exchange = {"request": {"method": "POST", "url": SCORPIUS_BASE + OIDC_PATH},
                "response": {"content": {"text": json.dumps(
                    {"token": TOKEN_A, "refresh_token": refresh})}}}
    return json.dumps({"log": {"entries": [exchange]}})


@pytest.fixture
def temp_files(monkeypatch):
    """Every temporary file the HAR fallback writes, to check it is gone."""
    made = []
    real = session_module.tempfile.NamedTemporaryFile

    def recording(*a, **kw):
        handle = real(*a, **kw)
        made.append(Path(handle.name))
        return handle

    monkeypatch.setattr(session_module.tempfile, "NamedTemporaryFile", recording)
    return made


def test_signing_in_streams_its_progress_and_stores_the_session(hosts, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    browser = FakeBrowser()
    with page_client(make_app(hosts, tmp_path, signed_in=False, browser=browser)) as web:
        before = web.get("/api/session").json()
        assert before["state"] == "signed_out" and before["sign_in"]["available"]

        events = sign_in(web)
        notes = [e["message"] for e in events if e["type"] == "note"]
        assert notes == ["reusing the saved sign-in", "signed in"]
        assert events[-1]["status"] == "signed_in"

        after = web.get("/api/session").json()
        assert after["state"] == "signed_in" and after["live"] == "ok"
        assert after["user"] == USER and after["sign_in"]["running"] is None
        everything = json.dumps(events) + json.dumps(after) + caplog.text

    assert browser.calls[0].name == ".andromeda-browser"
    assert TokenCache(tmp_path / "session.json").refresh_token == REFRESH
    for token in (TOKEN_A, REFRESH):
        assert token.split(".")[1] not in everything


def test_one_sign_in_at_a_time(hosts, tmp_path):
    hold = threading.Event()
    with page_client(make_app(hosts, tmp_path, signed_in=False,
                              browser=FakeBrowser(hold=hold))) as web:
        first = web.post("/api/session/sign-in").json()["job_id"]
        second = web.post("/api/session/sign-in")
        assert second.status_code == 409 and second.json()["job_id"] == first
        assert web.get("/api/session").json()["sign_in"]["running"] == first
        hold.set()
        with web.stream("GET", f"/api/session/sign-in/{first}/events") as stream:
            "".join(stream.iter_text())
        assert web.get("/api/session").json()["state"] == "signed_in"


def test_without_playwright_the_page_is_told_how_to_install_it(hosts, tmp_path):
    with page_client(make_app(hosts, tmp_path, signed_in=False,
                              can_open_browser=False)) as web:
        status = web.get("/api/session").json()
        assert not status["sign_in"]["available"]
        assert "playwright install chromium" in status["sign_in"]["install"]
        r = web.post("/api/session/sign-in")
    assert r.status_code == 409 and r.json()["kind"] == "playwright_missing"
    assert "pip install playwright" in r.json()["error"]


def test_playwright_missing_at_sign_in_is_explained_not_raised(hosts, tmp_path):
    browser = FakeBrowser(fail=PlaywrightMissing())
    with page_client(make_app(hosts, tmp_path, signed_in=False, browser=browser)) as web:
        events = sign_in(web)
        assert web.get("/api/session").json()["state"] == "signed_out"
    failed = [e for e in events if e["type"] == "failed"][0]
    assert failed["kind"] == "playwright_missing"
    assert events[-1]["status"] == "sign_in_failed"


def test_a_closed_window_says_what_to_do_and_leaks_nothing(hosts, tmp_path):
    closed = RuntimeError("Target closed: https://accounts.google.com/?code=SECRETCODE")
    with page_client(make_app(hosts, tmp_path, signed_in=False,
                              browser=FakeBrowser(fail=closed))) as web:
        events = sign_in(web)
    failed = [e for e in events if e["type"] == "failed"][0]
    assert failed["kind"] == "stopped" and "press Sign in again" in failed["message"]
    assert "SECRETCODE" not in json.dumps(events)


def test_a_saved_capture_starts_a_session_and_is_not_kept(hosts, tmp_path, temp_files):
    with page_client(make_app(hosts, tmp_path, signed_in=False,
                              can_open_browser=False)) as web:
        r = web.post("/api/session/har", files={"file": ("login.har", har())})
        assert r.status_code == 200, r.json()
        assert r.json()["state"] == "signed_in" and r.json()["user"] == USER
    assert temp_files and not any(p.exists() for p in temp_files)
    assert REFRESH.split(".")[1] not in r.text


def test_a_capture_that_does_not_parse_is_still_removed(hosts, tmp_path, temp_files):
    with page_client(make_app(hosts, tmp_path, signed_in=False)) as web:
        r = web.post("/api/session/har", files={"file": ("login.har", "not json")})
        assert r.status_code == 422 and "not a HAR" in r.json()["error"]
        r = web.post("/api/session/har", files={"file": ("empty.har", '{"log": {"entries": []}}')})
        assert r.status_code == 422 and "no Google sign-in" in r.json()["error"]
        assert str(temp_files[-1]) not in r.json()["error"]     # no temp path in the message
    assert len(temp_files) == 2 and not any(p.exists() for p in temp_files)


def test_a_stale_capture_does_not_replace_a_working_session(web, hosts, temp_files):
    stale = make_token(USER, minutes=24 * 60, type="refresh", jti="old")
    hosts.scorpius.revoked.add(stale)
    r = web.post("/api/session/har", files={"file": ("old.har", har(stale))})
    assert r.status_code == 422 and "run out" in r.json()["error"]
    assert web.app_state.andromeda_auth.cache.refresh_token == REFRESH
    assert not any(p.exists() for p in temp_files)


def test_signing_out_forgets_the_session(web, tmp_path):
    assert web.post("/api/session/sign-out").json()["state"] == "signed_out"
    assert not (tmp_path / "session.json").exists()


def test_the_override_token_wins_and_sign_in_is_off(hosts, tmp_path):
    override = make_token("paster@rapidsos.com", minutes=90)
    with page_client(make_app(hosts, tmp_path, env={"ANDROMEDA_TOKEN": override})) as web:
        status = web.get("/api/session").json()
        assert status["source"] == "env" and status["user"] == "paster@rapidsos.com"
        assert not status["sign_in"]["available"]
        assert web.post("/api/session/sign-in").json()["kind"] == "env"
        assert web.post("/api/session/har", files={"file": ("x.har", har())}).status_code == 409
        run_id = signed_up(web)
        assert web.post(f"/api/runs/{run_id}/continue",
                        json={"clicked_link": True}).status_code == 202
        assert wait(web, run_id)["status"] == "done"
    assert set(hosts.andromeda_tokens) == {override}
    assert hosts.scorpius.refreshes == 0


def test_writes_from_another_site_are_refused(hosts, tmp_path):
    app = make_app(hosts, tmp_path, signed_in=False)
    with TestClient(app) as bare:                              # no page header
        assert bare.post("/api/session/sign-in").status_code == 403
        assert bare.post("/api/session/sign-out").status_code == 403
        assert bare.get("/api/session").status_code == 200     # reads are fine
    with page_client(app) as web:
        r = web.post("/api/session/sign-out", headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        r = web.post("/api/session/sign-out", headers={"Origin": "http://testserver"})
        assert r.status_code == 200
