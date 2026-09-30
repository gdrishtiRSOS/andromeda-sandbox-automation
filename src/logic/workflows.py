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

from logic.capabilities import (
    CapabilityDriftError,
    CapabilityKey,
    CapabilityReport,
    CapabilityWriteError,
    DriftPolicy,
    _capabilities_path,
    apply_standard_capabilities,
    load_standard,
)
from logic.authorities import (
    DEFAULT_DISPATCH_TYPE,
    AccountInfoError,
    AccountInfoReport,
    AmbiguousAuthorityError,
    AuthorityNotFoundError,
    find_authority,
    get_authority,
    update_account_info,
)
from logic.integrations import (
    APP_NAME_TEMPLATE,
    DEFAULT_PRODUCT,
    ExistsPolicy,
    Integration,
    IntegrationError,
    create_integration,
    list_integrations,
)
from logic.jurisdictions import (
    IngressStatus,
    Jurisdiction,
    attach_jurisdiction,
    list_jurisdictions,
    load_geojson,
    normalize_geojson,
    validate_geojson,
)
from logic.revisions import (
    OtherAuthoritiesPendingError,
    PendingRevision,
    RevisionError,
    RevisionResult,
    activate_jurisdiction,
    get_pending,
)
from logic.roles import (
    RoleAccessReport,
    RoleClient,
    enable_all_data_sources,
    list_roles,
    snapshot_roles,
)

log = logging.getLogger(__name__)

#: Dispatch type 1, Primary: every sandbox account is set to it in step 2.
PRIMARY_DISPATCH_TYPE = DEFAULT_DISPATCH_TYPE

__all__ = [
    "PRIMARY_DISPATCH_TYPE",
    "CONFIGURE_STEPS",
    "AmbiguousIntegrationError",
    "AccountChanges",
    "AccountIdInUseError",
    "AccountOverview",
    "AttachResult",
    "CHANGE_STEPS",
    "ChangePlan",
    "ChangeRefusedError",
    "ChangeResult",
    "EDITABLE_ACCOUNT_FIELDS",
    "IntegrationSummary",
    "NEW_INTEGRATION",
    "PartialChangeError",
    "SectionPlan",
    "StalePlanError",
    "apply_account_changes",
    "change_next_action",
    "describe_account",
    "plan_account_changes",
    "AuthorityMismatchError",
    "ConfigureError",
    "ConfigureResult",
    "JurisdictionNotActiveError",
    "NewAccountChecks",
    "NoBoundaryError",
    "NotInPendingBatchError",
    "PartialActivationError",
    "PartialConfigureError",
    "PartialProvisionError",
    "ProvisionResult",
    "SandboxAccount",
    "StepOutcome",
    "UnexpectedRevocationError",
    "UnpublishableJurisdictionError",
    "WorkflowClient",
    "attach_and_activate",
    "authority_names",
    "check_new_account",
    "configure_account",
    "confirm_jurisdiction_active",
    "create_sandbox_account",
    "next_action",
    "plan_roles_restore",
    "provision_authority",
    "restore_capabilities",
    "root_cause",
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
            from logic.jurisdictions import JurisdictionError
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


class AccountIdInUseError(Exception):
    """Another authority already has this account_id.

    The field cannot be changed once set, and the STATE_NAMESAND convention
    truncates the county name, so collisions within a state are possible.
    """

    def __init__(self, account_id: str, existing: Mapping[str, Any]):
        self.account_id = account_id
        self.existing = dict(existing)
        super().__init__(
            f"account_id {account_id!r} is already used by authority "
            f"{existing.get('id')} ({existing.get('name')!r}). It cannot be "
            f"changed once set -- pass account_id= to choose another."
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
    before_capabilities: Callable[[Integration, Mapping[str, Any]], None] | None = None,
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

    `before_capabilities` is called with the integration and its live
    capability body just before step 6 writes anything -- the place to take a
    snapshot, since the PATCH replaces the whole list and has no server-side
    undo.

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

    if before_capabilities is not None:
        try:
            live = client.get(_capabilities_path(authority_id, integration.id))
            before_capabilities(integration, live)
        except Exception as exc:
            raise PartialProvisionError(result, "snapshotting capabilities", exc) from exc

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
    check_account_id: bool = True,
    update_account_info_first: bool = True,
    dispatch_type: int | None = -1,          # -1 means "use the module default"
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
    AccountIdInUseError
        Another authority already holds the generated account_id.
    PartialProvisionError
        A step failed; the error carries what completed.
    """
    # imported here: geopandas is only needed when a place is actually resolved
    from logic.places import account_fields, resolve_place, to_andromeda_polygon
    from logic.authorities import (
        DEFAULT_DISPATCH_TYPE, resolve_authority_id, update_account_info,
    )

    if dispatch_type == -1:
        dispatch_type = DEFAULT_DISPATCH_TYPE

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

    # account_id cannot be changed once set, and the STATE_NAMESAND convention
    # truncates the county name -- Washington and Washita in OK both give
    # WASHI. Check before writing something permanent.
    if check_account_id and fields["account_id"]:
        from logic.authorities import (
            AuthorityNotFoundError, find_authority,
        )

        try:
            clash = find_authority(client, account_id=fields["account_id"])
        except AuthorityNotFoundError:
            clash = None
        if clash is not None and str(clash.get("id")) != str(authority_id):
            raise AccountIdInUseError(fields["account_id"], clash)

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


# ------------------------------------------ signed-up account -> configured
#
# Runbook steps 2, 4, 7, 5, 6 and 8 for an account that sign-up (step 1)
# already created. Every step first looks at what exists, so the same call
# both configures a fresh account and resumes a half-configured one.
#
# The refusals that can be judged up front -- someone else's work in the
# revision batch, an ambiguous integration, an unpublishable jurisdiction --
# are checked before the first write, so they leave the account as found.
# A role revocation can only be judged at step 8 (see configure_account).


#: (key, label) for each step, in the order they run.
CONFIGURE_STEPS: tuple[tuple[str, str], ...] = (
    ("account", "Find the account"),
    ("account_info", "Account info"),
    ("boundary", "Boundary"),
    ("revision", "Publish revision"),
    ("integration", "Integration"),
    ("capabilities", "Capabilities"),
    ("roles", "Roles"),
)

_FINISHED = ("done", "skipped")


class ConfigureError(Exception):
    """Base for the refusals `configure_account` raises itself."""


class AuthorityMismatchError(ConfigureError):
    """The authority found is not the one sign-up created."""

    def __init__(self, authority_id: str, expected_org: str, found_org: str):
        self.authority_id = authority_id
        self.expected_org = expected_org
        self.found_org = found_org
        super().__init__(
            f"authority {authority_id} belongs to organization {found_org or '(none)'}, "
            f"but sign-up created organization {expected_org}. Refusing to configure "
            f"someone else's account."
        )


class NoBoundaryError(ConfigureError):
    def __init__(self, authority_id: str):
        self.authority_id = authority_id
        super().__init__(
            f"authority {authority_id} has no jurisdiction yet and no boundary was "
            f"given. Choose a place or a .geojson file."
        )


class UnpublishableJurisdictionError(ConfigureError):
    """A jurisdiction exists but is neither Active nor in the pending batch."""

    def __init__(self, authority_id: str, jurisdictions: list[Jurisdiction]):
        self.authority_id = authority_id
        self.jurisdictions = list(jurisdictions)
        listing = ", ".join(f"{j.id} ({j.ingress_label})" for j in jurisdictions)
        super().__init__(
            f"authority {authority_id} has jurisdiction(s) {listing}, none Active and "
            f"none in the pending revision, so publishing would not activate them. "
            f"Check them in Andromeda rather than creating another."
        )


class AmbiguousIntegrationError(ConfigureError):
    """Several existing integrations look like this account's."""

    def __init__(self, authority_id: str, matches: list[Integration]):
        self.authority_id = authority_id
        self.matches = list(matches)
        super().__init__(
            f"authority {authority_id} already has {len(matches)} integrations that "
            f"look generated: {', '.join(str(m) for m in matches)}. Not guessing "
            f"which one to configure."
        )


class UnexpectedRevocationError(ConfigureError):
    """Granting the standard permissions would also take some away."""

    def __init__(self, report: RoleAccessReport):
        self.report = report
        listing = "; ".join(
            f"{role} would lose {', '.join(names)}"
            for role, names in sorted(report.revoked.items())
        )
        super().__init__(
            f"organization {report.organization_id}: {listing}. The portal's "
            f"permission catalog lists {report.catalog_size} permission(s) and "
            f"these are not among them. Refusing rather than removing them; the "
            f"roles were not changed."
        )


@dataclass(frozen=True)
class StepOutcome:
    step: str                  # a key from CONFIGURE_STEPS
    status: str                # running | done | skipped | failed | note
    detail: str = ""


@dataclass
class ConfigureResult:
    authority: str
    authority_id: str | None = None
    authority_name: str | None = None
    organization_id: str | None = None
    account_info: AccountInfoReport | None = None
    jurisdiction: Jurisdiction | None = None
    revision: RevisionResult | None = None
    provision: ProvisionResult | None = None
    roles: RoleAccessReport | None = None
    snapshots: dict[str, str] = field(default_factory=dict)
    steps: list[StepOutcome] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def integration(self) -> Integration | None:
        return self.provision.integration if self.provision else None

    @property
    def capabilities(self) -> CapabilityReport | None:
        return self.provision.capabilities if self.provision else None

    def status_of(self, step: str) -> str:
        """The step's latest status, or "not run"."""
        for outcome in reversed(self.steps):
            if outcome.step == step and outcome.status != "note":
                return outcome.status
        return "not run"

    @property
    def completed(self) -> list[str]:
        return [key for key, _ in CONFIGURE_STEPS if self.status_of(key) in _FINISHED]

    @property
    def not_done(self) -> list[str]:
        return [key for key, _ in CONFIGURE_STEPS if self.status_of(key) not in _FINISHED]

    def existing(self) -> list[str]:
        """What is really there now, in words -- for telling a person."""
        out = []
        if self.authority_id:
            out.append(f"authority {self.authority_name!r} (id {self.authority_id}), "
                       f"organization {self.organization_id}")
        if self.account_info and self.account_info.applied:
            out.append("account info updated")
        if self.jurisdiction and self.jurisdiction.id:
            out.append(f"jurisdiction {self.jurisdiction.id} "
                       f"({self.jurisdiction.ingress_label})")
        if self.revision:
            out.append(f"revision {self.revision.revision_number} published")
        if self.integration and self.integration.id:
            out.append(f"integration {self.integration}")
        if self.capabilities and self.capabilities.applied:
            out.append(f"{len(self.capabilities.changed)} capability change(s) applied")
        if self.roles and self.roles.applied:
            out.append("portal roles updated")
        return out

    def summary(self) -> str:
        parts = [f"authority {self.authority_name or self.authority} "
                 f"(id {self.authority_id or '?'})"]
        if self.jurisdiction and self.jurisdiction.id:
            parts.append(f"jurisdiction {self.jurisdiction.id} {self.jurisdiction.ingress_label}")
        if self.integration and self.integration.id:
            parts.append(f"integration {self.integration.id}")
        if self.capabilities:
            skipped = len(self.capabilities.alerts_skipped)
            parts.append(f"{len(self.capabilities.changed)} capability change(s)"
                         + (f", {skipped} alerts skipped" if skipped else ""))
        if self.roles:
            parts.append(self.roles.summary())
        return " | ".join(parts)


class PartialConfigureError(Exception):
    """`configure_account` stopped. Carries everything that did happen."""

    def __init__(self, result: ConfigureResult, failed_step: str, cause: Exception):
        self.result = result
        self.failed_step = failed_step
        self.cause = cause
        done = ", ".join(result.completed) or "nothing"
        super().__init__(f"stopped at {failed_step}: {cause}\nCompleted: {done}")

    @property
    def refused(self) -> bool:
        """A deliberate refusal rather than something breaking."""
        refusals = (ConfigureError, OtherAuthoritiesPendingError, AmbiguousAuthorityError)
        return any(isinstance(c, refusals) for c in _cause_chain(self.cause))


def _cause_chain(exc: BaseException | None) -> list[BaseException]:
    """exc, then whatever it wraps, via `.cause` or `__cause__`."""
    chain: list[BaseException] = []
    while exc is not None and all(exc is not seen for seen in chain):
        chain.append(exc)
        exc = getattr(exc, "cause", None) or exc.__cause__
    return chain


def root_cause(exc: BaseException) -> BaseException:
    return _cause_chain(exc)[-1]


def next_action(error: PartialConfigureError) -> str:
    """What a person should do about `error`, in one or two sentences."""
    from logic.authorities import UnknownCountryError, UnknownStateError

    for cause in _cause_chain(error.cause):
        if isinstance(cause, OtherAuthoritiesPendingError):
            return ("Publishing a revision is environment-wide, and the pending "
                    "revision holds other authorities' changes. Nothing of yours was "
                    "published. Ask the team who owns those changes; once they have "
                    "published them, press Resume.")
        if isinstance(cause, AuthorityNotFoundError):
            return ("The authority is not visible in Andromeda yet. Sign-up creates "
                    "it in the background; wait a minute, then press Resume.")
        if isinstance(cause, AmbiguousAuthorityError):
            return ("Several authorities have this name. Resume with the numeric id "
                    "of the right one.")
        if isinstance(cause, AuthorityMismatchError):
            return ("The authority with this name belongs to a different organization "
                    "from the one sign-up created. Check the name, then Resume with "
                    "the numeric id.")
        if isinstance(cause, NoBoundaryError):
            return "Choose a place or upload a .geojson boundary, then press Resume."
        if isinstance(cause, UnpublishableJurisdictionError):
            return ("Look at the authority's jurisdictions in Andromeda before retrying. "
                    "Creating a second boundary would be wrong.")
        if isinstance(cause, AmbiguousIntegrationError):
            return ("Remove or rename the extra integrations in Andromeda so only one "
                    "remains, then press Resume.")
        if isinstance(cause, UnexpectedRevocationError):
            return ("The roles were not changed; every earlier step is done. The "
                    "portal's permission list is missing permissions the roles already "
                    "hold, so applying would remove them. Check Admin -> Role and "
                    "Access in the portal; if the data sources appear there later, "
                    "press Resume -- only the roles step will run.")
        if isinstance(cause, (UnknownCountryError, UnknownStateError)):
            return "Correct the country or state code, then press Resume."
        if isinstance(cause, CapabilityWriteError):
            return ("The server did not store the capabilities as requested. Undo them "
                    "from the snapshot, then check the integration in Andromeda.")
    return ("Fix the cause shown, then press Resume. Steps that already completed "
            "are detected and skipped.")


def _generated_name_prefix(authority_name: str) -> str:
    """The part of a generated app name before the date: '<name> Sandbox RSP '."""
    return APP_NAME_TEMPLATE.partition("{date")[0].format(authority=authority_name)


def _find_authority_id(
    client: WorkflowClient,
    authority: str,
    organization_id: str | None,
    *,
    attempts: int,
    delay: float,
    sleep: Callable[[float], None],
) -> str:
    """Resolve a name to an id, waiting for a just-created authority to appear.

    A name shared by several authorities is resolved only when exactly one of
    them belongs to `organization_id` -- an exact key, not a guess.
    """
    text = str(authority).strip()
    if text.isdigit():
        return text

    for attempt in range(1, attempts + 1):
        try:
            return str(find_authority(client, text)["id"])
        except AuthorityNotFoundError:
            if attempt == attempts:
                raise
            log.info("authority %r not visible yet, waiting %.0fs (attempt %d/%d)",
                     text, delay, attempt, attempts)
            sleep(delay)
        except AmbiguousAuthorityError as exc:
            if organization_id:
                mine = [m for m in exc.matches
                        if str(m.get("organization_id")) == str(organization_id)]
                if len(mine) == 1:
                    log.info("%d authorities are named %r; using %s, the one in "
                             "organization %s", len(exc.matches), text,
                             mine[0].get("id"), organization_id)
                    return str(mine[0]["id"])
            raise
    raise AssertionError("unreachable")


def configure_account(
    andromeda: WorkflowClient,
    portal: RoleClient,
    authority: str,
    *,
    organization_id: str | None = None,
    polygon: Mapping[str, Any] | None = None,
    account_id: str | None = None,
    country: str | None = None,
    state: str | None = None,
    dispatch_type: int | None = PRIMARY_DISPATCH_TYPE,
    standard: Mapping[CapabilityKey, tuple[bool, bool]] | None = None,
    product: str = DEFAULT_PRODUCT,
    save_snapshot: Callable[[str, Mapping[str, Any]], str | None] | None = None,
    on_step: Callable[[StepOutcome], None] | None = None,
    find_attempts: int = 5,
    find_delay: float = 3.0,
    confirm_attempts: int = 5,
    confirm_delay: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> ConfigureResult:
    """Everything after sign-up: runbook steps 2, 4, 7, 5, 6 and 8.

    `andromeda` and `portal` are clients for the two hosts; roles live on the
    portal. `authority` is the name sign-up gave it (the agency name) or its
    numeric id. `organization_id` is what sign-up returned: it picks the right
    authority when the name is shared, and guards against configuring an
    authority from a different organization.

    Safe to call again after a failure. Each step looks first:

    * account info is only written where it differs; a set account_id is kept;
    * an Active jurisdiction is left alone; one waiting in the pending batch is
      published rather than duplicated; only with none is `polygon` attached;
    * a previously generated integration is reused, whatever day it was made;
    * capabilities and roles are only written where they differ.

    `save_snapshot(label, body)` is called with the capabilities and roles as
    they are, before each is written, and returns where it stored them.
    `on_step` receives each StepOutcome as it happens.

    Raises
    ------
    PartialConfigureError
        A step failed or refused. Carries a ConfigureResult saying what exists;
        `next_action(error)` says what to do.
    """
    result = ConfigureResult(authority=str(authority))

    def record(step: str, status: str, detail: str = "") -> None:
        outcome = StepOutcome(step, status, detail)
        result.steps.append(outcome)
        if status == "note":
            result.notes.append(detail)
        if on_step is not None:
            on_step(outcome)

    def fail(step: str, exc: Exception) -> PartialConfigureError:
        record(step, "failed", str(root_cause(exc)))
        return PartialConfigureError(result, step, exc)

    def snapshot(label: str, body: Mapping[str, Any]) -> None:
        if save_snapshot is None:
            return
        where = save_snapshot(label, body)
        if where:
            result.snapshots[label] = where

    # --- look before writing anything ------------------------------------
    record("account", "running", "looking up the authority and checking both tokens")
    try:
        authority_id = _find_authority_id(
            andromeda, authority, organization_id,
            attempts=find_attempts, delay=find_delay, sleep=sleep,
        )
        found = get_authority(andromeda, authority_id)
        found_org = str(found.get("organization_id") or "")
        if organization_id and found_org != str(organization_id):
            raise AuthorityMismatchError(authority_id, str(organization_id), found_org)
        if not found_org:
            raise ConfigureError(
                f"authority {authority_id} has no organization_id, so its portal "
                f"roles cannot be found"
            )
        result.authority_id = authority_id
        result.authority_name = str(found.get("name") or found.get("display_name")
                                    or authority)
        result.organization_id = found_org

        # a cheap read on the portal, so a bad token fails before any write
        list_roles(portal, found_org)

        if polygon is not None:
            polygon = normalize_geojson(polygon)
            validate_geojson(polygon)

        jurisdictions = list_jurisdictions(andromeda, authority_id)
        pending = get_pending(andromeda)
        existing_integrations = list_integrations(andromeda, authority_id)
    except Exception as exc:
        raise fail("account", exc) from exc

    record("account", "done",
           f"{result.authority_name!r} (id {authority_id}), organization {found_org}")

    # Roles are deliberately NOT planned here. The portal's permission catalog
    # only lists an organization's data sources once its capabilities exist,
    # so before step 6 a new account's default permissions (CONNECTED_SITES,
    # SXM, ...) look like revocations. They are checked at step 8 instead.

    active = [j for j in jurisdictions if j.ingress_status == int(IngressStatus.ACTIVE)]
    if active:
        boundary_plan = "active"
    elif jurisdictions:
        if not pending.entries_for(authority_id):
            raise fail("boundary",
                       UnpublishableJurisdictionError(authority_id, jurisdictions))
        boundary_plan = "publish"
    else:
        if polygon is None:
            raise fail("boundary", NoBoundaryError(authority_id))
        boundary_plan = "attach"

    if boundary_plan != "active":
        # Checked here, before anything is created: attach_and_activate only
        # finds out after the jurisdiction exists, leaving it in the shared
        # batch for someone else's publish to activate.
        others = [a for a in pending.authority_ids if a != str(authority_id)]
        if others:
            raise fail("revision", OtherAuthoritiesPendingError(authority_id, others))

    prefix = _generated_name_prefix(result.authority_name)
    reusable = [i for i in existing_integrations
                if i.app_name.startswith(prefix) and i.product in ("", product)]
    if len(reusable) > 1:
        raise fail("integration", AmbiguousIntegrationError(authority_id, reusable))

    # --- step 2 ------------------------------------------------------------
    record("account_info", "running")
    try:
        report = update_account_info(
            andromeda, authority_id,
            account_id=account_id, country=country, state=state,
            dispatch_type=dispatch_type,
        )
    except Exception as exc:
        raise fail("account_info", exc) from exc
    result.account_info = report
    for field_name, reason in sorted(report.skipped.items()):
        record("account_info", "note", f"{field_name} left unchanged: {reason}")
    if report.is_noop:
        record("account_info", "skipped", "already up to date")
    else:
        record("account_info", "done", report.summary())

    # --- steps 4 and 7 -----------------------------------------------------
    jurisdiction_id: str | None = None
    if boundary_plan == "active":
        result.jurisdiction = active[0]
        record("boundary", "skipped", f"jurisdiction {active[0].id} is already Active")
        record("revision", "skipped", "nothing to publish")
        if polygon is not None:
            record("boundary", "note",
                   "the account already has an active boundary; the one supplied was not used")
    elif boundary_plan == "publish":
        waiting = jurisdictions[0]
        result.jurisdiction = waiting
        jurisdiction_id = waiting.id
        record("boundary", "skipped",
               f"jurisdiction {waiting.id} already exists ({waiting.ingress_label})")
        record("revision", "running")
        try:
            result.revision = activate_jurisdiction(andromeda, authority_id)
        except Exception as exc:
            raise fail("revision", exc) from exc
    else:
        record("boundary", "running", "creating the boundary, then publishing it")
        try:
            attached = attach_and_activate(andromeda, authority_id, polygon=polygon)
        except (PartialActivationError, NotInPendingBatchError) as exc:
            result.jurisdiction = exc.jurisdiction
            record("boundary", "done", f"jurisdiction {exc.jurisdiction.id} created")
            raise fail("revision", exc) from exc
        except Exception as exc:
            raise fail("boundary", exc) from exc
        result.jurisdiction = attached.jurisdiction
        result.revision = attached.revision
        jurisdiction_id = attached.jurisdiction.id
        record("boundary", "done", f"jurisdiction {jurisdiction_id} created")

    if jurisdiction_id is not None:
        try:
            confirmed = confirm_jurisdiction_active(
                andromeda, authority_id, jurisdiction_id,
                attempts=confirm_attempts, delay=confirm_delay, sleep=sleep,
            )
        except Exception as exc:
            raise fail("revision", exc) from exc
        if confirmed is not None:
            result.jurisdiction = confirmed
        record("revision", "done",
               f"revision {result.revision.revision_number} published")
        if confirmed is None or confirmed.ingress_status != int(IngressStatus.ACTIVE):
            label = confirmed.ingress_label if confirmed else "not listed"
            record("revision", "note",
                   f"jurisdiction {jurisdiction_id} is still {label}; alerts "
                   f"capabilities depend on it being Active and may be skipped")

    # --- steps 5 and 6 -----------------------------------------------------
    if reusable:
        app_name, if_exists = reusable[0].app_name, ExistsPolicy.REUSE
    else:
        app_name, if_exists = None, ExistsPolicy.ERROR

    def before_capabilities(integration: Integration, live: Mapping[str, Any]) -> None:
        verb = "created" if integration.created else "reused"
        record("integration", "done", f"{verb} {integration}")
        record("capabilities", "running")
        snapshot(f"capabilities-{integration.id}", live)

    record("integration", "running")
    try:
        provisioned = provision_authority(
            andromeda, authority_id,
            skip_jurisdiction=True,
            app_name=app_name,
            product=product,
            if_exists=if_exists,
            standard=standard,
            before_capabilities=before_capabilities,
            sleep=sleep,
        )
    except PartialProvisionError as exc:
        result.provision = exc.result
        step = "integration" if exc.result.integration is None else "capabilities"
        raise fail(step, exc) from exc
    result.provision = provisioned

    caps = provisioned.capabilities
    record("capabilities", "done" if caps.applied else "skipped", caps.summary())
    if caps.alerts_skipped:
        record("capabilities", "note",
               f"{len(caps.alerts_skipped)} alerts capabilities left off -- the "
               f"jurisdiction overlaps one that already has them: "
               f"{', '.join(str(k) for k in caps.alerts_skipped)}")

    # --- step 8 ------------------------------------------------------------
    record("roles", "running")
    try:
        # planned only now, once capabilities exist and the catalog lists them
        again = enable_all_data_sources(portal, found_org, dry_run=True)
        if again.revoked:
            raise UnexpectedRevocationError(again)
        if again.is_noop:
            result.roles = again
        else:
            snapshot(f"roles-{found_org}", snapshot_roles(portal, found_org))
            result.roles = enable_all_data_sources(portal, found_org)
    except Exception as exc:
        raise fail("roles", exc) from exc
    record("roles", "done" if result.roles.applied else "skipped", result.roles.summary())

    log.info("account configured: %s", result.summary())
    return result


# ------------------------------------------------------ checks and undo


@dataclass
class NewAccountChecks:
    """Read-only checks worth making before sign-up creates anything."""

    authority_name: str
    name_taken_by: list[dict[str, Any]] = field(default_factory=list)
    others_pending: dict[str, str] = field(default_factory=dict)   # id -> name

    @property
    def name_is_free(self) -> bool:
        return not self.name_taken_by


def authority_names(client: WorkflowClient, authority_ids: list[str]) -> dict[str, str]:
    """id -> name, so a refusal can say whose work it is protecting."""
    out = {}
    for aid in authority_ids:
        try:
            found = get_authority(client, aid)
            out[str(aid)] = str(found.get("name") or found.get("display_name") or aid)
        except Exception as exc:  # a name is nice to have, not essential
            log.debug("could not read authority %s: %s", aid, exc)
            out[str(aid)] = f"authority {aid}"
    return out


def check_new_account(client: WorkflowClient, authority_name: str) -> NewAccountChecks:
    """Is the name free, and is the revision batch clear of other people's work?

    Sign-up does not check the name, and a duplicate only surfaces later as an
    AmbiguousAuthorityError -- so it is worth asking before creating anything.
    """
    checks = NewAccountChecks(authority_name=authority_name)
    try:
        checks.name_taken_by = [find_authority(client, authority_name)]
    except AuthorityNotFoundError:
        pass
    except AmbiguousAuthorityError as exc:
        checks.name_taken_by = list(exc.matches)

    pending = get_pending(client)
    checks.others_pending = authority_names(client, pending.authority_ids)
    return checks


def restore_capabilities(
    client: WorkflowClient,
    authority_id: str,
    integration_id: str,
    snapshot: Mapping[str, Any],
    *,
    dry_run: bool = False,
) -> CapabilityReport:
    """Put an integration's capability flags back to a snapshot.

    The snapshot is the body `GET .../capabilities` returned. It is applied as
    a "standard set", so only flags that differ are sent and the result is
    verified -- the same path as applying the real standard. No alerts
    fallback: a restore should do exactly what it says or fail.
    """
    return apply_standard_capabilities(
        client, authority_id, integration_id,
        standard=load_standard(snapshot),
        alerts_fallback=False,
        dry_run=dry_run,
    )


def plan_roles_restore(
    client: RoleClient,
    snapshot: Mapping[str, Any],
    organization_id: str,
) -> RoleAccessReport:
    """What `roles.restore_roles` would change, without changing it."""
    from logic.roles import RoleError

    if str(snapshot.get("organization_id")) != str(organization_id):
        raise RoleError(f"snapshot is for organization {snapshot.get('organization_id')}, "
                        f"not {organization_id}")
    current = {r.id: r for r in list_roles(client, organization_id)}
    report = RoleAccessReport(organization_id=str(organization_id))
    for saved in snapshot.get("roles", []):
        role = current.get(str(saved["id"]))
        if role is None:
            raise RoleError(f"role {saved['name']!r} (id {saved['id']}) no longer exists")
        wanted = set(saved["permissions"])
        granted = sorted(wanted - role.permissions)
        revoked = sorted(role.permissions - wanted)
        if granted:
            report.granted[role.name] = granted
        if revoked:
            report.revoked[role.name] = revoked
        if not granted and not revoked:
            report.unchanged.append(role.name)
    return report


# ------------------------------------------ working account -> changed
#
# For an account that already works: show what it has, then change only what
# the user ticked. Planning is read-only and records each section's refusal
# instead of raising, so a person sees every problem at once. Applying plans
# again first and refuses if anything moved since the preview, then runs the
# ticked steps in CHANGE_STEPS order:
#
# * the jurisdiction before capabilities -- alerts need it Active;
# * an added integration before capabilities, which may target it;
# * roles last -- the portal lists data sources only once capabilities exist.
#
# Integrations are only ever added, never edited. A jurisdiction is only ever
# added; publishing it is environment-wide, so someone else's work in the
# batch is refused before anything is created.


#: (key, label) for each step a change can run, in the order they run.
CHANGE_STEPS: tuple[tuple[str, str], ...] = (
    ("account_info", "Account details"),
    ("boundary", "Add a jurisdiction"),
    ("revision", "Publish revision"),
    ("integration", "Add an integration"),
    ("capabilities", "Capabilities"),
    ("roles", "Role & Access"),
)

#: What "Account details" may change. account_id only while it is empty;
#: dispatch_type, name and display_name are not offered -- integration names
#: and Resume both key off the authority name.
EDITABLE_ACCOUNT_FIELDS: tuple[str, ...] = (
    "account_id", "country", "state", "contact_name", "contact_email",
    "contact_phone", "contact_title", "non_emergency_phone", "population",
    "phone_system", "cad_system", "mapping_system",
)

#: `AccountChanges.capabilities_integration` value meaning "the one being added".
NEW_INTEGRATION = "new"


@dataclass(frozen=True)
class IntegrationSummary:
    integration: Integration
    total: int
    enabled: int
    rsos_enabled: int


@dataclass
class AccountOverview:
    """What an existing account has. Read-only."""

    authority_id: str
    authority_name: str
    organization_id: str
    record: dict[str, Any]
    jurisdictions: list[Jurisdiction] = field(default_factory=list)
    integrations: list[IntegrationSummary] = field(default_factory=list)
    pending_mine: list[str] = field(default_factory=list)          # jurisdiction ids
    pending_others: dict[str, str] = field(default_factory=dict)   # id -> name

    @property
    def account_id_locked(self) -> bool:
        return bool(self.record.get("account_id"))


def _capability_counts(body: Any) -> tuple[int, int, int]:
    entries = (body.get("capabilities") or []) if isinstance(body, Mapping) else []
    return (len(entries),
            sum(1 for e in entries if e.get("authority_enabled")),
            sum(1 for e in entries if e.get("rsos_enabled")))


def describe_account(
    andromeda: WorkflowClient,
    authority: str,
    *,
    organization_id: str | None = None,
) -> AccountOverview:
    """The authority, its jurisdictions, its integrations and their capability
    counts, and the revision batch. Writes nothing.

    `authority` is a name or numeric id. A shared name is resolved only when
    exactly one match is in `organization_id`; otherwise AmbiguousAuthorityError.
    """
    authority_id = _find_authority_id(andromeda, authority, organization_id,
                                      attempts=1, delay=0, sleep=time.sleep)
    record = get_authority(andromeda, authority_id)
    overview = AccountOverview(
        authority_id=authority_id,
        authority_name=str(record.get("name") or record.get("display_name") or authority),
        organization_id=str(record.get("organization_id") or ""),
        record=dict(record),
        jurisdictions=list_jurisdictions(andromeda, authority_id),
    )
    for integration in list_integrations(andromeda, authority_id):
        body = andromeda.get(_capabilities_path(authority_id, integration.id))
        overview.integrations.append(IntegrationSummary(integration, *_capability_counts(body)))

    pending = get_pending(andromeda)
    overview.pending_mine = [str(e.get("id")) for e in pending.entries_for(authority_id)]
    others = [a for a in pending.authority_ids if a != str(authority_id)]
    overview.pending_others = authority_names(andromeda, others)
    return overview


class StalePlanError(ConfigureError):
    """The account changed between Preview and Apply."""

    def __init__(self, sections: list[str]):
        self.sections = list(sections)
        super().__init__(
            f"the account changed since Preview ({', '.join(sections)}). Nothing was "
            f"written. Preview again and check the new diff."
        )


class ChangeRefusedError(ConfigureError):
    """A ticked section cannot be applied as asked."""


@dataclass
class AccountChanges:
    """What the user ticked. Unset means "leave it alone"."""

    account_info: dict[str, Any] | None = None     # field -> new value
    polygon: Mapping[str, Any] | None = None       # add a jurisdiction
    add_integration: bool = False
    capabilities_integration: str | None = None    # an integration id, or NEW_INTEGRATION
    standard: Mapping[CapabilityKey, tuple[bool, bool]] | None = None   # None: packaged set
    standard_label: str = "the standard sandbox set"
    roles: bool = False

    @property
    def steps(self) -> list[str]:
        ticked = {
            "account_info": self.account_info is not None,
            "boundary": self.polygon is not None,
            "revision": self.polygon is not None,
            "integration": self.add_integration,
            "capabilities": self.capabilities_integration is not None,
            "roles": self.roles,
        }
        return [key for key, _ in CHANGE_STEPS if ticked[key]]


@dataclass
class SectionPlan:
    step: str
    changes: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    refusal: Exception | None = None

    def lines(self) -> tuple:
        """What was shown -- compared at Apply to detect a moved account."""
        return (tuple(self.changes), tuple(self.skipped),
                str(self.refusal) if self.refusal else None)


@dataclass
class ChangePlan:
    authority_id: str
    authority_name: str
    organization_id: str
    changes: AccountChanges
    sections: dict[str, SectionPlan] = field(default_factory=dict)
    app_name: str | None = None                   # for an added integration
    account_info: AccountInfoReport | None = None
    capabilities: CapabilityReport | None = None
    roles: RoleAccessReport | None = None

    @property
    def refusals(self) -> dict[str, Exception]:
        return {k: s.refusal for k, s in self.sections.items() if s.refusal is not None}

    @property
    def blocked(self) -> bool:
        return bool(self.refusals)

    def fingerprint(self) -> dict[str, tuple]:
        return {k: s.lines() for k, s in self.sections.items()}


def _unused_app_name(base: str, taken: set[str]) -> str:
    """`base`, or `base 2`, `base 3`, ... -- the first no integration has."""
    if base not in taken:
        return base
    n = 2
    while f"{base} {n}" in taken:
        n += 1
    return f"{base} {n}"


def plan_account_changes(
    andromeda: WorkflowClient,
    portal: RoleClient | None,
    authority_id: str,
    changes: AccountChanges,
    *,
    portal_problem: str | None = None,
) -> ChangePlan:
    """Dry-run every ticked section against the account as it is now.

    Never raises for a section's own problem: it is recorded as that section's
    `refusal`, and `ChangePlan.blocked` says whether Apply may run. `portal` is
    None when the portal login failed; `portal_problem` says why.
    """
    from logic.authorities import UnknownCountryError, UnknownStateError
    from logic.capabilities import ALERTS_CAPABILITIES
    from logic.capabilities import plan as plan_flags
    from logic.integrations import build_app_name
    from logic.jurisdictions import InvalidGeometryError, JurisdictionError, bbox

    record = get_authority(andromeda, authority_id)
    plan = ChangePlan(
        authority_id=str(authority_id),
        authority_name=str(record.get("name") or record.get("display_name") or authority_id),
        organization_id=str(record.get("organization_id") or ""),
        changes=changes,
    )
    steps = changes.steps

    # --- account details ---------------------------------------------------
    if "account_info" in steps:
        section = plan.sections["account_info"] = SectionPlan("account_info")
        fields = dict(changes.account_info or {})
        not_offered = sorted(set(fields) - set(EDITABLE_ACCOUNT_FIELDS))
        if not_offered:
            section.refusal = ChangeRefusedError(
                f"not editable here: {', '.join(not_offered)}")
        elif not fields:
            section.refusal = ChangeRefusedError("no account details were given")
        else:
            wanted_id = fields.get("account_id")
            try:
                if wanted_id and not record.get("account_id"):
                    try:
                        clash = find_authority(andromeda, account_id=wanted_id)
                    except AuthorityNotFoundError:
                        clash = None
                    if clash is not None and str(clash.get("id")) != str(authority_id):
                        raise AccountIdInUseError(wanted_id, clash)
                report = update_account_info(andromeda, authority_id, dispatch_type=None,
                                             dry_run=True, **fields)
            except (AccountIdInUseError, UnknownCountryError, UnknownStateError,
                    AccountInfoError) as exc:
                section.refusal = exc
            else:
                plan.account_info = report
                section.changes = [f"{name}: {before!r} -> {after!r}"
                                   for name, (before, after) in sorted(report.changed.items())]
                section.skipped = [f"{name}: {reason}"
                                   for name, reason in sorted(report.skipped.items())]
                if report.is_noop and not report.skipped:
                    section.notes.append("already has these values; nothing to write")

    # --- add a jurisdiction ------------------------------------------------
    if "boundary" in steps:
        section = plan.sections["boundary"] = SectionPlan("boundary")
        try:
            polygon = normalize_geojson(changes.polygon)
            validate_geojson(polygon)
        except (InvalidGeometryError, JurisdictionError) as exc:
            section.refusal = exc
        else:
            box = tuple(round(v, 4) for v in bbox(polygon))
            section.changes.append(
                f"create a jurisdiction ({len(polygon['features'])} feature(s), "
                f"bbox {box}), then publish a revision to make it Active")
            existing = list_jurisdictions(andromeda, authority_id)
            if existing:
                listing = ", ".join(f"{j.id} {j.ingress_label}" for j in existing)
                section.notes.append(
                    f"the account already has {len(existing)} jurisdiction(s) "
                    f"({listing}); this adds another and changes none of them")
            pending = get_pending(andromeda)
            mine = [str(e.get("id")) for e in pending.entries_for(authority_id)]
            if mine:
                section.changes.append(
                    f"the same revision also publishes this account's waiting "
                    f"jurisdiction(s) {', '.join(mine)}")
            others = [a for a in pending.authority_ids if a != str(authority_id)]
            if others:
                section.refusal = OtherAuthoritiesPendingError(str(authority_id), others)
        plan.sections["revision"] = SectionPlan("revision", notes=[
            "publishing a revision is environment-wide: it activates every "
            "authority's pending jurisdiction changes"])

    # --- add an integration ------------------------------------------------
    if "integration" in steps:
        section = plan.sections["integration"] = SectionPlan("integration")
        taken = {i.app_name for i in list_integrations(andromeda, authority_id)}
        base = build_app_name(plan.authority_name)
        plan.app_name = _unused_app_name(base, taken)
        section.changes.append(f"create integration {plan.app_name!r} ({DEFAULT_PRODUCT})")
        if plan.app_name != base:
            section.notes.append(f"{base!r} already exists, so this one is numbered")
        if any(name.startswith(_generated_name_prefix(plan.authority_name)) for name in taken):
            section.notes.append(
                "the account will then have more than one generated integration, so "
                "Resume setup will refuse to guess which one to configure")
        section.notes.append("its consumer secret is shown once, when it is created")

    # --- capabilities ------------------------------------------------------
    if "capabilities" in steps:
        section = plan.sections["capabilities"] = SectionPlan("capabilities")
        target = str(changes.capabilities_integration)
        standard = changes.standard if changes.standard is not None else load_standard()
        section.notes.append(f"source: {changes.standard_label}")
        if target == NEW_INTEGRATION:
            if not changes.add_integration:
                section.refusal = ChangeRefusedError(
                    "capabilities are for the new integration, but adding one is not ticked")
            else:
                section.changes.append(
                    f"apply {len(standard)} capability flag(s) to {plan.app_name!r} once "
                    f"it exists -- its catalog cannot be read before then")
        else:
            integrations = {i.id: i for i in list_integrations(andromeda, authority_id)}
            if target not in integrations:
                section.refusal = ChangeRefusedError(
                    f"authority {authority_id} has no integration {target}")
            else:
                live = andromeda.get(_capabilities_path(authority_id, target))
                _, report = plan_flags(live, standard, integration_id=target)
                plan.capabilities = report
                section.changes = [str(c) for c in report.changed]
                if report.is_noop:
                    section.notes.append(f"{integrations[target]} already matches")
                alerts = {CapabilityKey(n, c) for n, c in ALERTS_CAPABILITIES}
                in_play = [c for c in report.changed if c.key in alerts]
                if in_play:
                    section.notes.append(
                        f"{len(in_play)} of these are alerts capabilities; if the "
                        f"jurisdiction overlaps one that already has them, Andromeda "
                        f"refuses them and they are left as they are")
                if report.missing_from_target:
                    section.skipped.append(
                        f"{len(report.missing_from_target)} in the set but not offered by "
                        f"this integration: "
                        f"{', '.join(str(k) for k in report.missing_from_target)}")
                if report.missing_from_standard:
                    section.skipped.append(
                        f"{len(report.missing_from_standard)} on the integration but not "
                        f"in the set, left unchanged")

    # --- role & access -----------------------------------------------------
    if "roles" in steps:
        section = plan.sections["roles"] = SectionPlan("roles")
        if portal is None:
            section.refusal = ChangeRefusedError(
                portal_problem or "there is no portal login for this account")
        elif not plan.organization_id:
            section.refusal = ChangeRefusedError(
                f"authority {authority_id} has no organization_id, so its portal "
                f"roles cannot be found")
        else:
            report = enable_all_data_sources(portal, plan.organization_id, dry_run=True)
            plan.roles = report
            section.changes = (
                [f"{r}: +{', '.join(n)}" for r, n in sorted(report.granted.items())]
                + [f"{r}: -{', '.join(n)}" for r, n in sorted(report.revoked.items())])
            if report.is_noop:
                section.notes.append("roles already hold every data source")
            if report.revoked:
                if "capabilities" in steps:
                    section.notes.append(
                        "these removals may vanish once capabilities are written; roles "
                        "are checked again just before writing, and any removal then "
                        "is refused")
                else:
                    section.refusal = UnexpectedRevocationError(report)
            elif "capabilities" in steps:
                section.notes.append("checked again after capabilities, just before writing")

    return plan


@dataclass
class ChangeResult:
    authority_id: str
    authority_name: str
    organization_id: str
    planned: list[str]
    account_info: AccountInfoReport | None = None
    jurisdiction: Jurisdiction | None = None
    revision: RevisionResult | None = None
    integration: Integration | None = None
    capabilities: CapabilityReport | None = None
    roles: RoleAccessReport | None = None
    snapshots: dict[str, str] = field(default_factory=dict)
    steps: list[StepOutcome] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def status_of(self, step: str) -> str:
        for outcome in reversed(self.steps):
            if outcome.step == step and outcome.status != "note":
                return outcome.status
        return "not run"

    @property
    def completed(self) -> list[str]:
        return [k for k in self.planned if self.status_of(k) in _FINISHED]

    @property
    def not_done(self) -> list[str]:
        return [k for k in self.planned if self.status_of(k) not in _FINISHED]

    def existing(self) -> list[str]:
        """What this change really wrote, in words."""
        out = []
        if self.account_info and self.account_info.applied:
            out.append(f"account details updated "
                       f"({', '.join(sorted(self.account_info.changed))})")
        if self.jurisdiction and self.jurisdiction.id:
            out.append(f"jurisdiction {self.jurisdiction.id} added "
                       f"({self.jurisdiction.ingress_label})")
        if self.revision:
            out.append(f"revision {self.revision.revision_number} published")
        if self.integration and self.integration.id:
            out.append(f"integration {self.integration} added")
        if self.capabilities and self.capabilities.applied:
            out.append(f"{len(self.capabilities.changed)} capability change(s) applied")
        if self.roles and self.roles.applied:
            out.append("portal roles updated")
        return out


class PartialChangeError(PartialConfigureError):
    """`apply_account_changes` stopped. Carries a ChangeResult of what happened."""


def change_next_action(error: PartialChangeError) -> str:
    """What a person should do about `error`, in one or two sentences."""
    added = [k for k in ("boundary", "integration") if k in error.result.completed]
    untick = ""
    if added:
        what = " and ".join({"boundary": "the jurisdiction",
                             "integration": "the integration"}[k] for k in added)
        untick = (f" Untick {what} before previewing again: it was added, and "
                  f"ticking it again would add a second one.")
    for cause in _cause_chain(error.cause):
        if isinstance(cause, StalePlanError):
            return ("The account changed since Preview. Nothing was written. Press "
                    "Preview again and check the new diff.")
        if isinstance(cause, (PartialActivationError, NotInPendingBatchError)):
            return (f"Jurisdiction {cause.jurisdiction.id} was created but not published. "
                    f"It is waiting in the environment-wide batch, so the next revision "
                    f"anyone publishes will activate it. Do not add it again; tell the "
                    f"team and check it in Andromeda.")
        if isinstance(cause, OtherAuthoritiesPendingError):
            return ("Publishing a revision is environment-wide, and the pending revision "
                    "holds other authorities' changes. Nothing was created or published. "
                    "Ask the team who owns those changes, then preview again.")
        if isinstance(cause, UnexpectedRevocationError):
            return ("The roles were not changed. Applying would remove permissions the "
                    "portal's catalog does not list; check Admin -> Role and Access in "
                    "the portal." + untick)
        if isinstance(cause, CapabilityWriteError):
            return ("The server did not store the capabilities as requested. Undo them "
                    "from the snapshot, then check the integration in Andromeda." + untick)
    return "Fix the cause shown, then preview again." + untick


def apply_account_changes(
    andromeda: WorkflowClient,
    portal: RoleClient | None,
    plan: ChangePlan,
    *,
    portal_problem: str | None = None,
    save_snapshot: Callable[[str, Mapping[str, Any]], str | None] | None = None,
    on_step: Callable[[StepOutcome], None] | None = None,
    confirm_attempts: int = 5,
    confirm_delay: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> ChangeResult:
    """Apply a previewed ChangePlan: only the ticked steps, in CHANGE_STEPS order.

    Refuses, writing nothing, when the plan has a refused section, or when
    planning again gives anything different from what was previewed.
    Capabilities and roles are passed to `save_snapshot(label, body)` before
    each is written.

    Raises
    ------
    PartialChangeError
        A step failed or refused. Carries a ChangeResult saying what was
        written; `change_next_action(error)` says what to do.
    """
    changes = plan.changes
    authority_id = plan.authority_id
    result = ChangeResult(authority_id=authority_id, authority_name=plan.authority_name,
                          organization_id=plan.organization_id, planned=changes.steps)

    def record(step: str, status: str, detail: str = "") -> None:
        outcome = StepOutcome(step, status, detail)
        result.steps.append(outcome)
        if status == "note":
            result.notes.append(detail)
        if on_step is not None:
            on_step(outcome)

    def fail(step: str, exc: Exception) -> PartialChangeError:
        record(step, "failed", str(root_cause(exc)))
        return PartialChangeError(result, step, exc)

    def snapshot(label: str, body: Mapping[str, Any]) -> None:
        if save_snapshot is None:
            return
        where = save_snapshot(label, body)
        if where:
            result.snapshots[label] = where

    # --- look again before writing anything ------------------------------
    if not changes.steps:
        raise PartialChangeError(result, "account_info",
                                 ChangeRefusedError("nothing was ticked"))
    if plan.blocked:
        step, refusal = next(iter(plan.refusals.items()))
        raise fail(step, refusal)
    try:
        again = plan_account_changes(andromeda, portal, authority_id, changes,
                                     portal_problem=portal_problem)
    except Exception as exc:
        raise fail(changes.steps[0], exc) from exc
    before, now = plan.fingerprint(), again.fingerprint()
    moved = [k for k in changes.steps if before.get(k) != now.get(k)]
    if moved:
        raise fail(moved[0], StalePlanError(moved))

    # --- account details ---------------------------------------------------
    if "account_info" in changes.steps:
        record("account_info", "running")
        try:
            report = update_account_info(andromeda, authority_id, dispatch_type=None,
                                         **dict(changes.account_info or {}))
        except Exception as exc:
            raise fail("account_info", exc) from exc
        result.account_info = report
        for name, reason in sorted(report.skipped.items()):
            record("account_info", "note", f"{name} left unchanged: {reason}")
        record("account_info", "skipped" if report.is_noop else "done",
               "already up to date" if report.is_noop else report.summary())

    # --- add a jurisdiction, publish it ------------------------------------
    if "boundary" in changes.steps:
        record("boundary", "running", "creating the jurisdiction, then publishing it")
        try:
            attached = attach_and_activate(andromeda, authority_id, polygon=changes.polygon)
        except (PartialActivationError, NotInPendingBatchError) as exc:
            result.jurisdiction = exc.jurisdiction
            record("boundary", "done", f"jurisdiction {exc.jurisdiction.id} created")
            raise fail("revision", exc) from exc
        except Exception as exc:
            raise fail("boundary", exc) from exc
        result.jurisdiction = attached.jurisdiction
        result.revision = attached.revision
        record("boundary", "done", f"jurisdiction {attached.jurisdiction.id} created")
        try:
            confirmed = confirm_jurisdiction_active(
                andromeda, authority_id, attached.jurisdiction.id,
                attempts=confirm_attempts, delay=confirm_delay, sleep=sleep)
        except Exception as exc:
            raise fail("revision", exc) from exc
        if confirmed is not None:
            result.jurisdiction = confirmed
        record("revision", "done", f"revision {attached.revision.revision_number} published")
        if confirmed is None or confirmed.ingress_status != int(IngressStatus.ACTIVE):
            label = confirmed.ingress_label if confirmed else "not listed"
            record("revision", "note",
                   f"jurisdiction {attached.jurisdiction.id} is still {label}; alerts "
                   f"capabilities depend on it being Active and may be skipped")

    # --- add an integration ------------------------------------------------
    if "integration" in changes.steps:
        record("integration", "running")
        try:
            integration = create_integration(
                andromeda, authority_id, app_name=plan.app_name, product=DEFAULT_PRODUCT,
                authority_name=plan.authority_name, if_exists=ExistsPolicy.ERROR)
        except Exception as exc:
            raise fail("integration", exc) from exc
        result.integration = integration
        record("integration", "done", f"created {integration}")

    # --- capabilities ------------------------------------------------------
    if "capabilities" in changes.steps:
        target = str(changes.capabilities_integration)
        if target == NEW_INTEGRATION:
            target = result.integration.id
        record("capabilities", "running")
        try:
            live = andromeda.get(_capabilities_path(authority_id, target))
            snapshot(f"capabilities-{target}", live)
            caps = apply_standard_capabilities(andromeda, authority_id, target,
                                               standard=changes.standard)
        except Exception as exc:
            raise fail("capabilities", exc) from exc
        result.capabilities = caps
        record("capabilities", "done" if caps.applied else "skipped", caps.summary())
        if caps.alerts_skipped:
            record("capabilities", "note",
                   f"{len(caps.alerts_skipped)} alerts capabilities left off -- the "
                   f"jurisdiction overlaps one that already has them: "
                   f"{', '.join(str(k) for k in caps.alerts_skipped)}")

    # --- role & access -----------------------------------------------------
    if "roles" in changes.steps:
        org = plan.organization_id
        record("roles", "running")
        try:
            # planned again now: capabilities may have changed the catalog
            planned_roles = enable_all_data_sources(portal, org, dry_run=True)
            if planned_roles.revoked:
                raise UnexpectedRevocationError(planned_roles)
            if planned_roles.is_noop:
                result.roles = planned_roles
            else:
                snapshot(f"roles-{org}", snapshot_roles(portal, org))
                result.roles = enable_all_data_sources(portal, org)
        except Exception as exc:
            raise fail("roles", exc) from exc
        record("roles", "done" if result.roles.applied else "skipped", result.roles.summary())

    log.info("account changed: %s", "; ".join(result.existing()) or "nothing to write")
    return result