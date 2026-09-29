"""Tests for Role and Access (runbook step 8)."""

from __future__ import annotations

import pytest

from logic.roles import (
    ADMIN_ROLE,
    AGENT_ROLE,
    Role,
    RoleNotFoundError,
    RoleWriteError,
    enable_all_data_sources,
    list_permissions,
    list_roles,
    target_permissions,
)

# A miniature catalog with the same shape as the real one: a few data sources
# and two administrative permissions flagged rsp_rbac.
CATALOG = [
    {"name": "alerts", "display_name": "Alerts", "rsp_rbac": False,
     "whitelisting_required": False},
    {"name": "solutions_engineering", "display_name": "Solutions Engineering",
     "rsp_rbac": False, "whitelisting_required": False},
    {"name": "eagleview", "display_name": "EagleView", "rsp_rbac": False,
     "whitelisting_required": True},
    {"name": "MANAGE_USERS", "display_name": "Users & Roles", "rsp_rbac": True,
     "whitelisting_required": False},
    {"name": "MANAGE_SSO", "display_name": "Manage SSO", "rsp_rbac": True,
     "whitelisting_required": False},
]

ALL_NAMES = {p["name"] for p in CATALOG}
NON_ADMIN = {p["name"] for p in CATALOG if not p["rsp_rbac"]}


def role(id_, name, permissions):
    return {"id": id_, "name": name, "permissions": list(permissions),
            "application": "capstone", "avatar_bg_color": "#ff8b00"}


class FakeClient:
    def __init__(self, roles=None, catalog=CATALOG):
        self.catalog = catalog
        self.roles = roles if roles is not None else [
            role(16944, "Admin", ["alerts"]),
            role(16945, "Agent", []),
        ]
        self.patches = []

    def get(self, path):
        if path.endswith("/permissions"):
            return self.catalog
        if path.endswith("/roles"):
            return self.roles
        raise AssertionError(path)

    def patch(self, path, json):
        self.patches.append((path, json))
        role_id = path.rsplit("/", 1)[1]
        for r in self.roles:
            if str(r["id"]) == role_id:
                r["permissions"] = list(json["permissions"])
                return dict(r)
        raise AssertionError(f"no role {role_id}")


# ---------------------------------------------------------------- policy


def test_admin_gets_every_permission():
    assert target_permissions(CATALOG, ADMIN_ROLE) == ALL_NAMES


def test_agent_gets_everything_except_the_administrative_ones():
    wanted = target_permissions(CATALOG, AGENT_ROLE)
    assert wanted == NON_ADMIN
    assert "MANAGE_USERS" not in wanted
    assert "MANAGE_SSO" not in wanted


def test_agent_still_gets_whitelisted_data_sources():
    """whitelisting_required is not the same as administrative."""
    assert "eagleview" in target_permissions(CATALOG, AGENT_ROLE)


def test_a_new_permission_is_picked_up_automatically():
    """The point of deriving rather than listing."""
    extended = CATALOG + [{"name": "brand_new", "rsp_rbac": False}]
    assert "brand_new" in target_permissions(extended, AGENT_ROLE)
    assert "brand_new" in target_permissions(extended, ADMIN_ROLE)


def test_a_new_admin_permission_stays_off_agent():
    extended = CATALOG + [{"name": "MANAGE_NEW", "rsp_rbac": True}]
    assert "MANAGE_NEW" not in target_permissions(extended, AGENT_ROLE)
    assert "MANAGE_NEW" in target_permissions(extended, ADMIN_ROLE)


def test_an_unknown_role_is_treated_as_non_admin():
    assert target_permissions(CATALOG, "Supervisor") == NON_ADMIN


# ----------------------------------------------------------------- reads


def test_catalog_and_roles_are_parsed():
    client = FakeClient()
    assert len(list_permissions(client, "15516")) == 5
    roles = list_roles(client, "15516")
    assert [r.name for r in roles] == ["Admin", "Agent"]
    assert roles[0].permissions == frozenset({"alerts"})


def test_wrapped_payloads_are_handled():
    class Wrapped(FakeClient):
        def get(self, path):
            if path.endswith("/permissions"):
                return {"permissions": self.catalog}
            return {"roles": self.roles}

    assert len(list_permissions(Wrapped(), "15516")) == 5
    assert len(list_roles(Wrapped(), "15516")) == 2


# ----------------------------------------------------------------- write


def test_both_roles_are_updated():
    client = FakeClient()
    report = enable_all_data_sources(client, "15516")

    assert len(client.patches) == 2
    paths = [p for p, _ in client.patches]
    assert paths == [
        "/v1/scorpius/organizations/15516/capstone/roles/16944",
        "/v1/scorpius/organizations/15516/capstone/roles/16945",
    ]
    admin_body = client.patches[0][1]
    agent_body = client.patches[1][1]
    assert set(admin_body["permissions"]) == ALL_NAMES
    assert set(agent_body["permissions"]) == NON_ADMIN
    assert report.applied


def test_the_body_carries_the_role_name():
    """The API requires it alongside permissions."""
    client = FakeClient()
    enable_all_data_sources(client, "15516")
    assert client.patches[0][1]["name"] == "Admin"
    assert client.patches[1][1]["name"] == "Agent"


def test_report_lists_what_was_granted():
    client = FakeClient()
    report = enable_all_data_sources(client, "15516")
    assert "MANAGE_SSO" in report.granted["Admin"]
    assert "alerts" not in report.granted["Admin"]        # already held
    assert "alerts" in report.granted["Agent"]


def test_running_twice_changes_nothing_the_second_time():
    client = FakeClient()
    enable_all_data_sources(client, "15516")
    client.patches.clear()
    report = enable_all_data_sources(client, "15516")
    assert client.patches == []
    assert report.is_noop
    assert set(report.unchanged) == {"Admin", "Agent"}


def test_an_extra_permission_on_agent_is_revoked_and_reported():
    client = FakeClient(roles=[
        role(16944, "Admin", ALL_NAMES),
        role(16945, "Agent", list(NON_ADMIN) + ["MANAGE_SSO"]),
    ])
    report = enable_all_data_sources(client, "15516")
    assert report.revoked["Agent"] == ["MANAGE_SSO"]
    assert "MANAGE_SSO" not in client.patches[0][1]["permissions"]


def test_dry_run_writes_nothing():
    client = FakeClient()
    report = enable_all_data_sources(client, "15516", dry_run=True)
    assert client.patches == []
    assert not report.applied
    assert report.granted


def test_a_missing_role_is_a_clear_error():
    client = FakeClient(roles=[role(16944, "Admin", [])])
    with pytest.raises(RoleNotFoundError) as exc:
        enable_all_data_sources(client, "15516")
    assert exc.value.wanted == ["Agent"]
    assert exc.value.available == ["Admin"]


def test_only_named_roles_are_touched():
    client = FakeClient()
    enable_all_data_sources(client, "15516", role_names=["Admin"])
    assert len(client.patches) == 1
    assert client.patches[0][1]["name"] == "Admin"


def test_a_silent_rejection_is_caught():
    class Stubborn(FakeClient):
        def patch(self, path, json):
            self.patches.append((path, json))
            stored = dict(json)
            stored["id"] = path.rsplit("/", 1)[1]
            stored["permissions"] = [p for p in json["permissions"] if p != "MANAGE_SSO"]
            return stored

    with pytest.raises(RoleWriteError) as exc:
        enable_all_data_sources(Stubborn(), "15516")
    assert exc.value.missing == ["MANAGE_SSO"]


def test_role_str_is_readable():
    r = Role(id="16944", name="Admin", permissions=frozenset({"a", "b"}))
    assert str(r) == "Admin (id 16944, 2 permission(s))"


def test_summary_reads_well():
    client = FakeClient()
    report = enable_all_data_sources(client, "15516")
    assert report.summary() == (
        "organization 15516: Admin +4, Agent +3 (applied)"
    )


# --------------------------------------------------------- snapshot / undo


def test_snapshot_captures_both_roles():
    from logic.roles import snapshot_roles
    snap = snapshot_roles(FakeClient(), "15516")
    assert snap["organization_id"] == "15516"
    assert [r["name"] for r in snap["roles"]] == ["Admin", "Agent"]
    assert snap["roles"][0]["permissions"] == ["alerts"]
    assert "captured_at" in snap


def test_a_change_can_be_undone():
    from logic.roles import restore_roles, snapshot_roles
    client = FakeClient()
    before = snapshot_roles(client, "15516")

    enable_all_data_sources(client, "15516")
    assert set(client.roles[0]["permissions"]) == ALL_NAMES

    restore_roles(client, before)
    assert client.roles[0]["permissions"] == ["alerts"]
    assert client.roles[1]["permissions"] == []


def test_restoring_an_unchanged_org_writes_nothing():
    from logic.roles import restore_roles, snapshot_roles
    client = FakeClient()
    snap = snapshot_roles(client, "15516")
    client.patches.clear()
    report = restore_roles(client, snap)
    assert client.patches == []
    assert report.is_noop


def test_a_snapshot_from_another_org_is_refused():
    from logic.roles import RoleError, restore_roles
    snap = {"organization_id": "99999", "roles": []}
    with pytest.raises(RoleError, match="not 15516"):
        restore_roles(FakeClient(), snap, organization_id="15516")


def test_restoring_a_role_that_no_longer_exists_is_refused():
    from logic.roles import RoleError, restore_roles
    snap = {"organization_id": "15516",
            "roles": [{"id": "99", "name": "Ghost", "permissions": []}]}
    with pytest.raises(RoleError, match="no longer exists"):
        restore_roles(FakeClient(), snap)


def test_snapshot_survives_a_round_trip_through_json():
    import json
    from logic.roles import restore_roles, snapshot_roles
    client = FakeClient()
    snap = json.loads(json.dumps(snapshot_roles(client, "15516")))
    enable_all_data_sources(client, "15516")
    restore_roles(client, snap)
    assert client.roles[0]["permissions"] == ["alerts"]