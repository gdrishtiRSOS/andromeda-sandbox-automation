"""Tests for the combined attach-and-activate flow."""

from __future__ import annotations

import json

import pytest

from logic.jurisdictions import InvalidGeometryError
from logic.revisions import OtherAuthoritiesPendingError
from logic.workflows import (
    NotInPendingBatchError,
    PartialActivationError,
    attach_and_activate,
)

COLLECTION = {
    "type": "FeatureCollection",
    "features": [{
        "type": "Feature",
        "properties": {"name": "Dublin"},
        "geometry": {"type": "Polygon", "coordinates": [[
            [-6.3, 53.3], [-6.2, 53.3], [-6.2, 53.4], [-6.3, 53.4], [-6.3, 53.3]
        ]]},
    }],
}


def batch_entry(jurisdiction_id, authority_id=4958):
    return {
        "authority_id": authority_id,
        "id": jurisdiction_id,
        "ingress_status": 2,
        "egress_status": 3,
        "shapes": [{"id": 1}],
    }


class FakeClient:
    """Models the real sequence: creating a jurisdiction adds it to the batch."""

    def __init__(self, *, extra_batch_entries=(), pending_id="1986", new_id=3799):
        self.new_id = new_id
        self.pending_id = pending_id
        self.extra = list(extra_batch_entries)
        self.batch = list(extra_batch_entries)
        self.published = False
        self.calls = []

    def get(self, path):
        self.calls.append(("GET", path))
        if path.endswith("/jurisdictions"):
            return []
        if path.endswith("/pending"):
            return {
                "id": "2019" if self.published else self.pending_id,
                "revision_number": None,
                "revision_date": None,
                "created": [],
                "modified": list(self.batch),
                "deleted": [],
            }
        return {}

    def post(self, path, json=None):
        self.calls.append(("POST", path))
        if path.endswith("/jurisdictions"):
            self.batch.append(batch_entry(self.new_id))
            return {
                "id": self.new_id, "authority_id": 4958,
                "ingress_status": json["ingress_status"],
                "egress_status": json["egress_status"],
                "shapes": [{"top_left": [-6.3, 53.4], "bottom_right": [-6.2, 53.3]}],
            }
        if path.endswith("/revisions/active"):
            self.published = True
        return None

    def patch(self, path, json):
        self.calls.append(("PATCH", path))
        return {"id": json["id"], "authority_id": 4958,
                "ingress_status": json["ingress_status"],
                "egress_status": json["egress_status"], "shapes": []}


def test_attaches_then_publishes():
    client = FakeClient()
    result = attach_and_activate(client, "4958", polygon=COLLECTION,
                                 revision_date="2026-09-22")

    assert result.activated
    assert result.jurisdiction.id == "3799"
    assert result.revision.revision_number == 2209
    assert result.revision.published_revision_id == "1986"
    assert result.revision.next_pending_id == "2019"

    methods = [c for c in client.calls if c[0] in ("POST", "PATCH")]
    assert methods == [
        ("POST", "/v1/andromeda/authorities/4958/jurisdictions"),
        ("PATCH", "/v1/andromeda/authorities/4958/jurisdictions/3799"),
        ("POST", "/v1/andromeda/revisions/pending"),
        ("POST", "/v1/andromeda/revisions/active"),
    ]


def test_pending_is_read_after_the_jurisdiction_is_created():
    """Creating the jurisdiction is what puts it in the batch."""
    client = FakeClient()
    attach_and_activate(client, "4958", polygon=COLLECTION)
    order = [f"{m} {p}" for m, p in client.calls]
    created = order.index("POST /v1/andromeda/authorities/4958/jurisdictions")
    read = order.index("GET /v1/andromeda/revisions/pending")
    assert created < read


def test_activation_can_be_skipped():
    client = FakeClient()
    result = attach_and_activate(client, "4958", polygon=COLLECTION, activate=False)
    assert not result.activated
    assert result.revision is None
    assert not any(p.endswith("/revisions/active") for _, p in client.calls)


def test_bad_geometry_creates_nothing():
    client = FakeClient()
    with pytest.raises(InvalidGeometryError):
        attach_and_activate(client, "4958",
                            polygon={"type": "Polygon", "coordinates": [[[9999, 9999]]]})
    assert not any(m in ("POST", "PATCH") for m, _ in client.calls)


def test_dry_run_does_nothing():
    client = FakeClient()
    result = attach_and_activate(client, "4958", polygon=COLLECTION, dry_run=True)
    assert not any(m in ("POST", "PATCH") for m, _ in client.calls)
    assert not result.activated


def test_reads_a_named_boundary_from_the_data_dir(tmp_path):
    (tmp_path / "dublin.geojson").write_text(json.dumps(COLLECTION))
    from logic.jurisdictions import load_geojson
    client = FakeClient()
    result = attach_and_activate(
        client, "4958", polygon=load_geojson("dublin", data_dir=tmp_path)
    )
    assert result.activated


def test_failure_to_publish_reports_the_orphan_jurisdiction():
    """Another authority in the batch blocks publishing -- say what exists."""
    client = FakeClient(extra_batch_entries=[batch_entry(9999, authority_id=5001)])
    with pytest.raises(PartialActivationError) as exc:
        attach_and_activate(client, "4958", polygon=COLLECTION)

    assert exc.value.jurisdiction.id == "3799"
    assert isinstance(exc.value.cause, OtherAuthoritiesPendingError)
    assert "was created but not activated" in str(exc.value)
    # the jurisdiction really was created; only the publish was refused
    assert ("POST", "/v1/andromeda/authorities/4958/jurisdictions") in client.calls
    assert not any(p.endswith("/revisions/active") for _, p in client.calls)


def test_other_authorities_can_still_be_allowed():
    client = FakeClient(extra_batch_entries=[batch_entry(9999, authority_id=5001)])
    result = attach_and_activate(client, "4958", polygon=COLLECTION,
                                 allow_other_authorities=True)
    assert result.activated


def test_jurisdiction_missing_from_the_batch_is_caught():
    class Amnesiac(FakeClient):
        def post(self, path, json=None):
            if path.endswith("/jurisdictions"):
                self.calls.append(("POST", path))
                return {"id": self.new_id, "authority_id": 4958,
                        "ingress_status": 1, "egress_status": 3, "shapes": []}
            return super().post(path, json)

    client = Amnesiac()
    with pytest.raises(NotInPendingBatchError) as exc:
        attach_and_activate(client, "4958", polygon=COLLECTION)
    assert exc.value.jurisdiction.id == "3799"
    assert not any(p.endswith("/revisions/active") for _, p in client.calls)


def test_summary_reads_well():
    client = FakeClient()
    result = attach_and_activate(client, "4958", polygon=COLLECTION,
                                 revision_date="2026-09-22")
    assert result.summary() == (
        "jurisdiction 3799 attached and activated via revision 1986 (number 2209)"
    )


# ------------------------------------------------------- full provisioning


CAPABILITY_CATALOG = [
    {"authority_enabled": False, "rsos_enabled": False,
     "capability_type": {"name": "jurisdiction_view", "category": 0,
                         "display_name": "Jurisdiction View"}},
    {"authority_enabled": False, "rsos_enabled": False,
     "capability_type": {"name": "alerts", "category": 2,
                         "display_name": "Alerts ADR"}},
]

STANDARD = {
    "capabilities": [
        {"authority_enabled": True, "rsos_enabled": True,
         "capability_type": {"name": "jurisdiction_view", "category": 0,
                             "display_name": "Jurisdiction View"}},
        {"authority_enabled": True, "rsos_enabled": True,
         "capability_type": {"name": "alerts", "category": 2,
                             "display_name": "Alerts ADR"}},
    ]
}


class ProvisionClient(FakeClient):
    """Adds authority, integration and capability endpoints to the fake."""

    def __init__(self, *, jurisdiction_active=True, fail_alerts=False, **kw):
        super().__init__(**kw)
        self.jurisdiction_active = jurisdiction_active
        self.fail_alerts = fail_alerts
        self.integrations = []
        self.capabilities = [dict(c, capability_type=dict(c["capability_type"]))
                             for c in CAPABILITY_CATALOG]

    def get(self, path):
        self.calls.append(("GET", path))
        if path.endswith("/capabilities"):
            return {"capabilities": self.capabilities}
        if path.endswith("/integrations"):
            return list(self.integrations)
        if path.endswith("/jurisdictions"):
            if not self.published:
                return []
            return [{"id": self.new_id, "authority_id": 4958,
                     "ingress_status": 3 if self.jurisdiction_active else 2,
                     "egress_status": 3, "shapes": []}]
        if path.endswith("/pending"):
            return super().get(path)
        if "integration-types" in path:
            return {"integration_types": [
                {"id": 6, "name": "RapidSOS Portal",
                 "apigee_product_name": "Capstone-Pre-Production",
                 "capability_types": []}]}
        return {"id": 4958, "name": "gDTest", "display_name": "gDTest"}

    def post(self, path, json=None):
        if path.endswith("/integrations"):
            self.calls.append(("POST", path))
            record = {"id": 5223, "app_name": json["app_name"],
                      "product": json["product"], "consumer_key": "KEY",
                      "consumer_secret": "SECRET"}
            self.integrations.append(record)
            return record
        return super().post(path, json)

    def patch(self, path, json):
        if path.endswith("/capabilities"):
            self.calls.append(("PATCH", path))
            enabling_alerts = any(
                c["capability_type"]["name"] == "alerts" and c["authority_enabled"]
                for c in json["capabilities"]
            )
            if self.fail_alerts and enabling_alerts:
                raise RuntimeError("500 Internal Server Error")
            self.capabilities = json["capabilities"]
            return json
        return super().patch(path, json)


def standard_map():
    from logic.capabilities import load_standard
    return load_standard(STANDARD)


def test_provisions_in_the_documented_order():
    from logic.workflows import provision_authority
    client = ProvisionClient()
    result = provision_authority(client, "4958", polygon=COLLECTION,
                                 standard=standard_map(), sleep=lambda _: None)

    writes = [f"{m} {p}" for m, p in client.calls if m in ("POST", "PATCH")]
    assert writes == [
        "POST /v1/andromeda/authorities/4958/jurisdictions",
        "PATCH /v1/andromeda/authorities/4958/jurisdictions/3799",
        "POST /v1/andromeda/revisions/pending",
        "POST /v1/andromeda/revisions/active",
        "POST /v1/andromeda/authorities/4958/integrations",
        "PATCH /v1/andromeda/authorities/4958/integrations/5223/capabilities",
    ]
    assert result.jurisdiction.id == "3799"
    assert result.integration.id == "5223"
    assert len(result.capabilities.changed) == 2
    assert "jurisdiction confirmed" in result.steps


def test_jurisdiction_is_confirmed_before_the_integration_is_made():
    from logic.workflows import provision_authority
    client = ProvisionClient()
    provision_authority(client, "4958", polygon=COLLECTION,
                        standard=standard_map(), sleep=lambda _: None)
    order = [f"{m} {p}" for m, p in client.calls]
    confirmed = order.index("GET /v1/andromeda/authorities/4958/jurisdictions",
                            order.index("POST /v1/andromeda/revisions/active"))
    integration = order.index("POST /v1/andromeda/authorities/4958/integrations")
    assert confirmed < integration


def test_polls_while_the_jurisdiction_is_not_yet_active():
    from logic.workflows import confirm_jurisdiction_active
    slept = []

    class Slow(ProvisionClient):
        def __init__(self):
            super().__init__(jurisdiction_active=False)
            self.published = True
            self.reads = 0

        def get(self, path):
            if path.endswith("/jurisdictions"):
                self.reads += 1
                active = self.reads >= 3
                return [{"id": 3799, "authority_id": 4958,
                         "ingress_status": 3 if active else 2,
                         "egress_status": 3, "shapes": []}]
            return super().get(path)

    client = Slow()
    seen = confirm_jurisdiction_active(client, "4958", "3799",
                                       sleep=lambda d: slept.append(d))
    assert seen.ingress_status == 3
    assert len(slept) == 2


def test_never_active_warns_but_continues_by_default():
    from logic.workflows import confirm_jurisdiction_active
    client = ProvisionClient(jurisdiction_active=False)
    client.published = True
    seen = confirm_jurisdiction_active(client, "4958", "3799", attempts=2,
                                       sleep=lambda _: None)
    assert seen.ingress_status == 2


def test_never_active_can_be_made_fatal():
    from logic.workflows import JurisdictionNotActiveError, confirm_jurisdiction_active
    client = ProvisionClient(jurisdiction_active=False)
    client.published = True
    with pytest.raises(JurisdictionNotActiveError):
        confirm_jurisdiction_active(client, "4958", "3799", attempts=2,
                                    require_active=True, sleep=lambda _: None)


def test_skip_jurisdiction_goes_straight_to_the_integration():
    from logic.workflows import provision_authority
    client = ProvisionClient()
    result = provision_authority(client, "4958", skip_jurisdiction=True,
                                 standard=standard_map(), sleep=lambda _: None)
    assert result.jurisdiction is None
    assert result.integration.id == "5223"
    assert not any(p.endswith("/jurisdictions") and m == "POST"
                   for m, p in client.calls)


def test_alerts_fallback_still_applies_within_provisioning():
    from logic.workflows import provision_authority
    client = ProvisionClient(fail_alerts=True)
    result = provision_authority(client, "4958", polygon=COLLECTION,
                                 standard=standard_map(), sleep=lambda _: None)
    assert result.capabilities.fallback_used
    assert [str(k) for k in result.capabilities.alerts_skipped] == ["alerts(cat 2)"]
    assert "1 alerts skipped" in result.steps


def test_failure_reports_what_already_happened():
    from logic.workflows import PartialProvisionError, provision_authority

    class NoIntegrations(ProvisionClient):
        def post(self, path, json=None):
            if path.endswith("/integrations"):
                raise RuntimeError("404 Not Found")
            return super().post(path, json)

    client = NoIntegrations()
    with pytest.raises(PartialProvisionError) as exc:
        provision_authority(client, "4958", polygon=COLLECTION,
                            standard=standard_map(), sleep=lambda _: None)
    assert exc.value.failed_step == "creating the integration"
    assert exc.value.result.jurisdiction.id == "3799"
    assert "revision published" in exc.value.result.steps
    assert exc.value.result.integration is None


def test_dry_run_writes_nothing_anywhere():
    from logic.workflows import provision_authority
    client = ProvisionClient()
    result = provision_authority(client, "4958", polygon=COLLECTION,
                                 standard=standard_map(), dry_run=True,
                                 sleep=lambda _: None)
    assert not any(m in ("POST", "PATCH") for m, _ in client.calls)
    assert result.capabilities is None


# ------------------------------------------------ place -> sandbox account


def fake_place(geoid="48477", name="Washington", state="TX"):
    from logic.places import ResolvedPlace
    return ResolvedPlace(
        query=geoid, geoid=geoid, county_name=name, state=state,
        matched_as="geoid", matched_name=geoid,
        result={
            "county": {"name": name, "state": state, "geoid": geoid,
                       "boundary_type": "county_footprint"},
            "geometry": {"type": "MultiPolygon", "coordinates": [[[
                [-96.79, 30.04], [-96.08, 30.04], [-96.08, 30.39],
                [-96.79, 30.39], [-96.79, 30.04]]]]},
            "jurisdiction_scope": {"status": "ok", "eccs": [
                {"name": "Washington County 9-1-1", "fcc_psap_id": "6452"}]},
            "sources": [], "disclaimer": "not for routing",
        },
    )


class PlaceAwareClient(ProvisionClient):
    """Adds the authority listing and record so resolution works."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.authority = {
            "id": 4958, "name": "gDTest", "display_name": "gDTest",
            "account_id": None, "dispatch_type": 1, "organization_id": 15516,
            "attributes": {"country": None, "state": None},
        }
        self.put_body = None
        self.jurisdiction_body = None

    def get(self, path):
        if path.startswith("/v1/andromeda/authorities?"):
            self.calls.append(("GET", path))
            return {"authorities": [self.authority], "total_records": 1}
        if path == "/v1/andromeda/country":
            return [{"code": "USA", "name": "United States"}]
        if path.startswith("/v1/andromeda/country/"):
            return [{"code": "TX", "name": "Texas"}]
        if path == "/v1/andromeda/authorities/4958":
            self.calls.append(("GET", path))
            return self.authority
        return super().get(path)

    def put(self, path, json):
        self.calls.append(("PUT", path))
        self.put_body = json
        return json

    def post(self, path, json=None):
        if path.endswith("/jurisdictions"):
            self.jurisdiction_body = json
        return super().post(path, json)


def test_place_drives_the_whole_account(monkeypatch):
    from logic import workflows
    import logic.places as places_mod
    monkeypatch.setattr(places_mod, "resolve_place",
                        lambda q, **kw: fake_place())

    client = PlaceAwareClient()
    account = workflows.create_sandbox_account(
        client, "Washington County, TX", authority="gDTest",
        standard=standard_map(), sleep=lambda _: None,
    )

    assert account.authority_id == "4958"
    assert account.fields["account_id"] == "SAND_48477"
    assert account.fields["state"] == "TX"
    # account info written before the boundary
    assert client.put_body["attributes"]["state"] == "TX"
    assert client.put_body["attributes"]["country"] == "USA"
    assert client.put_body["account_id"] == "SAND_48477"
    # and the full provisioning ran
    assert account.provision.integration.id == "5223"
    assert account.provision.capabilities is not None


def test_account_info_runs_before_the_jurisdiction(monkeypatch):
    from logic import workflows
    import logic.places as places_mod
    monkeypatch.setattr(places_mod, "resolve_place", lambda q, **kw: fake_place())

    client = PlaceAwareClient()
    workflows.create_sandbox_account(
        client, "48477", authority="4958",
        standard=standard_map(), sleep=lambda _: None,
    )
    order = [f"{m} {p}" for m, p in client.calls]
    put = order.index("PUT /v1/andromeda/authorities/4958")
    jur = order.index("POST /v1/andromeda/authorities/4958/jurisdictions")
    assert put < jur


def test_the_posted_polygon_is_the_trimmed_one(monkeypatch):
    from logic import workflows
    import logic.places as places_mod
    monkeypatch.setattr(places_mod, "resolve_place", lambda q, **kw: fake_place())

    client = PlaceAwareClient()
    workflows.create_sandbox_account(
        client, "48477", authority="4958",
        standard=standard_map(), sleep=lambda _: None,
    )
    posted = client.jurisdiction_body["exact_polygon"]
    props = posted["features"][0]["properties"]
    assert set(props) == {"name", "state", "geoid", "boundary_type"}
    assert "disclaimer" not in str(posted)


def test_account_id_can_be_overridden(monkeypatch):
    from logic import workflows
    import logic.places as places_mod
    monkeypatch.setattr(places_mod, "resolve_place", lambda q, **kw: fake_place())

    client = PlaceAwareClient()
    account = workflows.create_sandbox_account(
        client, "48477", authority="4958", account_id="CUSTOM_1",
        standard=standard_map(), sleep=lambda _: None,
    )
    assert account.fields["account_id"] == "CUSTOM_1"
    assert client.put_body["account_id"] == "CUSTOM_1"


def test_account_info_can_be_skipped(monkeypatch):
    from logic import workflows
    import logic.places as places_mod
    monkeypatch.setattr(places_mod, "resolve_place", lambda q, **kw: fake_place())

    client = PlaceAwareClient()
    workflows.create_sandbox_account(
        client, "48477", authority="4958", update_account_info_first=False,
        standard=standard_map(), sleep=lambda _: None,
    )
    assert client.put_body is None