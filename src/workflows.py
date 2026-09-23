"""Multi-step flows composed from the single-purpose modules.

Runbook steps 4 and 7 are separate in the manual process only because a human
does other work in between. For automation they belong together: a
jurisdiction that is never published does nothing.

The modules stay independent -- `jurisdictions` and `revisions` know nothing
of each other -- and this is where they are combined.

`create_sandbox_account` is the top of the stack: a typed place name in, a
configured sandbox account out. It is the only place that knows the runbook's
steps belong in one sequence.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from andromeda.capabilities import (
    CapabilityDriftError,
    CapabilityKey,
    CapabilityReport,
    CapabilityWriteError,
    DriftPolicy,
    apply_standard_capabilities,
)
from andromeda.integrations import (
    DEFAULT_PRODUCT,
    ExistsPolicy,
    Integration,
    IntegrationError,
    create_integration,
)
from andromeda.jurisdictions import (
    IngressStatus,
    Jurisdiction,
    attach_jurisdiction,
    list_jurisdictions,
    load_geojson,
    normalize_geojson,
    validate_geojson,
)
from andromeda.revisions import (
    PendingRevision,
    RevisionError,
    RevisionResult,
    activate_jurisdiction,
    get_pending,
)

log = logging.getLogger(__name__)

__all__ = [
    "AttachResult",
    "JurisdictionNotActiveError",
    "NotInPendingBatchError",
    "PartialActivationError",
    "PartialProvisionError",
    "ProvisionResult",
    "SandboxAccount",
    "WorkflowClient",
    "attach_and_activate",
    "confirm_jurisdiction_active",
    "create_sandbox_account",
    "provision_authority",
]


class PartialActivationError(Exception):
    """The jurisdiction was created but publishing it failed.

    Carries the jurisdiction so the caller knows what exists and can retry the
    activation alone rather than creating a second boundary.
    """

    def __init__(self, jurisdiction: Jurisdiction, cause: Exception):
        self.jurisdiction = jurisdiction
        self.cause = cause
        super().__init__(
            f"jurisdiction {jurisdiction.id} was created but not activated: {cause}. "
            f"Retry the activation alone once the cause is resolved."
        )


class NotInPendingBatchError(Exception):
    """The new jurisdiction did not appear in the pending revision."""

    def __init__(self, jurisdiction: Jurisdiction, pending: PendingRevision):
        self.jurisdiction = jurisdiction
        self.pending = pending
        super().__init__(
            f"jurisdiction {jurisdiction.id} was created but is not in pending "
            f"revision {pending.id}. Publishing would not activate it."
        )


@runtime_checkable
class WorkflowClient(Protocol):
    def get(self, path: str) -> Any: ...

    def post(self, path: str, json: Mapping[str, Any] | None = None) -> Any: ...

    def patch(self, path: str, json: Mapping[str, Any]) -> Any: ...


@dataclass(frozen=True)
class AttachResult:
    jurisdiction: Jurisdiction
    revision: RevisionResult | None = None

    @property
    def activated(self) -> bool:
        return self.revision is not None

    def summary(self) -> str:
        if not self.activated:
            return f"{self.jurisdiction} -- not activated"
        return (
            f"jurisdiction {self.jurisdiction.id} attached and activated via "
            f"revision {self.revision.published_revision_id} "
            f"(number {self.revision.revision_number})"
        )


def attach_and_activate(
    client: WorkflowClient,
    authority_id: str,
    geojson_path: str | Path | None = None,
    *,
    polygon: Mapping[str, Any] | None = None,
    activate: bool = True,
    revision_number: int | None = None,
    revision_date: dt.date | str | None = None,
    allow_other_authorities: bool = False,
    retry_on_conflict: bool = True,
    dry_run: bool = False,
) -> AttachResult:
    """Attach a boundary and publish the revision that makes it live.

    Runbook steps 4 and 7 in one call: create the jurisdiction as Verified, set
    it to Pending, then create and publish a revision.

    The pending revision is read *after* the jurisdiction is created, since
    creating one adds it to the open batch. The new jurisdiction is confirmed
    to be in that batch before anything is published.

    Raises
    ------
    InvalidGeometryError
        The geometry is unusable. Nothing is created.
    NotInPendingBatchError
        The jurisdiction was created but is absent from the pending batch.
    PartialActivationError
        The jurisdiction was created but publishing failed -- for example
        because another authority's changes are in the batch. The jurisdiction
        still exists; activate it separately once resolved.
    """
    # Validate before touching anything, so a bad file costs nothing.
    if polygon is None:
        if geojson_path is None:
            from andromeda.jurisdictions import JurisdictionError
            raise JurisdictionError("pass geojson_path or polygon")
        polygon = load_geojson(geojson_path)
    else:
        polygon = normalize_geojson(polygon)
        validate_geojson(polygon)

    jurisdiction = attach_jurisdiction(
        client, authority_id, polygon=polygon, dry_run=dry_run
    )

    if not activate:
        log.info("activation skipped; publish a revision to make it live")
        return AttachResult(jurisdiction=jurisdiction)

    if dry_run:
        log.info("dry run: would then create and publish a revision")
        return AttachResult(jurisdiction=jurisdiction)

    # Read the batch now -- creating the jurisdiction put it there.
    pending = get_pending(client)
    in_batch = any(
        str(entry.get("id")) == jurisdiction.id
        for entry in pending.entries_for(authority_id)
    )
    if not in_batch:
        raise NotInPendingBatchError(jurisdiction, pending)

    log.info(
        "jurisdiction %s is in pending revision %s; publishing",
        jurisdiction.id, pending.id,
    )

    try:
        revision = activate_jurisdiction(
            client, authority_id,
            revision_number=revision_number,
            revision_date=revision_date,
            allow_other_authorities=allow_other_authorities,
            retry_on_conflict=retry_on_conflict,
            pending=pending,
        )
    except RevisionError as exc:
        raise PartialActivationError(jurisdiction, exc) from exc

    return AttachResult(jurisdiction=jurisdiction, revision=revision)


class JurisdictionNotActiveError(Exception):
    """The jurisdiction did not reach Active within the allowed time."""

    def __init__(self, jurisdiction: Jurisdiction, waited: float):
        self.jurisdiction = jurisdiction
        self.waited = waited
        super().__init__(
            f"jurisdiction {jurisdiction.id} is still {jurisdiction.ingress_label} "
            f"after {waited:.0f}s. Alerts capabilities depend on an active "
            f"jurisdiction, so continuing may fail."
        )


class PartialProvisionError(Exception):
    """Provisioning stopped part-way. Carries what was completed.

    Every step before the failure really happened, so a retry should skip
    them rather than creating a second jurisdiction or integration.
    """

    def __init__(self, result: "ProvisionResult", failed_step: str, cause: Exception):
        self.result = result
        self.failed_step = failed_step
        self.cause = cause
        done = ", ".join(result.steps) or "nothing"
        super().__init__(
            f"provisioning failed at {failed_step}: {cause}\nCompleted: {done}"
        )


@dataclass
class ProvisionResult:
    authority_id: str
    jurisdiction: Jurisdiction | None = None
    revision: RevisionResult | None = None
    integration: Integration | None = None
    capabilities: CapabilityReport | None = None
    steps: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"authority {self.authority_id}"]
        if self.jurisdiction and self.jurisdiction.id:
            parts.append(f"jurisdiction {self.jurisdiction.id}")
        if self.revision:
            parts.append(f"revision {self.revision.revision_number}")
        if self.integration and self.integration.id:
            parts.append(f"integration {self.integration.id}")
        if self.capabilities:
            skipped = len(self.capabilities.alerts_skipped)
            parts.append(
                f"{len(self.capabilities.changed)} capability change(s)"
                + (f", {skipped} alerts skipped" if skipped else "")
            )
        return " | ".join(parts)


def confirm_jurisdiction_active(
    client: WorkflowClient,
    authority_id: str,
    jurisdiction_id: str,
    *,
    attempts: int = 5,
    delay: float = 2.0,
    require_active: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> Jurisdiction | None:
    """Read the jurisdiction back and wait for it to reach Active.

    Publishing returns 202 and the status change is not instant, so this polls.
    The documented lifecycle is 1 Verified -> 2 Pending -> 3 Active.

    Returns the jurisdiction as last seen, or None if it is not listed at all.
    With `require_active`, raises rather than returning a non-active one.
    """
    seen: Jurisdiction | None = None
    waited = 0.0

    for attempt in range(1, attempts + 1):
        listed = list_jurisdictions(client, authority_id)
        seen = next((j for j in listed if j.id == str(jurisdiction_id)), None)

        if seen is None:
            log.warning(
                "jurisdiction %s is not listed on authority %s",
                jurisdiction_id, authority_id,
            )
            return None

        if seen.ingress_status == int(IngressStatus.ACTIVE):
            log.info("jurisdiction %s is Active", seen.id)
            return seen

        if attempt < attempts:
            log.info(
                "jurisdiction %s is %s, waiting %.0fs (attempt %d/%d)",
                seen.id, seen.ingress_label, delay, attempt, attempts,
            )
            sleep(delay)
            waited += delay

    if require_active:
        raise JurisdictionNotActiveError(seen, waited)

    log.warning(
        "jurisdiction %s is %s, not Active, after %.0fs -- continuing anyway",
        seen.id, seen.ingress_label, waited,
    )
    return seen


def provision_authority(
    client: WorkflowClient,
    authority_id: str,
    geojson_path: str | Path | None = None,
    *,
    polygon: Mapping[str, Any] | None = None,
    skip_jurisdiction: bool = False,
    app_name: str | None = None,
    product: str = DEFAULT_PRODUCT,
    if_exists: ExistsPolicy = ExistsPolicy.ERROR,
    standard: Mapping[CapabilityKey, tuple[bool, bool]] | None = None,
    drift_policy: DriftPolicy = DriftPolicy.WARN,
    revision_number: int | None = None,
    revision_date: dt.date | str | None = None,
    allow_other_authorities: bool = False,
    confirm_attempts: int = 5,
    confirm_delay: float = 2.0,
    require_active: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    dry_run: bool = False,
) -> ProvisionResult:
    """Runbook steps 4, 7, 5 and 6 in order.

    1. Attach the boundary and publish the revision that activates it.
    2. Confirm the jurisdiction reached Active.
    3. Create the integration.
    4. Apply the standard capability set.

    The jurisdiction comes first deliberately: alerts capabilities depend on
    an active jurisdiction, so configuring capabilities beforehand means the
    alerts fallback would silently drop them.

    Use `skip_jurisdiction` for an authority whose boundary is already live.

    Raises
    ------
    PartialProvisionError
        A step failed. The error carries a ProvisionResult describing
        everything that did complete, so a retry can skip it.
    """
    result = ProvisionResult(authority_id=str(authority_id))

    # --- steps 4 and 7 --------------------------------------------------
    if not skip_jurisdiction:
        try:
            attached = attach_and_activate(
                client, authority_id, geojson_path,
                polygon=polygon,
                revision_number=revision_number,
                revision_date=revision_date,
                allow_other_authorities=allow_other_authorities,
                dry_run=dry_run,
            )
        except PartialActivationError as exc:
            result.jurisdiction = exc.jurisdiction
            result.steps.append("jurisdiction created")
            raise PartialProvisionError(result, "activating the jurisdiction", exc) from exc
        except Exception as exc:
            raise PartialProvisionError(result, "attaching the jurisdiction", exc) from exc

        result.jurisdiction = attached.jurisdiction
        result.revision = attached.revision
        result.steps.append("jurisdiction created")
        if attached.activated:
            result.steps.append("revision published")

        # --- confirm before depending on it ------------------------------
        if not dry_run and attached.activated:
            try:
                confirmed = confirm_jurisdiction_active(
                    client, authority_id, attached.jurisdiction.id,
                    attempts=confirm_attempts, delay=confirm_delay,
                    require_active=require_active, sleep=sleep,
                )
            except JurisdictionNotActiveError as exc:
                raise PartialProvisionError(
                    result, "confirming the jurisdiction is active", exc
                ) from exc
            if confirmed is not None:
                result.jurisdiction = confirmed
            result.steps.append("jurisdiction confirmed")
    else:
        log.info("skipping the jurisdiction; assuming it is already live")

    # --- step 5 ---------------------------------------------------------
    try:
        integration = create_integration(
            client, authority_id,
            app_name=app_name,
            product=product,
            if_exists=if_exists,
            dry_run=dry_run,
        )
    except Exception as exc:
        raise PartialProvisionError(result, "creating the integration", exc) from exc

    result.integration = integration
    result.steps.append(f"integration {integration.id or '(dry run)'} created")

    if dry_run:
        log.info("dry run: would then apply the standard capability set")
        return result

    # --- step 6 ---------------------------------------------------------
    try:
        report = apply_standard_capabilities(
            client, authority_id, integration.id,
            standard=standard,
            drift_policy=drift_policy,
        )
    except Exception as exc:
        raise PartialProvisionError(result, "applying capabilities", exc) from exc

    result.capabilities = report
    result.steps.append(f"{len(report.changed)} capability change(s) applied")
    if report.alerts_skipped:
        result.steps.append(f"{len(report.alerts_skipped)} alerts skipped")

    log.info("provisioning complete: %s", result.summary())
    return result


# --------------------------------------------------- place -> sandbox account


@dataclass
class SandboxAccount:
    """What a single "create me an account for X" request produced."""

    query: str
    place: Any                                   # places.ResolvedPlace
    fields: dict[str, Any]
    authority_id: str | None = None
    provision: ProvisionResult | None = None

    @property
    def created_authority(self) -> bool:
        return self.authority_id is not None

    def summary(self) -> str:
        head = f"{self.query!r} -> {self.place}"
        if self.provision is None:
            return f"{head} (nothing provisioned)"
        return f"{head}\n  {self.provision.summary()}"


def create_sandbox_account(
    client: WorkflowClient,
    query: str,
    *,
    authority: str,
    account_id: str | None = None,
    name_template: str | None = None,
    use_ecc_name: bool = False,
    require_status: bool = True,
    update_account_info_first: bool = True,
    dispatch_type: int | None = None,
    counties: Any = None,
    places_table: Any = None,
    **provision_kwargs: Any,
) -> SandboxAccount:
    """Create a sandbox account for a named place.

    `query` is what a person typed -- "Lincoln, Nebraska", "Washington
    County, TX", or a 5-digit county GEOID. It resolves to a county boundary,
    which drives everything else: the Account Info state, the generated
    account_id, and the jurisdiction polygon.

    `authority` is the existing authority to configure, by name or numeric id.
    The signup wizard (runbook step 1) is not yet automated, so the authority
    must already exist.

    Everything else follows `provision_authority`: boundary, revision,
    integration, capabilities.

    Raises
    ------
    PlaceError and subclasses
        The query could not be resolved to exactly one county.
    PartialProvisionError
        A step failed; the error carries what completed.
    """
    # imported here: geopandas is only needed when a place is actually resolved
    from andromeda.places import account_fields, resolve_place, to_andromeda_polygon
    from andromeda.authorities import resolve_authority_id, update_account_info

    place = resolve_place(
        query, require_status=require_status, counties=counties, places=places_table
    )
    log.info("resolved %r to %s", query, place)

    field_kwargs: dict[str, Any] = {"use_ecc_name": use_ecc_name}
    if name_template:
        field_kwargs["name_template"] = name_template
    fields = account_fields(place, **field_kwargs)
    if account_id:
        fields["account_id"] = account_id

    authority_id = resolve_authority_id(client, authority)
    account = SandboxAccount(query=query, place=place, fields=fields,
                             authority_id=authority_id)

    dry_run = provision_kwargs.get("dry_run", False)

    if update_account_info_first:
        report = update_account_info(
            client, authority_id,
            account_id=fields["account_id"],
            country=fields["country"],
            state=fields["state"],
            dispatch_type=dispatch_type,
            dry_run=dry_run,
        )
        log.info("account info: %s", report.summary())

    polygon = to_andromeda_polygon(place)
    account.provision = provision_authority(
        client, authority_id, polygon=polygon, **provision_kwargs
    )
    return account