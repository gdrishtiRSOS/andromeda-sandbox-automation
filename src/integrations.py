"""Create an Andromeda integration on an authority.

Runbook step 5. Given an authority that already exists, add an integration for
a product, generating the app name to the runbook's convention.

    POST /v1/andromeda/authorities/{authorityId}/integrations
    {"app_name": "...", "product": "RapidSOS Portal", "owner": "..."}
    -> 201 {"id": 5223, "app_name": ..., "product": ...,
            "consumer_key": ..., "consumer_secret": ...}

Design notes
------------
* `owner` is the authority's `name` field, not an email address. The UI's
  placeholder text says owner@email.com but it submits the agency name, and
  the server 404s on a value it cannot resolve -- which reads as "integration
  endpoint not found" and sends you looking in the wrong place.
* `product` is the display name from the integration-types catalog
  ("RapidSOS Portal"), not the internal apigee_product_name
  ("Capstone-Pre-Production") and not the numeric id.
* consumer_secret is returned ONCE, at creation. It never appears in the
  integrations list afterwards. If anything downstream needs it, capture it
  from the returned object now.
* Creating an integration is not idempotent -- the API happily makes two with
  the same app_name. `if_exists` controls what happens instead.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_PRODUCT",
    "ExistsPolicy",
    "Integration",
    "IntegrationError",
    "IntegrationExistsError",
    "ProductNotFoundError",
    "AndromedaWriteClient",
    "build_app_name",
    "create_integration",
    "get_authority",
    "list_integrations",
    "list_products",
]

DEFAULT_PRODUCT = "RapidSOS Portal"

#: Runbook step 5.2: authority name, then "Sandbox", "RSP" and the date.
APP_NAME_TEMPLATE = "{authority} Sandbox RSP {date:%Y-%m-%d}"


class ExistsPolicy(str, Enum):
    ERROR = "error"    # refuse -- the default, so reruns cannot quietly duplicate
    REUSE = "reuse"    # return the existing one
    CREATE = "create"  # make another anyway


class IntegrationError(Exception):
    """Base for this module."""


class IntegrationExistsError(IntegrationError):
    def __init__(self, existing: "Integration"):
        self.existing = existing
        super().__init__(
            f"integration {existing.app_name!r} already exists (id {existing.id})"
        )


class ProductNotFoundError(IntegrationError):
    def __init__(self, product: str, available: list[str]):
        self.product = product
        self.available = available
        super().__init__(
            f"no product named {product!r}. Available: {', '.join(sorted(available))}"
        )


@dataclass(frozen=True)
class Integration:
    id: str
    app_name: str
    product: str
    consumer_key: str | None = None
    consumer_secret: str | None = None
    created: bool = False

    @classmethod
    def from_api(cls, data: Mapping[str, Any], *, created: bool = False) -> "Integration":
        return cls(
            id=str(data["id"]),
            app_name=data.get("app_name", ""),
            product=data.get("product", ""),
            consumer_key=data.get("consumer_key"),
            consumer_secret=data.get("consumer_secret"),
            created=created,
        )

    def __str__(self) -> str:
        return f"{self.app_name!r} (id {self.id}, {self.product})"


@runtime_checkable
class AndromedaWriteClient(Protocol):
    """The client surface this module needs. Implementations own auth and
    should raise on non-2xx rather than returning an error body."""

    def get(self, path: str) -> Any: ...

    def post(self, path: str, json: Mapping[str, Any]) -> Any: ...


# ----------------------------------------------------------------- reads


def get_authority(client: AndromedaWriteClient, authority_id: str) -> dict[str, Any]:
    """The authority record. Its `name` is what `owner` must be set to."""
    return client.get(f"/v1/andromeda/authorities/{authority_id}")


def list_integrations(client: AndromedaWriteClient, authority_id: str) -> list[Integration]:
    data = client.get(f"/v1/andromeda/authorities/{authority_id}/integrations")
    return [Integration.from_api(item) for item in data or []]


def list_products(client: AndromedaWriteClient, limit: int = 500) -> dict[str, dict[str, Any]]:
    """Product display name -> its catalog entry."""
    data = client.get(f"/v1/andromeda/integration-types?page=1&limit={limit}")
    entries = data.get("integration_types", []) if isinstance(data, dict) else (data or [])
    return {entry["name"]: entry for entry in entries}


# ----------------------------------------------------------------- naming


def build_app_name(
    authority_name: str,
    *,
    date: dt.date | None = None,
    template: str = APP_NAME_TEMPLATE,
) -> str:
    """Generate the app name to the runbook's convention.

    >>> build_app_name("gDTest", date=dt.date(2026, 9, 22))
    'gDTest Sandbox RSP 2026-09-22'
    """
    return template.format(authority=authority_name, date=date or dt.date.today())


# ----------------------------------------------------------------- write


def create_integration(
    client: AndromedaWriteClient,
    authority_id: str,
    *,
    app_name: str | None = None,
    product: str = DEFAULT_PRODUCT,
    owner: str | None = None,
    authority_name: str | None = None,
    if_exists: ExistsPolicy = ExistsPolicy.ERROR,
    validate_product: bool = True,
    dry_run: bool = False,
) -> Integration:
    """Add an integration to an authority.

    `app_name` and `owner` are both derived from the authority's name when not
    given, which costs one GET unless `authority_name` is supplied.

    Raises
    ------
    IntegrationExistsError
        When an integration with this app_name exists and `if_exists` is ERROR.
    ProductNotFoundError
        When `validate_product` is on and the product is not in the catalog.
    """
    if authority_name is None and (app_name is None or owner is None):
        record = get_authority(client, authority_id)
        authority_name = record.get("name") or record.get("display_name")
        if not authority_name:
            raise IntegrationError(
                f"authority {authority_id} has no name; pass authority_name explicitly"
            )
        log.debug("authority %s is named %r", authority_id, authority_name)

    app_name = app_name or build_app_name(authority_name)
    owner = owner or authority_name

    if validate_product:
        products = list_products(client)
        if product not in products:
            raise ProductNotFoundError(product, list(products))
        entry = products[product]
        log.debug(
            "product %r -> id %s, apigee %r, %d capability types",
            product, entry.get("id"), entry.get("apigee_product_name"),
            len(entry.get("capability_types", [])),
        )

    existing = [i for i in list_integrations(client, authority_id) if i.app_name == app_name]
    if existing:
        if if_exists is ExistsPolicy.ERROR:
            raise IntegrationExistsError(existing[0])
        if if_exists is ExistsPolicy.REUSE:
            log.info("reusing existing integration %s", existing[0])
            return existing[0]
        log.warning(
            "creating a second integration named %r; %d already exist",
            app_name, len(existing),
        )

    body = {"app_name": app_name, "product": product, "owner": owner}

    if dry_run:
        log.info("dry run: would POST %s to authority %s", body, authority_id)
        return Integration(id="", app_name=app_name, product=product)

    data = client.post(f"/v1/andromeda/authorities/{authority_id}/integrations", body)
    integration = Integration.from_api(data, created=True)

    log.info("created integration %s on authority %s", integration, authority_id)
    if integration.consumer_secret:
        log.info(
            "consumer_secret returned for integration %s -- it is not retrievable "
            "later, so store it now if anything needs it", integration.id,
        )
    return integration
