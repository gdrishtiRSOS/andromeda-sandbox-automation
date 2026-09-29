"""Tests for integration creation."""

from __future__ import annotations

import datetime as dt

import pytest

from logic.integrations import (
    ExistsPolicy,
    Integration,
    IntegrationError,
    IntegrationExistsError,
    ProductNotFoundError,
    build_app_name,
    create_integration,
    list_products,
)

AUTHORITY = {
    "id": 4958,
    "name": "gDTest",
    "display_name": "gDTest",
    "account_id": "GD_0209",
}

PRODUCTS = {
    "integration_types": [
        {"id": 6, "name": "RapidSOS Portal",
         "apigee_product_name": "Capstone-Pre-Production",
         "capability_types": [{"id": 471, "name": "fugro_web_map"}]},
        {"id": 7, "name": "LEI_Sandbox", "apigee_product_name": "LEI",
         "capability_types": []},
    ],
    "total_records": 2,
}


class FakeClient:
    def __init__(self, integrations=None, *, authority=AUTHORITY, products=PRODUCTS):
        self._authority = authority
        self._products = products
        self._integrations = list(integrations or [])
        self.posted = None
        self.gets = []
        self._next_id = 5223

    def get(self, path):
        self.gets.append(path)
        if path.endswith("/integrations"):
            return self._integrations
        if "integration-types" in path:
            return self._products
        return self._authority

    def post(self, path, json):
        self.posted = (path, json)
        record = {
            "id": self._next_id,
            "app_name": json["app_name"],
            "product": json["product"],
            "consumer_key": "KEY123",
            "consumer_secret": "SECRET456",
        }
        self._integrations.append(record)
        return record


def test_app_name_follows_the_runbook_convention():
    assert build_app_name("gDTest", date=dt.date(2026, 9, 22)) == "gDTest Sandbox RSP 2026-09-22"


def test_creates_with_derived_name_and_owner():
    client = FakeClient()
    integration = create_integration(client, "4958")

    path, body = client.posted
    assert path == "/v1/andromeda/authorities/4958/integrations"
    assert body["product"] == "RapidSOS Portal"
    assert body["owner"] == "gDTest"          # the authority name, not an email
    assert body["app_name"].startswith("gDTest Sandbox RSP ")
    assert integration.id == "5223"
    assert integration.created


def test_consumer_secret_is_returned_to_the_caller():
    client = FakeClient()
    integration = create_integration(client, "4958")
    assert integration.consumer_key == "KEY123"
    assert integration.consumer_secret == "SECRET456"


def test_explicit_values_win_and_skip_the_authority_lookup():
    client = FakeClient()
    create_integration(
        client, "4958", app_name="Custom Name", owner="someone", validate_product=False
    )
    _, body = client.posted
    assert body == {"app_name": "Custom Name", "product": "RapidSOS Portal", "owner": "someone"}
    assert not any(p == "/v1/andromeda/authorities/4958" for p in client.gets)


def test_authority_name_can_be_supplied_to_avoid_a_request():
    client = FakeClient()
    create_integration(client, "4958", authority_name="Elsewhere", validate_product=False)
    _, body = client.posted
    assert body["owner"] == "Elsewhere"
    assert body["app_name"].startswith("Elsewhere Sandbox RSP ")
    assert not any(p == "/v1/andromeda/authorities/4958" for p in client.gets)


def test_duplicate_app_name_raises_by_default():
    name = build_app_name("gDTest")
    client = FakeClient([{"id": 5157, "app_name": name, "product": "RapidSOS Portal"}])
    with pytest.raises(IntegrationExistsError) as exc:
        create_integration(client, "4958")
    assert exc.value.existing.id == "5157"
    assert client.posted is None


def test_duplicate_can_be_reused():
    name = build_app_name("gDTest")
    client = FakeClient([{"id": 5157, "app_name": name, "product": "RapidSOS Portal"}])
    integration = create_integration(client, "4958", if_exists=ExistsPolicy.REUSE)
    assert integration.id == "5157"
    assert not integration.created
    assert client.posted is None


def test_duplicate_can_be_forced():
    name = build_app_name("gDTest")
    client = FakeClient([{"id": 5157, "app_name": name, "product": "RapidSOS Portal"}])
    integration = create_integration(client, "4958", if_exists=ExistsPolicy.CREATE)
    assert integration.id == "5223"
    assert client.posted is not None


def test_unknown_product_is_rejected_before_posting():
    client = FakeClient()
    with pytest.raises(ProductNotFoundError) as exc:
        create_integration(client, "4958", product="Nonexistent")
    assert "RapidSOS Portal" in exc.value.available
    assert client.posted is None


def test_apigee_name_is_not_accepted_as_the_product():
    """Capstone-Pre-Production is the internal name; the API wants the display name."""
    client = FakeClient()
    with pytest.raises(ProductNotFoundError):
        create_integration(client, "4958", product="Capstone-Pre-Production")


def test_dry_run_posts_nothing():
    client = FakeClient()
    integration = create_integration(client, "4958", dry_run=True)
    assert client.posted is None
    assert integration.id == ""
    assert integration.app_name.startswith("gDTest Sandbox RSP ")


def test_authority_without_a_name_is_an_error():
    client = FakeClient(authority={"id": 4958})
    with pytest.raises(IntegrationError):
        create_integration(client, "4958")


def test_list_products_keys_by_display_name():
    products = list_products(FakeClient())
    assert set(products) == {"RapidSOS Portal", "LEI_Sandbox"}
    assert products["RapidSOS Portal"]["apigee_product_name"] == "Capstone-Pre-Production"


def test_integration_str_is_readable():
    integration = Integration(id="5223", app_name="gDTest Sandbox RSP 2026-09-22",
                              product="RapidSOS Portal")
    assert str(integration) == "'gDTest Sandbox RSP 2026-09-22' (id 5223, RapidSOS Portal)"
