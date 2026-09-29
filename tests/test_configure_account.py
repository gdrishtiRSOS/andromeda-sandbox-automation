"""Tests for configure_account: everything after sign-up, and resuming it."""

from __future__ import annotations

import copy
import re

import pytest

from logic.capabilities import load_standard
from logic.workflows import (
    PRIMARY_DISPATCH_TYPE,
    AmbiguousIntegrationError,
    AuthorityMismatchError,
    NoBoundaryError,
    PartialConfigureError,
    UnexpectedRevocationError,
    UnpublishableJurisdictionError,
    check_new_account,
    configure_account,
    next_action,
    plan_roles_restore,
    provision_authority,
    restore_capabilities,
)

COLLECTION = {
    "type": "FeatureCollection",
    "features": [{
        "type": "Feature",
        "properties": {"name": "Lancaster"},
        "geometry": {"type": "Polygon", "coordinates": [[
            [-96.9, 40.5], [-96.4, 40.5], [-96.4, 41.0], [-96.9, 41.0], [-96.9, 40.5]
        ]]},
    }],
}

CAPABILITY_CATALOG = [
    {"authority_enabled": False, "rsos_enabled": False,
     "capability_type": {"name": "jurisdiction_view", "category": 0}},
    {"authority_enabled": False, "rsos_enabled": False,
     "capability_type": {"name": "alerts", "category": 2}},
]

STANDARD = load_standard({"capabilities": [
    {"authority_enabled": True, "rsos_enabled": True,
     "capability_type": {"name": "jurisdiction_view", "category": 0}},
    {"authority_enabled": True, "rsos_enabled": True,
     "capability_type": {"name": "alerts", "category": 2}},
]})


def authority_record(aid=4958, name="Lancaster NE", org=15516, account_id=None):
    return {"id": aid, "name": name, "display_name": name, "organization_id": org,
            "account_id": account_id, "dispatch_type": None, "attributes": {}}


class Andromeda:
    """Enough of Andromeda to run the whole composition.

    Modelled on the real sequence: creating a jurisdiction adds it to the
    pending batch; publishing makes it Active -- and its entry *stays* in the
    new pending batch, which is what the real service does.
    """

    def __init__(self, *, authorities=None, jurisdictions=(), batch=(),
                 integrations=(), fail_alerts=False, fail_capabilities=False,
                 hidden_reads=0):
        self.authorities = [dict(a) for a in (authorities or [authority_record()])]
        self.jurisdictions = [dict(j) for j in jurisdictions]
        self.batch = [dict(e) for e in batch]
        self.integrations = [dict(i) for i in integrations]
        self.capabilities = copy.deepcopy(CAPABILITY_CATALOG)
        self.fail_alerts = fail_alerts
        self.fail_capabilities = fail_capabilities
        self.hidden_reads = hidden_reads      # list reads that miss a new authority
        self.pending_id = 1986
        self.calls = []

    # -- helpers
    def writes(self):
        return [f"{m} {p}" for m, p in self.calls if m != "GET"]

    def _authority(self, aid):
        return next(a for a in self.authorities if str(a["id"]) == str(aid))

    def get(self, path):
        self.calls.append(("GET", path))
        if path.startswith("/v1/andromeda/authorities?"):
            if self.hidden_reads:
                self.hidden_reads -= 1
                return {"authorities": [], "total_records": 0}
            return {"authorities": list(self.authorities),
                    "total_records": len(self.authorities)}
        if path == "/v1/andromeda/country":
            return [{"code": "USA", "name": "United States"}]
        if path.startswith("/v1/andromeda/country/"):
            return [{"code": "NE", "name": "Nebraska"}, {"code": "TX", "name": "Texas"}]
        if path == "/v1/andromeda/revisions/pending":
            return {"id": self.pending_id, "revision_number": None,
                    "revision_date": None, "created": [],
                    "modified": list(self.batch), "deleted": []}
        if path.startswith("/v1/andromeda/integration-types"):
            return {"integration_types": [{"id": 6, "name": "RapidSOS Portal"}]}
        if path.endswith("/capabilities"):
            return {"capabilities": copy.deepcopy(self.capabilities)}
        if path.endswith("/integrations"):
            return list(self.integrations)
        if path.endswith("/jurisdictions"):
            return [dict(j) for j in self.jurisdictions]
        match = re.fullmatch(r"/v1/andromeda/authorities/(\d+)", path)
        if match:
            return copy.deepcopy(self._authority(match.group(1)))
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, json=None):
        self.calls.append(("POST", path))
        if path.endswith("/jurisdictions"):
            aid = int(path.split("/")[4])
            record = {"id": 3799, "authority_id": aid, "ingress_status": 1,
                      "egress_status": 3, "shapes": []}
            self.jurisdictions.append(record)
            self.batch.append({"id": 3799, "authority_id": aid, "ingress_status": 2,
                               "egress_status": 3, "shapes": []})
            return dict(record)
        if path == "/v1/andromeda/revisions/pending":
            return None
        if path == "/v1/andromeda/revisions/active":
            published = {str(e["authority_id"]) for e in self.batch}
            for j in self.jurisdictions:
                if str(j["authority_id"]) in published:
                    j["ingress_status"] = 3
            self.pending_id += 1
            return None
        if path.endswith("/integrations"):
            record = {"id": 5223, "app_name": json["app_name"], "product": json["product"],
                      "consumer_key": "KEY", "consumer_secret": "SECRET"}
            self.integrations.append({k: record[k] for k in ("id", "app_name", "product")})
            return record
        raise AssertionError(f"unexpected POST {path}")

    def patch(self, path, json):
        self.calls.append(("PATCH", path))
        if path.endswith("/capabilities"):
            if self.fail_capabilities:
                raise RuntimeError("502 Bad Gateway")
            enabling_alerts = any(c["capability_type"]["name"] == "alerts"
                                  and c["authority_enabled"] for c in json["capabilities"])
            if self.fail_alerts and enabling_alerts:
                raise RuntimeError("500 Internal Server Error")
            self.capabilities = copy.deepcopy(json["capabilities"])
            return copy.deepcopy(json)
        if "/jurisdictions/" in path:
            j = next(j for j in self.jurisdictions if str(j["id"]) == str(json["id"]))
            j["ingress_status"] = json["ingress_status"]
            return dict(j)
        raise AssertionError(f"unexpected PATCH {path}")

    def put(self, path, json):
        self.calls.append(("PUT", path))
        record = self._authority(path.rsplit("/", 1)[1])
        record.clear()
        record.update(copy.deepcopy(json))
        return copy.deepcopy(record)


class Portal:
    CATALOG = [{"name": "alerts", "rsp_rbac": False},
               {"name": "caller_info", "rsp_rbac": False},
               {"name": "MANAGE_SSO", "rsp_rbac": True}]

    def __init__(self, *, agent=(), admin=()):
        self.roles = [{"id": 1, "name": "Admin", "permissions": list(admin)},
                      {"id": 2, "name": "Agent", "permissions": list(agent)}]
        self.calls = []

    def writes(self):
        return [f"{m} {p}" for m, p in self.calls if m != "GET"]

    def get(self, path):
        self.calls.append(("GET", path))
        if path.endswith("/permissions"):
            return list(self.CATALOG)
        if path.endswith("/roles"):
            return copy.deepcopy(self.roles)
        raise AssertionError(f"unexpected GET {path}")

    def patch(self, path, json):
        self.calls.append(("PATCH", path))
        role = next(r for r in self.roles if str(r["id"]) == path.rsplit("/", 1)[1])
        role["permissions"] = list(json["permissions"])
        return copy.deepcopy(role)


def run(andromeda, portal, **kw):
    kw.setdefault("polygon", COLLECTION)
    kw.setdefault("standard", STANDARD)
    kw.setdefault("sleep", lambda _: None)
    return configure_account(andromeda, portal, kw.pop("authority", "Lancaster NE"), **kw)


def active_jurisdiction(aid=4958):
    return {"id": 3799, "authority_id": aid, "ingress_status": 3,
            "egress_status": 3, "shapes": []}


# ------------------------------------------------------------ a fresh account


def test_configures_a_fresh_account_in_runbook_order():
    a, p = Andromeda(), Portal()
    result = run(a, p, organization_id="15516", account_id="SAND_31109",
                 country="USA", state="NE")

    assert a.writes() == [
        "PUT /v1/andromeda/authorities/4958",
        "POST /v1/andromeda/authorities/4958/jurisdictions",
        "PATCH /v1/andromeda/authorities/4958/jurisdictions/3799",
        "POST /v1/andromeda/revisions/pending",
        "POST /v1/andromeda/revisions/active",
        "POST /v1/andromeda/authorities/4958/integrations",
        "PATCH /v1/andromeda/authorities/4958/integrations/5223/capabilities",
    ]
    assert p.writes() == [
        "PATCH /v1/scorpius/organizations/15516/capstone/roles/1",
        "PATCH /v1/scorpius/organizations/15516/capstone/roles/2",
    ]
    assert result.completed == ["account", "account_info", "boundary", "revision",
                                "integration", "capabilities", "roles"]
    assert result.jurisdiction.ingress_label == "Active"
    assert result.integration.consumer_secret == "SECRET"
    assert result.capabilities.applied
    assert result.roles.applied
    assert a._authority(4958)["account_id"] == "SAND_31109"
    assert a._authority(4958)["dispatch_type"] == PRIMARY_DISPATCH_TYPE


def test_the_dispatch_type_is_always_set_to_primary():
    a = Andromeda(authorities=[dict(authority_record(), dispatch_type=2)])
    result = run(a, Portal())
    assert a._authority(4958)["dispatch_type"] == PRIMARY_DISPATCH_TYPE
    assert result.account_info.changed["dispatch_type"] == (2, PRIMARY_DISPATCH_TYPE)


def test_an_account_already_primary_needs_no_account_info_write():
    a = Andromeda(authorities=[dict(authority_record(), dispatch_type=PRIMARY_DISPATCH_TYPE)])
    result = run(a, Portal())
    assert "PUT /v1/andromeda/authorities/4958" not in a.writes()
    assert result.status_of("account_info") == "skipped"


def test_steps_are_streamed_as_they_happen():
    seen = []
    run(Andromeda(), Portal(), on_step=seen.append,
        account_id="SAND_31109", country="USA", state="NE")
    running = [o.step for o in seen if o.status == "running"]
    assert running == ["account", "account_info", "boundary", "integration",
                       "capabilities", "roles"]
    assert [o.step for o in seen if o.status == "done"] == [
        "account", "account_info", "boundary", "revision", "integration",
        "capabilities", "roles"]


def test_snapshots_are_taken_before_each_write():
    a, p = Andromeda(), Portal()
    taken = {}

    def save(label, body):
        # nothing has been written to what is being snapshotted yet
        if label.startswith("capabilities"):
            assert not any("capabilities" in w for w in a.writes())
        if label.startswith("roles"):
            assert p.writes() == []
        taken[label] = copy.deepcopy(body)
        return f"snapshots/{label}.json"

    result = run(a, p, save_snapshot=save)
    assert sorted(taken) == ["capabilities-5223", "roles-15516"]
    assert taken["capabilities-5223"]["capabilities"] == CAPABILITY_CATALOG
    assert [r["permissions"] for r in taken["roles-15516"]["roles"]] == [[], []]
    assert result.snapshots == {"capabilities-5223": "snapshots/capabilities-5223.json",
                                "roles-15516": "snapshots/roles-15516.json"}


def test_waits_for_a_just_created_authority_to_appear():
    slept = []
    a = Andromeda(hidden_reads=2)
    result = run(a, Portal(), sleep=slept.append, find_delay=3.0)
    assert result.authority_id == "4958"
    assert slept[:2] == [3.0, 3.0]


# ------------------------------------------------------------- resuming


def test_running_again_writes_nothing():
    a, p = Andromeda(), Portal()
    run(a, p, account_id="SAND_31109", country="USA", state="NE")
    before_a, before_p = len(a.writes()), len(p.writes())

    again = run(a, p, account_id="SAND_31109", country="USA", state="NE")

    assert a.writes()[before_a:] == []
    assert p.writes()[before_p:] == []
    assert again.status_of("boundary") == "skipped"
    assert again.status_of("revision") == "skipped"
    assert again.status_of("roles") == "skipped"
    assert not again.integration.created


def test_the_published_jurisdiction_still_in_the_batch_is_not_republished():
    """After publishing, the real service lists the entry again in the new
    pending batch. That must read as 'done', not 'waiting to publish'."""
    a = Andromeda(jurisdictions=[active_jurisdiction()],
                  batch=[{"id": 3799, "authority_id": 4958, "ingress_status": 2}])
    result = run(a, Portal(), polygon=None)
    assert "POST /v1/andromeda/revisions/active" not in a.writes()
    assert result.status_of("revision") == "skipped"


def test_a_jurisdiction_waiting_in_the_batch_is_published_not_duplicated():
    waiting = {"id": 3799, "authority_id": 4958, "ingress_status": 2,
               "egress_status": 3, "shapes": []}
    a = Andromeda(jurisdictions=[waiting], batch=[dict(waiting)])
    result = run(a, Portal(), polygon=None)

    assert "POST /v1/andromeda/authorities/4958/jurisdictions" not in a.writes()
    assert "POST /v1/andromeda/revisions/active" in a.writes()
    assert result.status_of("boundary") == "skipped"
    assert result.status_of("revision") == "done"
    assert result.jurisdiction.ingress_label == "Active"


def test_a_supplied_boundary_is_not_used_when_one_is_already_active():
    a = Andromeda(jurisdictions=[active_jurisdiction()])
    result = run(a, Portal())
    assert "POST /v1/andromeda/authorities/4958/jurisdictions" not in a.writes()
    assert any("not used" in n for n in result.notes)


def test_an_integration_from_an_earlier_day_is_reused():
    earlier = {"id": 5100, "app_name": "Lancaster NE Sandbox RSP 2026-09-01",
               "product": "RapidSOS Portal"}
    a = Andromeda(jurisdictions=[active_jurisdiction()], integrations=[earlier])
    result = run(a, Portal(), polygon=None)
    assert "POST /v1/andromeda/authorities/4958/integrations" not in a.writes()
    assert result.integration.id == "5100"
    assert "PATCH /v1/andromeda/authorities/4958/integrations/5100/capabilities" in a.writes()


def test_an_unrelated_integration_does_not_count():
    other = {"id": 5100, "app_name": "Someone's demo", "product": "RapidSOS Portal"}
    a = Andromeda(jurisdictions=[active_jurisdiction()], integrations=[other])
    result = run(a, Portal(), polygon=None)
    assert result.integration.id == "5223"
    assert result.integration.created


# ------------------------------------------------------------- refusals


def assert_nothing_written(a, p):
    assert a.writes() == []
    assert p.writes() == []


def test_someone_elses_pending_work_is_refused_before_anything_is_written():
    a = Andromeda(batch=[{"id": 11, "authority_id": 777, "ingress_status": 2}])
    p = Portal()
    with pytest.raises(PartialConfigureError) as exc:
        run(a, p, account_id="SAND_31109", country="USA", state="NE")

    assert_nothing_written(a, p)
    assert exc.value.failed_step == "revision"
    assert exc.value.refused
    assert exc.value.cause.others == ["777"]
    assert "environment-wide" in next_action(exc.value)
    assert exc.value.result.completed == ["account"]      # a lookup, not a write


def test_other_pending_work_does_not_matter_when_nothing_needs_publishing():
    a = Andromeda(jurisdictions=[active_jurisdiction()],
                  batch=[{"id": 11, "authority_id": 777, "ingress_status": 2}])
    result = run(a, Portal(), polygon=None)
    assert result.status_of("revision") == "skipped"


def test_no_boundary_and_no_jurisdiction_is_refused():
    a, p = Andromeda(), Portal()
    with pytest.raises(PartialConfigureError) as exc:
        run(a, p, polygon=None)
    assert isinstance(exc.value.cause, NoBoundaryError)
    assert exc.value.failed_step == "boundary"
    assert_nothing_written(a, p)


def test_a_jurisdiction_that_cannot_be_published_is_refused():
    stuck = {"id": 3799, "authority_id": 4958, "ingress_status": 1,
             "egress_status": 3, "shapes": []}
    a, p = Andromeda(jurisdictions=[stuck]), Portal()
    with pytest.raises(PartialConfigureError) as exc:
        run(a, p)
    assert isinstance(exc.value.cause, UnpublishableJurisdictionError)
    assert_nothing_written(a, p)


def test_a_shared_name_is_resolved_by_the_signup_organization():
    twins = [authority_record(4958, org=15516), authority_record(4001, org=9999)]
    a = Andromeda(authorities=twins)
    result = run(a, Portal(), organization_id="15516")
    assert result.authority_id == "4958"


def test_a_shared_name_without_an_organization_is_refused():
    twins = [authority_record(4958, org=15516), authority_record(4001, org=9999)]
    a, p = Andromeda(authorities=twins), Portal()
    with pytest.raises(PartialConfigureError) as exc:
        run(a, p)
    assert exc.value.failed_step == "account"
    assert exc.value.refused
    assert_nothing_written(a, p)


def test_an_authority_from_another_organization_is_refused():
    a, p = Andromeda(), Portal()
    with pytest.raises(PartialConfigureError) as exc:
        run(a, p, organization_id="42")
    assert isinstance(exc.value.cause, AuthorityMismatchError)
    assert_nothing_written(a, p)


def test_two_generated_integrations_are_refused_rather_than_guessed():
    dupes = [{"id": 5100, "app_name": "Lancaster NE Sandbox RSP 2026-09-01",
              "product": "RapidSOS Portal"},
             {"id": 5101, "app_name": "Lancaster NE Sandbox RSP 2026-09-02",
              "product": "RapidSOS Portal"}]
    a, p = Andromeda(integrations=dupes), Portal()
    with pytest.raises(PartialConfigureError) as exc:
        run(a, p)
    assert isinstance(exc.value.cause, AmbiguousIntegrationError)
    assert_nothing_written(a, p)


NEW_ACCOUNT_DEFAULTS = ["CONNECTED_SITES", "PROFILE_INFO", "RIDE_SHARING", "SXM"]


class CatchingUpPortal(Portal):
    """A new organization: its roles already hold a few default data sources,
    but the permission catalog lists only the administrative entries until
    capabilities have been applied in Andromeda."""

    ADMIN_ONLY = [{"name": "MANAGE_SSO", "rsp_rbac": True}]
    FULL = ADMIN_ONLY + [{"name": n, "rsp_rbac": False}
                         for n in NEW_ACCOUNT_DEFAULTS + ["alerts"]]

    def __init__(self, andromeda, *, catches_up=True):
        super().__init__(agent=NEW_ACCOUNT_DEFAULTS,
                         admin=NEW_ACCOUNT_DEFAULTS + ["MANAGE_SSO"])
        self.andromeda = andromeda
        self.catches_up = catches_up

    def get(self, path):
        if path.endswith("/permissions"):
            self.calls.append(("GET", path))
            applied = any("capabilities" in w for w in self.andromeda.writes())
            return list(self.FULL if applied and self.catches_up else self.ADMIN_ONLY)
        return super().get(path)


def test_a_new_accounts_default_permissions_do_not_block_the_run():
    """Before capabilities exist, the defaults look like revocations. Judging
    roles then refused every new account, before anything was written."""
    a = Andromeda()
    p = CatchingUpPortal(a)
    result = run(a, p)
    assert result.status_of("roles") == "done"
    agent = next(r for r in p.roles if r["name"] == "Agent")
    assert set(NEW_ACCOUNT_DEFAULTS) <= set(agent["permissions"])
    assert "alerts" in agent["permissions"]


def test_a_role_revocation_is_refused_and_the_roles_are_left_alone():
    a = Andromeda()
    p = CatchingUpPortal(a, catches_up=False)
    with pytest.raises(PartialConfigureError) as exc:
        run(a, p)

    err = exc.value
    assert isinstance(err.cause, UnexpectedRevocationError)
    assert err.failed_step == "roles"
    assert err.refused
    assert "Agent would lose CONNECTED_SITES" in str(err.cause)
    assert "lists 1 permission(s)" in str(err.cause)
    assert p.writes() == []                                  # roles untouched
    assert err.result.completed == ["account", "account_info", "boundary",
                                    "revision", "integration", "capabilities"]
    assert "only the roles step will run" in next_action(err)


def test_refusals_found_up_front_still_mark_the_account_step_done():
    a = Andromeda(batch=[{"id": 11, "authority_id": 777, "ingress_status": 2}])
    with pytest.raises(PartialConfigureError) as exc:
        run(a, Portal())
    assert exc.value.result.status_of("account") == "done"
    assert exc.value.result.status_of("revision") == "failed"


# ---------------------------------------------------- expected, not failures


def test_an_account_id_already_set_is_kept_and_the_rest_applied():
    a = Andromeda(authorities=[authority_record(account_id="EXISTING_1")])
    result = run(a, Portal(), account_id="SAND_31109", country="USA", state="NE")
    assert a._authority(4958)["account_id"] == "EXISTING_1"
    assert a._authority(4958)["attributes"]["state"] == "NE"
    assert "account_id" in result.account_info.skipped
    assert any(n.startswith("account_id left unchanged") for n in result.notes)


def test_skipped_alerts_are_reported_as_a_note():
    a = Andromeda(fail_alerts=True)
    result = run(a, Portal())
    assert result.status_of("capabilities") == "done"
    assert [str(k) for k in result.capabilities.alerts_skipped] == ["alerts(cat 2)"]
    assert any("alerts capabilities left off" in n for n in result.notes)


# ------------------------------------------------------------ partial failure


def test_a_failure_part_way_says_what_exists():
    a = Andromeda(fail_capabilities=True)
    with pytest.raises(PartialConfigureError) as exc:
        run(a, Portal())

    err = exc.value
    assert err.failed_step == "capabilities"
    assert not err.refused
    assert err.result.completed == ["account", "account_info", "boundary",
                                    "revision", "integration"]
    assert err.result.not_done == ["capabilities", "roles"]
    assert err.result.integration.id == "5223"
    existing = " ".join(err.result.existing())
    assert "jurisdiction 3799 (Active)" in existing
    assert "integration" in existing
    assert "Resume" in next_action(err)


def test_resuming_after_a_capabilities_failure_finishes_without_duplicates():
    a, p = Andromeda(fail_capabilities=True), Portal()
    with pytest.raises(PartialConfigureError):
        run(a, p)

    a.fail_capabilities = False
    result = run(a, p)
    assert a.writes().count("POST /v1/andromeda/authorities/4958/jurisdictions") == 1
    assert a.writes().count("POST /v1/andromeda/authorities/4958/integrations") == 1
    assert result.capabilities.applied
    assert result.roles.applied


# ------------------------------------------------------ hook, checks, undo


def test_provision_authority_calls_the_hook_before_writing_capabilities():
    a = Andromeda(jurisdictions=[active_jurisdiction()])
    seen = []

    def hook(integration, live):
        seen.append((integration.id, len(a.writes())))

    provision_authority(a, "4958", skip_jurisdiction=True, standard=STANDARD,
                        before_capabilities=hook)
    assert seen == [("5223", 1)]     # only the integration POST had happened


def test_check_new_account_reports_a_taken_name_and_pending_work():
    a = Andromeda(authorities=[authority_record(),
                               authority_record(777, name="Someone Else", org=1)],
                  batch=[{"id": 11, "authority_id": 777}])
    checks = check_new_account(a, "lancaster ne")
    assert not checks.name_is_free
    assert checks.name_taken_by[0]["id"] == 4958
    assert checks.others_pending == {"777": "Someone Else"}

    assert check_new_account(a, "Brand New").name_is_free


def test_capabilities_can_be_put_back_from_a_snapshot():
    a = Andromeda()
    snapshot = {"capabilities": copy.deepcopy(CAPABILITY_CATALOG)}
    a.capabilities[0]["authority_enabled"] = True

    planned = restore_capabilities(a, "4958", "5223", snapshot, dry_run=True)
    assert len(planned.changed) == 1
    assert a.writes() == []

    done = restore_capabilities(a, "4958", "5223", snapshot)
    assert done.applied
    assert a.capabilities == CAPABILITY_CATALOG


def test_a_roles_restore_can_be_previewed():
    p = Portal(admin=["alerts", "MANAGE_SSO"])
    snapshot = {"organization_id": "15516",
                "roles": [{"id": 1, "name": "Admin", "permissions": []},
                          {"id": 2, "name": "Agent", "permissions": []}]}
    report = plan_roles_restore(p, snapshot, "15516")
    assert report.revoked == {"Admin": ["MANAGE_SSO", "alerts"]}
    assert report.unchanged == ["Agent"]
    assert p.writes() == []
