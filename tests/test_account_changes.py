"""Tests for working on an existing account: describe it, preview, apply."""

from __future__ import annotations

import copy

import pytest

from logic.authorities import AmbiguousAuthorityError, UnknownStateError
from logic.integrations import build_app_name
from logic.revisions import OtherAuthoritiesPendingError
from logic.workflows import (
    NEW_INTEGRATION,
    AccountChanges,
    AccountIdInUseError,
    ChangeRefusedError,
    PartialChangeError,
    StalePlanError,
    UnexpectedRevocationError,
    apply_account_changes,
    change_next_action,
    describe_account,
    plan_account_changes,
)
from test_configure_account import (
    COLLECTION, STANDARD, Andromeda, Portal, authority_record)

ACTIVE = {"id": 3001, "authority_id": 4958, "ingress_status": 3,
          "egress_status": 3, "shapes": []}
OLD_INTEGRATION = {"id": 88, "app_name": "Lancaster NE Sandbox RSP 2026-09-01",
                   "product": "RapidSOS Portal"}
OTHER_WORK = {"id": 3500, "authority_id": 1200, "ingress_status": 2,
              "egress_status": 3, "shapes": []}


def working(**kw):
    """A configured account: account_id set, an Active boundary, one integration."""
    authorities = kw.pop("authorities", None) or [
        authority_record(account_id="NE_LANCA"),
        authority_record(aid=1200, name="Someone Else", org=999, account_id="TX_WASHI")]
    kw.setdefault("jurisdictions", [ACTIVE])
    kw.setdefault("integrations", [OLD_INTEGRATION])
    return Andromeda(authorities=authorities, **kw)


def full_portal():
    everything = [p["name"] for p in Portal.CATALOG]
    return Portal(admin=everything, agent=["alerts", "caller_info"])


def plan(andromeda, portal, changes, **kw):
    return plan_account_changes(andromeda, portal, "4958", changes, **kw)


def apply(andromeda, portal, planned, **kw):
    kw.setdefault("sleep", lambda _: None)
    return apply_account_changes(andromeda, portal, planned, **kw)


# ---------------------------------------------------------------- describe


def test_describe_shows_what_the_account_has_and_writes_nothing():
    andromeda = working(batch=[OTHER_WORK])
    overview = describe_account(andromeda, "lancaster ne")

    assert overview.authority_id == "4958" and overview.organization_id == "15516"
    assert [j.id for j in overview.jurisdictions] == ["3001"]
    assert overview.account_id_locked
    (summary,) = overview.integrations
    assert summary.integration.id == "88"
    assert (summary.total, summary.enabled, summary.rsos_enabled) == (2, 0, 0)
    assert overview.pending_others == {"1200": "Someone Else"}
    assert andromeda.writes() == []


def test_describe_refuses_an_ambiguous_name():
    andromeda = working(authorities=[authority_record(),
                                     authority_record(aid=5000, org=777)])
    with pytest.raises(AmbiguousAuthorityError):
        describe_account(andromeda, "Lancaster NE")
    assert describe_account(andromeda, "5000").authority_id == "5000"


# ----------------------------------------------------------------- preview


def test_preview_writes_nothing_and_shows_every_ticked_section():
    andromeda, portal = working(), Portal()
    planned = plan(andromeda, portal, AccountChanges(
        account_info={"state": "TX"}, polygon=COLLECTION, add_integration=True,
        capabilities_integration="88", standard=STANDARD, roles=True))

    assert list(planned.sections) == ["account_info", "boundary", "revision",
                                      "integration", "capabilities", "roles"]
    assert not planned.blocked
    assert planned.sections["account_info"].changes == ["state: None -> 'TX'"]
    assert "publish a revision" in planned.sections["boundary"].changes[0]
    assert "environment-wide" in planned.sections["revision"].notes[0]
    assert len(planned.sections["capabilities"].changes) == 2
    assert "alerts" in " ".join(planned.sections["capabilities"].notes)
    assert planned.sections["roles"].changes
    assert andromeda.writes() == [] and portal.writes() == []


def test_a_set_account_id_is_shown_as_skipped_not_hidden():
    planned = plan(working(), None, AccountChanges(
        account_info={"account_id": "NE_OTHER", "state": "NE"}))
    section = planned.sections["account_info"]
    assert section.refusal is None
    assert section.changes == ["state: None -> 'NE'"]
    assert section.skipped == ["account_id: already set to 'NE_LANCA'; "
                               "the API refuses to change it"]


def test_an_account_id_held_by_another_authority_is_refused():
    andromeda = working(authorities=[
        authority_record(account_id=None),
        authority_record(aid=1200, name="Someone Else", org=999, account_id="TX_WASHI")])
    planned = plan(andromeda, None, AccountChanges(account_info={"account_id": "TX_WASHI"}))
    assert isinstance(planned.sections["account_info"].refusal, AccountIdInUseError)
    assert planned.blocked


def test_only_the_offered_account_fields_can_change():
    planned = plan(working(), None, AccountChanges(account_info={"name": "Renamed"}))
    refusal = planned.sections["account_info"].refusal
    assert isinstance(refusal, ChangeRefusedError) and "name" in str(refusal)


def test_an_unknown_state_is_refused_in_preview():
    planned = plan(working(), None, AccountChanges(account_info={"country": "USA",
                                                                 "state": "ZZ"}))
    assert isinstance(planned.sections["account_info"].refusal, UnknownStateError)


def test_someone_elses_pending_work_refuses_the_jurisdiction():
    planned = plan(working(batch=[OTHER_WORK]), None, AccountChanges(polygon=COLLECTION))
    assert isinstance(planned.sections["boundary"].refusal, OtherAuthoritiesPendingError)


def test_this_accounts_own_waiting_jurisdiction_is_named():
    mine = {"id": 3002, "authority_id": 4958, "ingress_status": 2,
            "egress_status": 3, "shapes": []}
    planned = plan(working(batch=[mine]), None, AccountChanges(polygon=COLLECTION))
    assert planned.sections["boundary"].refusal is None
    assert "3002" in planned.sections["boundary"].changes[1]


def test_an_added_integration_is_numbered_when_the_name_is_taken():
    today = build_app_name("Lancaster NE")
    andromeda = working(integrations=[OLD_INTEGRATION,
                                      {"id": 89, "app_name": today, "product": "RapidSOS Portal"},
                                      {"id": 90, "app_name": f"{today} 2",
                                       "product": "RapidSOS Portal"}])
    planned = plan(andromeda, None, AccountChanges(add_integration=True))
    assert planned.app_name == f"{today} 3"
    notes = " ".join(planned.sections["integration"].notes)
    assert "numbered" in notes and "Resume setup" in notes


def test_capabilities_need_a_real_integration_or_the_new_one():
    assert isinstance(plan(working(), None, AccountChanges(capabilities_integration="999"))
                      .sections["capabilities"].refusal, ChangeRefusedError)
    assert isinstance(plan(working(), None,
                           AccountChanges(capabilities_integration=NEW_INTEGRATION))
                      .sections["capabilities"].refusal, ChangeRefusedError)
    ok = plan(working(), None, AccountChanges(add_integration=True,
                                              capabilities_integration=NEW_INTEGRATION))
    assert ok.sections["capabilities"].refusal is None


def test_roles_without_a_portal_login_are_refused_with_the_reason():
    planned = plan(working(), None, AccountChanges(roles=True),
                   portal_problem="The portal refused to log in as x@y.z")
    assert "refused to log in" in str(planned.sections["roles"].refusal)


def test_a_role_revocation_is_refused_unless_capabilities_may_change_it():
    portal = Portal(admin=["alerts", "caller_info", "MANAGE_SSO", "GONE"])
    alone = plan(working(), portal, AccountChanges(roles=True))
    assert isinstance(alone.sections["roles"].refusal, UnexpectedRevocationError)

    with_caps = plan(working(), portal, AccountChanges(
        roles=True, capabilities_integration="88", standard=STANDARD))
    assert with_caps.sections["roles"].refusal is None
    assert "checked again" in " ".join(with_caps.sections["roles"].notes)


# ------------------------------------------------------------------- apply


def test_only_the_ticked_steps_run():
    andromeda = working()
    planned = plan(andromeda, None, AccountChanges(account_info={"state": "NE"}))
    result = apply(andromeda, None, planned)

    assert andromeda.writes() == ["PUT /v1/andromeda/authorities/4958"]
    assert [s.step for s in result.steps if s.status == "done"] == ["account_info"]
    assert andromeda._authority(4958)["dispatch_type"] is None      # left alone


def test_jurisdiction_runs_before_capabilities_and_roles_run_last():
    andromeda, portal = working(), Portal()
    snapshots = []
    planned = plan(andromeda, portal, AccountChanges(
        polygon=COLLECTION, capabilities_integration="88", standard=STANDARD, roles=True))
    result = apply(andromeda, portal, planned,
                   save_snapshot=lambda label, body: snapshots.append(label) or label)

    writes = andromeda.writes()
    assert writes.index("POST /v1/andromeda/revisions/active") < writes.index(
        "PATCH /v1/andromeda/authorities/4958/integrations/88/capabilities")
    assert snapshots == ["capabilities-88", "roles-15516"]
    assert result.jurisdiction.ingress_label == "Active"
    assert result.roles.applied
    assert [s.step for s in result.steps if s.status == "done"] == [
        "boundary", "revision", "capabilities", "roles"]


def test_capabilities_can_target_the_integration_being_added():
    andromeda = working()
    planned = plan(andromeda, None, AccountChanges(
        add_integration=True, capabilities_integration=NEW_INTEGRATION, standard=STANDARD))
    result = apply(andromeda, None, planned)
    assert result.integration.consumer_secret == "SECRET"
    assert "PATCH /v1/andromeda/authorities/4958/integrations/5223/capabilities" in \
        andromeda.writes()


def test_the_alerts_fallback_is_reported_not_failed():
    andromeda = working(fail_alerts=True)
    planned = plan(andromeda, None, AccountChanges(capabilities_integration="88",
                                                   standard=STANDARD))
    result = apply(andromeda, None, planned)
    assert [str(k) for k in result.capabilities.alerts_skipped] == ["alerts(cat 2)"]
    assert any("alerts capabilities left off" in n for n in result.notes)


def test_a_refused_plan_writes_nothing():
    andromeda = working(batch=[OTHER_WORK])
    planned = plan(andromeda, None, AccountChanges(account_info={"state": "NE"},
                                                   polygon=COLLECTION))
    with pytest.raises(PartialChangeError) as caught:
        apply(andromeda, None, planned)
    assert caught.value.refused
    assert andromeda.writes() == []


def test_someone_elses_work_arriving_after_preview_is_refused_before_anything_is_created():
    andromeda = working()
    planned = plan(andromeda, None, AccountChanges(account_info={"state": "NE"},
                                                   polygon=COLLECTION))
    andromeda.batch.append(dict(OTHER_WORK))
    with pytest.raises(PartialChangeError) as caught:
        apply(andromeda, None, planned)
    assert isinstance(caught.value.cause, StalePlanError)
    assert andromeda.writes() == []
    assert "Preview again" in change_next_action(caught.value)


def test_an_account_that_moved_since_preview_is_refused():
    andromeda = working()
    planned = plan(andromeda, None, AccountChanges(capabilities_integration="88",
                                                   standard=STANDARD))
    for entry in andromeda.capabilities:
        entry["authority_enabled"] = entry["rsos_enabled"] = True   # someone else did it
    with pytest.raises(PartialChangeError) as caught:
        apply(andromeda, None, planned)
    assert caught.value.cause.sections == ["capabilities"]
    assert andromeda.writes() == []


def test_a_revocation_that_appears_at_the_roles_step_is_refused():
    andromeda = working()
    portal = full_portal()
    planned = plan(andromeda, portal, AccountChanges(
        capabilities_integration="88", standard=STANDARD, roles=True))
    portal.roles[0]["permissions"].append("GONE")           # arrives mid-run
    portal_before = copy.deepcopy(portal.roles)

    # the plan is re-checked before writing, so this is caught as a moved account
    with pytest.raises(PartialChangeError) as caught:
        apply(andromeda, portal, planned)
    assert isinstance(caught.value.cause, StalePlanError)
    assert portal.roles == portal_before and andromeda.writes() == []


def test_a_revocation_that_only_shows_after_capabilities_is_refused_there():
    portal = full_portal()

    class Shifting(Andromeda):
        def patch(self, path, json):
            out = super().patch(path, json)
            portal.roles[0]["permissions"].append("GONE")   # the catalog moved
            return out

    andromeda = Shifting(authorities=[authority_record(account_id="NE_LANCA")],
                         jurisdictions=[ACTIVE], integrations=[OLD_INTEGRATION])
    planned = plan(andromeda, portal, AccountChanges(
        capabilities_integration="88", standard=STANDARD, roles=True))
    with pytest.raises(PartialChangeError) as caught:
        apply(andromeda, portal, planned)
    assert isinstance(caught.value.cause, UnexpectedRevocationError)
    assert caught.value.result.completed == ["capabilities"]
    assert portal.writes() == []


def test_a_failure_after_an_add_says_to_untick_it():
    andromeda = working(fail_capabilities=True)
    planned = plan(andromeda, None, AccountChanges(
        add_integration=True, capabilities_integration=NEW_INTEGRATION, standard=STANDARD))
    with pytest.raises(PartialChangeError) as caught:
        apply(andromeda, None, planned)
    err = caught.value
    assert err.failed_step == "capabilities" and not err.refused
    assert err.result.completed == ["integration"]
    assert err.result.not_done == ["capabilities"]
    assert any("integration" in line for line in err.result.existing())
    assert "Untick the integration" in change_next_action(err)


def test_nothing_ticked_is_refused():
    with pytest.raises(PartialChangeError):
        apply(working(), None, plan(working(), None, AccountChanges()))
