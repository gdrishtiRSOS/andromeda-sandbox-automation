"""Every smoke_test.py stage, run as a dry run against a fake server.

The fake sits under the HTTP client, at `requests.Session.request`, so these
tests hold whichever client smoke_test.py builds. Each stage is checked for
two things: it reached its own handler (a stage name shadowed by an earlier
branch once did nothing, silently), and it sent no writes.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests

import smoke_test

ROOT = Path(__file__).resolve().parents[1]
STANDARD = str(ROOT / "src" / "logic" / "data" / "standard_capabilities.json")
GEOJSON = str(ROOT / "src" / "logic" / "data" / "authority-4958.geojson")

ANDROMEDA = "andromeda.sandbox.rapidsos.com"
PORTAL = "api-sandbox.rapidsosportal.com"
SCORPIUS = "api-sandbox.rapidsos.com"


def make_token(**claims) -> str:
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    claims.setdefault("exp", 4102444800)          # 2100-01-01
    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


TOKEN = make_token(username="ada@rapidsos.com", sub=7)
PORTAL_TOKEN = make_token(username="ada+portal@rapidsos.com", sub=8)

POLYGON = {
    "type": "FeatureCollection",
    "features": [{
        "type": "Feature",
        "properties": {"name": "Dublin"},
        "geometry": {"type": "Polygon", "coordinates": [[
            [-6.3, 53.3], [-6.2, 53.3], [-6.2, 53.4], [-6.3, 53.4], [-6.3, 53.3]
        ]]},
    }],
}

AUTHORITY = {
    "id": 4958, "name": "gDTest", "display_name": "gDTest",
    "account_id": None, "dispatch_type": 1, "organization_id": 15516,
    "attributes": {"country": None, "state": None},
}

CAPABILITIES = [
    {"authority_enabled": False, "rsos_enabled": False,
     "capability_type": {"name": "jurisdiction_view", "category": 0,
                         "display_name": "Jurisdiction View"}},
    {"authority_enabled": True, "rsos_enabled": True,
     "capability_type": {"name": "alerts", "category": 2,
                         "display_name": "Alerts ADR"}},
]

PERMISSIONS = [
    {"name": "alerts", "display_name": "Alerts", "rsp_rbac": False,
     "whitelisting_required": False},
    {"name": "MANAGE_USERS", "display_name": "Users & Roles", "rsp_rbac": True,
     "whitelisting_required": False},
]

ROLES = [
    {"id": 16944, "name": "Admin", "permissions": ["alerts"],
     "application": "capstone", "avatar_bg_color": "#ff8b00"},
    {"id": 16945, "name": "Agent", "permissions": [],
     "application": "capstone", "avatar_bg_color": "#ff8b00"},
]


def pending(authority_ids=(4958,)):
    return {
        "id": "1986", "revision_number": None, "revision_date": None,
        "created": [], "deleted": [],
        "modified": [{"authority_id": a, "id": 3799 + i, "ingress_status": 2,
                      "egress_status": 3, "shapes": [{"id": 1}]}
                     for i, a in enumerate(authority_ids)],
    }


def routes():
    """(method, host, path) -> response body. The query string is ignored."""
    a = "/v1/andromeda"
    caps = f"{a}/authorities/4958/integrations/5223/capabilities"
    org = "/v1/scorpius/organizations/15516/capstone"
    return {
        ("GET", ANDROMEDA, f"{a}/authorities"):
            {"authorities": [AUTHORITY], "total_records": 1},
        ("GET", ANDROMEDA, f"{a}/authorities/4958"): AUTHORITY,
        ("GET", ANDROMEDA, f"{a}/authorities/4958/integrations"): [],
        ("GET", ANDROMEDA, f"{a}/authorities/4958/jurisdictions"): [{
            "id": 3799, "authority_id": 4958, "ingress_status": 3,
            "egress_status": 3, "shapes": [], "exact_polygon": POLYGON}],
        ("GET", ANDROMEDA, f"{a}/integration-types"):
            {"integration_types": [{
                "id": 6, "name": "RapidSOS Portal",
                "apigee_product_name": "Capstone-Pre-Production",
                "capability_types": []}]},
        ("GET", ANDROMEDA, caps): {"capabilities": CAPABILITIES},
        ("GET", ANDROMEDA, f"{a}/revisions/pending"): pending(),
        ("GET", ANDROMEDA, f"{a}/country"):
            [{"code": "USA", "name": "United States"}],
        ("GET", ANDROMEDA, f"{a}/country/USA"): [{"code": "TX", "name": "Texas"}],
        ("GET", PORTAL, f"{org}/permissions"): PERMISSIONS,
        ("GET", PORTAL, f"{org}/roles"): ROLES,
        ("POST", SCORPIUS, "/v1/scorpius/user/api-token-auth/refresh"):
            {"token": TOKEN},
    }


class FakeServer:
    """Answers `requests.Session.request` from a route table; records calls."""

    def __init__(self):
        self.routes = routes()
        self.calls = []

    def request(self, session, method, url, **kw):
        parts = urlsplit(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        self.calls.append((method, parts.hostname, path,
                           dict(session.headers), kw.get("json")))
        key = (method, parts.hostname, parts.path)
        response = requests.Response()
        response.url = url
        if key in self.routes:
            response.status_code = 200
            response._content = json.dumps(self.routes[key]).encode()
        else:
            response.status_code = 404
            response._content = f"no fake route for {method} {url}".encode()
        return response

    def writes(self):
        """Every call that is not a read. Minting a token is a POST but
        changes nothing, so it does not count."""
        return [(m, h, p) for m, h, p, _, _ in self.calls
                if m != "GET" and not p.endswith("/api-token-auth/refresh")]


@pytest.fixture
def server(monkeypatch):
    fake = FakeServer()
    monkeypatch.setattr(requests.Session, "request",
                        lambda self, method, url, **kw: fake.request(self, method, url, **kw))
    return fake


@pytest.fixture
def cli(server, monkeypatch, capsys, tmp_path):
    """Run smoke_test.main() with argv; return (exit code, stdout, stderr)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANDROMEDA_TOKEN", TOKEN)
    monkeypatch.delenv("ANDROMEDA_COOKIE", raising=False)
    monkeypatch.setenv("RAPIDSOS_PORTAL_TOKEN", PORTAL_TOKEN)

    def run(*argv):
        base = ["smoke_test.py", "--session-file", str(tmp_path / "session.json"),
                "--snapshot-dir", str(tmp_path / "snapshots"), "--standard", STANDARD]
        monkeypatch.setattr("sys.argv", base + list(argv))
        try:
            code = smoke_test.main()
        except SystemExit as exc:
            if isinstance(exc.code, str):
                print(exc.code, file=__import__("sys").stderr)
                code = 1
            else:
                code = exc.code
        out, err = capsys.readouterr()
        return code, out, err

    return run


def fake_place():
    from logic.places import ResolvedPlace
    return ResolvedPlace(
        query="48477", geoid="48477", county_name="Washington", state="TX",
        matched_as="geoid", matched_name="48477",
        result={
            "county": {"name": "Washington", "state": "TX", "geoid": "48477",
                       "boundary_type": "county_footprint"},
            "geometry": {"type": "MultiPolygon", "coordinates": [[[
                [-96.79, 30.04], [-96.08, 30.04], [-96.08, 30.39],
                [-96.79, 30.39], [-96.79, 30.04]]]]},
            "jurisdiction_scope": {"status": "ok", "eccs": [
                {"name": "Washington County 9-1-1", "fcc_psap_id": "6452"}]},
            "sources": [], "disclaimer": "not for routing",
        },
    )


@pytest.fixture
def place(monkeypatch):
    import logic.places as places_mod
    seen = []

    def resolve(query, **kw):
        seen.append(query)
        return fake_place()

    monkeypatch.setattr(places_mod, "resolve_place", resolve)
    return seen


# ------------------------------------------------------ every stage, dry


DRY_RUNS = {
    # stage: (argv, the stage's own output -- proof its handler ran)
    "read": (["--authority-id", "4958", "--integration-id", "5223"],
             ["read OK: 2 capabilities, 1 currently enabled", "snapshot:"]),
    "plan": (["--authority-id", "4958", "--integration-id", "5223", "--stage", "plan"],
             ["live catalog :", "[plan] nothing sent. Body we would PATCH:"]),
    "verify": (["--authority-id", "4958", "--integration-id", "5223", "--stage", "verify"],
               ["DOES NOT match"]),
    "create": (["--authority-id", "4958", "--stage", "create"],
               ["[dry-run] would create", "on authority 4958"]),
    "scratch": (["--authority-id", "4958", "--stage", "scratch"],
                ["[dry-run] would create", "on authority 4958"]),
    "pending": (["--authority-id", "4958", "--stage", "pending"],
                ["pending revision : 1986", "<-- yours",
                 "[pending] read-only. Activating would stamp this"]),
    "activate": (["--authority-id", "4958", "--stage", "activate"],
                 ["pending revision : 1986", "[dry-run] would publish revision 1986"]),
    "jurisdiction": (["--authority-id", "4958", "--stage", "jurisdiction",
                      "--geojson", GEOJSON],
                     ["existing   : 1 jurisdiction(s) on authority 4958",
                      "[dry-run] would create the jurisdiction as Verified"]),
    "provision": (["--authority-id", "4958", "--stage", "provision", "--geojson", GEOJSON],
                  ["boundary   :", "product    :", "[dry-run] nothing sent."]),
    "account-info": (["--authority-id", "4958", "--stage", "account-info",
                      "--country", "USA", "--state", "TX"],
                     ["authority   : gDTest (id 4958)", "dispatch   : will be set to",
                      "[dry-run] nothing sent."]),
    "catalogs": (["--authority-id", "4958", "--stage", "catalogs", "--country", "USA"],
                 ["countries (1):", "states/regions in USA (1):"]),
    "export-boundary": (["--authority-id", "4958", "--stage", "export-boundary",
                         "--out", "out.geojson"],
                        ["features   : 1", "written to : out.geojson"]),
    "roles": (["--authority-id", "4958", "--stage", "roles"],
              ["authority 4958 -> organization 15516", "catalog   : 2 permission(s)",
               "[dry-run] nothing sent."]),
    "signup": (["--stage", "signup", "--email", "ada+t@rapidsos.com",
                "--agency-name", "Ada PD", "--first-name", "Ada", "--last-name", "L",
                "--password", "pw"],
               ["auth      : none", "agency    : Ada PD", "[dry-run] nothing sent."]),
    "confirm": (["--stage", "confirm", "--confirm-token", TOKEN],
                ["token     : ...", "[dry-run] nothing sent."]),
}


@pytest.mark.parametrize("stage", sorted(DRY_RUNS))
def test_every_stage_reaches_its_own_handler_and_writes_nothing(cli, server, stage):
    argv, expected = DRY_RUNS[stage]
    code, out, err = cli(*argv)
    for line in expected:
        assert line in out, f"--stage {stage}: {line!r} missing from\n{out}{err}"
    assert server.writes() == []
    assert code == (1 if stage == "verify" else 0), out + err


def test_every_stage_is_covered():
    """A stage added to argparse without a dry-run test fails here."""
    stages = next(a for a in smoke_test_parser_actions() if a.dest == "stage").choices
    covered = set(DRY_RUNS) | {"place", "sandbox", "session", "apply"}
    assert set(stages) == covered


def smoke_test_parser_actions():
    import argparse
    captured = {}
    real = argparse.ArgumentParser.parse_args

    def grab(self, *a, **kw):
        captured["parser"] = self
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = grab
    try:
        with pytest.raises(SystemExit):
            smoke_test.main()
    finally:
        argparse.ArgumentParser.parse_args = real
    return captured["parser"]._actions


def test_authority_name_is_resolved_to_its_id(cli, server):
    code, out, _ = cli("--authority-id", "gDTest", "--stage", "pending")
    assert code == 0
    assert "authority : 'gDTest' -> id 4958" in out


def test_apply_stage_refuses_without_apply(cli, server):
    code, _, err = cli("--authority-id", "4958", "--integration-id", "5223",
                       "--stage", "apply")
    assert code == 1
    assert "--stage apply also needs --apply" in err
    assert server.writes() == []


def test_restore_is_a_dry_run_without_apply(cli, server, tmp_path):
    snap = tmp_path / "before.json"
    snap.write_text(json.dumps({"capabilities": CAPABILITIES}))
    code, out, _ = cli("--authority-id", "4958", "--integration-id", "5223",
                       "--restore", str(snap))
    assert code == 0
    assert "[dry-run] would restore 2 capabilities" in out
    assert server.writes() == []


def test_activate_names_other_authorities_in_the_batch(cli, server):
    server.routes[("GET", ANDROMEDA, "/v1/andromeda/revisions/pending")] = \
        pending(authority_ids=(4958, 5001))
    code, out, _ = cli("--authority-id", "4958", "--stage", "activate")
    assert "the batch also holds changes for 5001" in out
    assert code == 1
    assert "OtherAuthoritiesPendingError" in out
    assert server.writes() == []


def test_bearer_token_is_sent_to_andromeda(cli, server):
    cli("--authority-id", "4958", "--stage", "pending")
    headers = [h for _, host, _, h, _ in server.calls if host == ANDROMEDA]
    assert headers and all(h["Authorization"] == f"Bearer {TOKEN}" for h in headers)
    assert all(h["x-rapidsos-org"] == "RapidSOS Admin" for h in headers)


def test_roles_uses_the_portal_token_on_the_portal(cli, server):
    cli("--authority-id", "4958", "--stage", "roles")
    sent = {host: h.get("Authorization") for _, host, _, h, _ in server.calls}
    assert sent[PORTAL] == f"Bearer {PORTAL_TOKEN}"
    assert sent[ANDROMEDA] == f"Bearer {TOKEN}"


def test_roles_without_a_portal_credential_says_how_to_get_one(cli, server, monkeypatch):
    monkeypatch.delenv("RAPIDSOS_PORTAL_TOKEN")
    code, _, err = cli("--authority-id", "4958", "--stage", "roles")
    assert code == 1
    assert "--stage roles needs a portal credential" in err


# --------------------------------------------------- place and sandbox


def test_place_is_lookup_only(cli, server, place):
    code, out, _ = cli("--authority-id", "4958", "--stage", "place", "--place", "48477")
    assert code == 0
    assert place == ["48477"]
    assert "[place] lookup only. Use --stage sandbox to provision." in out
    assert server.writes() == []


def test_sandbox_resolves_the_place_and_dry_runs_the_whole_account(cli, server, place):
    code, out, err = cli("--authority-id", "4958", "--stage", "sandbox",
                         "--place", "48477")
    assert code == 0, out + err
    # the lookup it shares with --stage place ...
    assert "resolved   :" in out
    assert "'SAND_48477'" in out
    # ... and then its own work: create_sandbox_account, dry
    assert "[place] lookup only" not in out
    assert "[dry-run] nothing sent. Re-run with --apply." in out
    assert place == ["48477", "48477"]          # once to show, once to provision
    assert ("GET", ANDROMEDA, "/v1/andromeda/authorities/4958") in \
        [(m, h, p) for m, h, p, _, _ in server.calls]
    assert server.writes() == []


def test_sandbox_needs_a_place(cli, server):
    code, _, err = cli("--authority-id", "4958", "--stage", "sandbox")
    assert code == 1
    assert "--stage sandbox needs --place" in err


# -------------------------------------------------------------- session


def test_session_sign_in_stores_the_refresh_token(cli, server, monkeypatch, tmp_path):
    import logic.browser_auth as browser_auth
    seen = {}

    def sign_in(**kw):
        seen.update(kw)
        kw["on_status"]("browser opened")
        return browser_auth.SignInResult(token=TOKEN, refresh_token=make_token(sub=7),
                                         interactive=False)

    monkeypatch.setattr(browser_auth, "sign_in", sign_in)
    code, out, err = cli("--stage", "session", "--sign-in")

    assert code == 0, out + err
    assert seen["headless"] is None
    assert "  browser opened" in out
    assert "stored the session" in out
    assert "signed in : ada@rapidsos.com" in out
    stored = json.loads((tmp_path / "session.json").read_text())
    assert stored["refresh_token"] == make_token(sub=7)
    # the token was minted, and nothing else was sent
    assert [(m, p) for m, _, p, _, _ in server.calls] == \
        [("POST", "/v1/scorpius/user/api-token-auth/refresh")]


def test_session_sign_in_failure_is_reported(cli, server, monkeypatch):
    import logic.browser_auth as browser_auth

    def sign_in(**kw):
        raise browser_auth.BrowserAuthError("window closed")

    monkeypatch.setattr(browser_auth, "sign_in", sign_in)
    code, out, _ = cli("--stage", "session", "--sign-in")
    assert code == 1
    assert "sign-in failed: window closed" in out


def test_session_without_a_stored_token_explains_how(cli, server):
    code, out, _ = cli("--stage", "session")
    assert code == 1
    assert "No session stored." in out
    assert server.calls == []


# --------------------------------------------------------------- errors


def test_expired_token_exits_with_advice(cli, server):
    def unauthorised(session, method, url, **kw):
        response = requests.Response()
        response.status_code = 401
        response._content = b"token expired"
        return response

    server.request = unauthorised
    code, _, err = cli("--authority-id", "4958", "--stage", "pending")
    assert code == 1
    assert "401 on GET /v1/andromeda/revisions/pending" in err
    assert "Authorization header sent: yes" in err
    assert "Most likely the token expired." in err


def test_api_error_is_reported_not_raised(cli, server):
    del server.routes[("GET", ANDROMEDA, "/v1/andromeda/revisions/pending")]
    code, out, _ = cli("--authority-id", "4958", "--stage", "pending")
    assert code == 1
    assert "API ERROR: 404 on GET /v1/andromeda/revisions/pending" in out
