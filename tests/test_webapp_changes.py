"""Tests for the page's "Work on an existing account" routes.

The workflow has its own tests (test_account_changes.py); these check that the
routes show the account, preview without writing, insist on the second
confirmation, and stream the apply -- with no token in anything they return.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from test_account_changes import ACTIVE, OLD_INTEGRATION, OTHER_WORK  # noqa: E402
from test_configure_account import Andromeda, authority_record  # noqa: E402
from test_webapp import TOKEN_A, Hosts, make_app, page_client, wait  # noqa: E402

EMAIL = "ada+lancaster@rapidsos.com"


def working_hosts(batch=()):
    record = authority_record(account_id="NE_LANCA")
    record["attributes"] = {"contact_email": EMAIL}
    other = authority_record(aid=1200, name="Someone Else", org=999)
    andromeda = Andromeda(authorities=[record, other], jurisdictions=[ACTIVE],
                          integrations=[OLD_INTEGRATION], batch=batch)
    hosts = Hosts(andromeda)
    hosts.portal.registered.append(EMAIL)       # made by this tool: the shared password works
    return hosts


@pytest.fixture
def hosts():
    return working_hosts()


@pytest.fixture
def web(hosts, tmp_path):
    with page_client(make_app(hosts, tmp_path)) as client:
        yield client


def preview(web, **body):
    return web.post("/api/accounts/4958/changes", json=body)


def test_the_overview_shows_what_the_account_has(web, hosts):
    shown = web.get("/api/accounts", params={"authority": "Lancaster NE"}).json()
    assert shown["authority"]["id"] == "4958"
    assert shown["account_id_locked"] and shown["fields"]["account_id"] == "NE_LANCA"
    assert shown["email"] == EMAIL
    assert shown["jurisdictions"] == [{"id": "3001", "status": "Active", "shapes": 0}]
    assert shown["integrations"][0] == {"id": "88", "app_name": OLD_INTEGRATION["app_name"],
                                        "product": "RapidSOS Portal", "total": 2,
                                        "enabled": 0, "rsos_enabled": 0}
    assert hosts.andromeda.writes() == []

    roles = web.get("/api/accounts/4958/roles").json()
    assert [r["name"] for r in roles["roles"]] == ["Admin", "Agent"]
    assert hosts.portal.logins == [EMAIL]


def test_roles_for_an_account_made_elsewhere_are_explained_not_raised(web):
    r = web.get("/api/accounts/4958/roles", params={"email": "someone@else.com"})
    assert r.status_code == 400 and r.json()["kind"] == "login"
    assert "shared sandbox password" in r.json()["error"]

    shown = preview(web, email="someone@else.com", roles=True).json()
    assert not shown["can_apply"]
    assert "refused to log in" in shown["plan"]["sections"][0]["refusal"]


def test_an_ambiguous_name_offers_the_candidates(tmp_path):
    hosts = working_hosts()
    hosts.andromeda.authorities.append(authority_record(aid=5000, org=777))
    with page_client(make_app(hosts, tmp_path)) as web:
        r = web.get("/api/accounts", params={"authority": "Lancaster NE"})
        assert r.status_code == 409
        assert {c["id"] for c in r.json()["candidates"]} == {4958, 5000}


def test_preview_writes_nothing_and_apply_needs_the_confirmation(web, hosts):
    shown = preview(web, account_info={"state": "NE", "account_id": "NE_OTHER", "country": ""})
    assert shown.status_code == 200, shown.json()
    body = shown.json()
    (section,) = body["plan"]["sections"]
    assert section["changes"] == ["state: None -> 'NE'"]
    assert section["skipped"][0].startswith("account_id: already set to 'NE_LANCA'")
    assert hosts.andromeda.writes() == []

    run_id = body["run_id"]
    assert web.post(f"/api/runs/{run_id}/apply", json={}).json()["kind"] == "confirm"
    assert hosts.andromeda.writes() == []

    assert web.post(f"/api/runs/{run_id}/apply", json={"confirm": True}).status_code == 202
    state = wait(web, run_id)
    assert state["status"] == "done", state["error"]
    assert hosts.andromeda.writes() == ["PUT /v1/andromeda/authorities/4958"]
    assert any("account_id left unchanged" in e.get("message", "")
               for e in state["events"] if e["type"] == "note")

    # applied once; a second press is refused rather than repeated
    assert web.post(f"/api/runs/{run_id}/apply", json={"confirm": True}).status_code == 409


def test_nothing_ticked_is_refused(web):
    r = preview(web)
    assert r.status_code == 422 and "changes" in r.json()["errors"]


def test_a_jurisdiction_needs_the_revision_acknowledged(web, hosts):
    boundary = web.get("/api/place", params={"q": "Lincoln, NE"}).json()
    run_id = preview(web, boundary_id=boundary["boundary_id"],
                     capabilities_integration="88").json()["run_id"]
    r = web.post(f"/api/runs/{run_id}/apply", json={"confirm": True})
    assert r.json()["kind"] == "revision" and hosts.andromeda.writes() == []

    r = web.post(f"/api/runs/{run_id}/apply",
                 json={"confirm": True, "acknowledge_revision": True})
    assert r.status_code == 202
    state = wait(web, run_id)
    assert state["status"] == "done", state["error"]
    done = [e["step"] for e in state["events"] if e["type"] == "step" and e["status"] == "done"]
    assert done == ["boundary", "revision", "capabilities"]
    assert state["snapshots"] == ["capabilities"]


def test_someone_elses_pending_work_is_refused_by_name(tmp_path):
    hosts = working_hosts(batch=[OTHER_WORK])
    with page_client(make_app(hosts, tmp_path)) as web:
        boundary = web.get("/api/place", params={"q": "Lincoln, NE"}).json()
        shown = preview(web, boundary_id=boundary["boundary_id"]).json()
        assert not shown["can_apply"]
        assert "Someone Else (id 1200)" in shown["plan"]["sections"][0]["refusal"]
        r = web.post(f"/api/runs/{shown['run_id']}/apply",
                     json={"confirm": True, "acknowledge_revision": True})
        assert r.json()["kind"] == "refused"
        assert hosts.andromeda.writes() == []


def test_an_added_integration_shows_its_secret_once_and_capabilities_can_be_undone(web, hosts):
    shown = preview(web, add_integration=True, capabilities_integration="new").json()
    assert shown["plan"]["app_name"].startswith("Lancaster NE Sandbox RSP ")
    run_id = shown["run_id"]
    web.post(f"/api/runs/{run_id}/apply", json={"confirm": True})
    state = wait(web, run_id)
    assert state["status"] == "done", state["error"]
    assert state["result"]["integration"]["consumer_secret"] == "SECRET"

    undo = web.post(f"/api/runs/{run_id}/restore", json={"kind": "capabilities"}).json()
    assert undo["applied"] is False and undo["changes"]           # previewed first
    undone = web.post(f"/api/runs/{run_id}/restore",
                      json={"kind": "capabilities", "apply": True}).json()
    assert undone["applied"] is True


def test_roles_are_applied_and_can_be_undone(web, hosts):
    run_id = preview(web, roles=True).json()["run_id"]
    web.post(f"/api/runs/{run_id}/apply", json={"confirm": True})
    state = wait(web, run_id)
    assert state["status"] == "done", state["error"]
    assert state["snapshots"] == ["roles"]
    undo = web.post(f"/api/runs/{run_id}/restore", json={"kind": "roles"}).json()
    assert undo["changes"] and not undo["applied"]


def test_a_failure_says_what_was_written_and_what_to_untick(tmp_path):
    hosts = working_hosts()
    hosts.andromeda.fail_capabilities = True
    with page_client(make_app(hosts, tmp_path)) as web:
        run_id = preview(web, add_integration=True,
                         capabilities_integration="new").json()["run_id"]
        web.post(f"/api/runs/{run_id}/apply", json={"confirm": True})
        state = wait(web, run_id)
        assert state["status"] == "failed"
        error = state["error"]
        assert error["step"] == "capabilities" and not error["refused"]
        assert error["not_done"] == ["Capabilities"]
        assert "Untick the integration" in error["advice"]
        assert error["partial"]["integration"]["consumer_secret"] == "SECRET"


def test_no_token_leaves_the_server(web, hosts):
    run_id = preview(web, roles=True, account_info={"state": "NE"}).json()["run_id"]
    web.post(f"/api/runs/{run_id}/apply", json={"confirm": True})
    state = wait(web, run_id)
    everything = json.dumps(state)
    assert TOKEN_A not in everything
    assert not any(t in everything for t in hosts.portal.issued)
