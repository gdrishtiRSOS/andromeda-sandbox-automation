"""Enable the portal's data sources per role -- runbook step 8.

    GET   /v1/scorpius/organizations/{orgId}/capstone/permissions
    GET   /v1/scorpius/organizations/{orgId}/capstone/roles
    PATCH /v1/scorpius/organizations/{orgId}/capstone/roles/{roleId}
          {"name": "Admin", "permissions": ["MANAGE_SSO", "alerts", ...]}

Design notes
------------
* **A different host.** These live on the portal API
  (`api-sandbox.rapidsosportal.com`), not Andromeda. That host authenticates
  with email/password via `/v1/scorpius/user/api-token-auth`, so the client
  passed in here is not the Andromeda one.

* **The org id, not the authority id.** It comes from the authority record's
  `organization_id` -- 4958 -> 15516.

* **No golden file.** The runbook says "enable all data sources for both Admin
  and Agent", and the split is derivable from the catalog rather than fixed:
  Admin gets every permission, Agent gets every permission except those
  flagged `rsp_rbac` -- the twelve MANAGE_* entries that are administrative
  by nature. Verified against a capture: the set Agent lacked was exactly the
  rsp_rbac set. Deriving it means a permission added upstream is picked up
  automatically instead of quietly missing.

* **The PATCH replaces the whole list**, and the body must carry `name` as
  well as `permissions`, so the role is read first and its name echoed back.

* **There is no undo on the server.** The PATCH replaces a role's whole list
  and nothing keeps history, so `snapshot_roles` / `restore_roles` exist and
  a snapshot should be taken before writing. Roles belong to an ORGANIZATION,
  not an authority, so a mistake here affects every user of that org.

* Without this step the account has capabilities in Andromeda but the data
  sources are off in the portal, so the Alerts tab stays empty -- the symptom
  that looks like a broken account rather than a missing step.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

log = logging.getLogger(__name__)

__all__ = [
    "ADMIN_ROLE",
    "AGENT_ROLE",
    "Role",
    "RoleAccessReport",
    "RoleClient",
    "RoleError",
    "RoleNotFoundError",
    "RoleWriteError",
    "enable_all_data_sources",
    "list_permissions",
    "list_roles",
    "restore_roles",
    "snapshot_roles",
    "target_permissions",
    "update_role",
]

ADMIN_ROLE = "Admin"
AGENT_ROLE = "Agent"


class RoleError(Exception):
    """Base for this module."""


class RoleNotFoundError(RoleError):
    def __init__(self, wanted: Iterable[str], available: Iterable[str]):
        self.wanted = list(wanted)
        self.available = list(available)
        super().__init__(
            f"no role named {', '.join(repr(w) for w in self.wanted)}. "
            f"This organization has: {', '.join(self.available) or '(none)'}"
        )


class RoleWriteError(RoleError):
    def __init__(self, role: str, missing: list[str], extra: list[str]):
        self.role = role
        self.missing = missing
        self.extra = extra
        super().__init__(
            f"role {role!r} did not store what was requested: "
            f"{len(missing)} missing, {len(extra)} unexpected"
        )


@dataclass(frozen=True)
class Role:
    id: str
    name: str
    permissions: frozenset[str]
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "Role":
        return cls(
            id=str(data["id"]),
            name=str(data.get("name", "")),
            permissions=frozenset(data.get("permissions") or []),
            raw=data,
        )

    def __str__(self) -> str:
        return f"{self.name} (id {self.id}, {len(self.permissions)} permission(s))"


@dataclass
class RoleAccessReport:
    organization_id: str
    catalog_size: int = 0
    granted: dict[str, list[str]] = field(default_factory=dict)
    revoked: dict[str, list[str]] = field(default_factory=dict)
    unchanged: list[str] = field(default_factory=list)
    applied: bool = False

    @property
    def is_noop(self) -> bool:
        return not self.granted and not self.revoked

    def summary(self) -> str:
        if self.is_noop:
            return f"organization {self.organization_id}: roles already correct"
        parts = []
        for role in sorted(set(self.granted) | set(self.revoked)):
            bits = []
            if self.granted.get(role):
                bits.append(f"+{len(self.granted[role])}")
            if self.revoked.get(role):
                bits.append(f"-{len(self.revoked[role])}")
            parts.append(f"{role} {' '.join(bits)}")
        state = "applied" if self.applied else "planned"
        return f"organization {self.organization_id}: {', '.join(parts)} ({state})"


@runtime_checkable
class RoleClient(Protocol):
    """The portal API client -- NOT the Andromeda one."""

    def get(self, path: str) -> Any: ...

    def patch(self, path: str, json: Mapping[str, Any]) -> Any: ...


def _base(organization_id: str) -> str:
    return f"/v1/scorpius/organizations/{organization_id}/capstone"


def _items(payload: Any, key: str) -> list[dict]:
    if isinstance(payload, Mapping):
        return list(payload.get(key) or [])
    return list(payload or [])


# ------------------------------------------------------------------ reads


def list_permissions(client: RoleClient, organization_id: str) -> list[dict[str, Any]]:
    """The permission catalog for this organization."""
    return _items(client.get(f"{_base(organization_id)}/permissions"), "permissions")


def list_roles(client: RoleClient, organization_id: str) -> list[Role]:
    return [Role.from_api(r)
            for r in _items(client.get(f"{_base(organization_id)}/roles"), "roles")]


# ----------------------------------------------------------------- policy


def target_permissions(
    catalog: Iterable[Mapping[str, Any]],
    role_name: str,
    *,
    admin_only_flag: str = "rsp_rbac",
) -> set[str]:
    """Which permissions a role should hold.

    Admin gets everything. Every other role gets everything except the
    administrative permissions -- those flagged `rsp_rbac`, which are the
    MANAGE_* entries. Derived rather than listed so new permissions are picked
    up automatically.
    """
    names = {str(p["name"]) for p in catalog}
    if role_name == ADMIN_ROLE:
        return names
    return {str(p["name"]) for p in catalog if not p.get(admin_only_flag)}


# ------------------------------------------------------------------ write


def update_role(
    client: RoleClient,
    organization_id: str,
    role: Role,
    permissions: Iterable[str],
    *,
    verify: bool = True,
) -> Role:
    """Replace a role's permission list. The body must carry `name` too."""
    wanted = sorted(set(permissions))
    body = {"name": role.name, "permissions": wanted}
    result = client.patch(f"{_base(organization_id)}/roles/{role.id}", body)
    updated = Role.from_api(result) if isinstance(result, Mapping) else role

    if verify and isinstance(result, Mapping):
        missing = sorted(set(wanted) - updated.permissions)
        extra = sorted(updated.permissions - set(wanted))
        if missing or extra:
            for name in missing:
                log.error("role %s: %s was requested but not stored", role.name, name)
            for name in extra:
                log.error("role %s: %s was stored but not requested", role.name, name)
            raise RoleWriteError(role.name, missing, extra)

    log.info("updated %s", updated)
    return updated


def snapshot_roles(client: RoleClient, organization_id: str) -> dict[str, Any]:
    """Capture every role's current permission list, so a change can be undone.

    The PATCH replaces a role's whole list and there is no server-side history,
    so this is the only way back. Take one before writing.
    """
    import datetime as dt

    roles = list_roles(client, organization_id)
    snapshot = {
        "organization_id": str(organization_id),
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "roles": [
            {"id": r.id, "name": r.name, "permissions": sorted(r.permissions)}
            for r in roles
        ],
    }
    log.info(
        "snapshot: %s",
        ", ".join(f"{r['name']} {len(r['permissions'])}" for r in snapshot["roles"]),
    )
    return snapshot


def restore_roles(
    client: RoleClient,
    snapshot: Mapping[str, Any],
    *,
    organization_id: str | None = None,
    verify: bool = True,
) -> RoleAccessReport:
    """Put roles back to a snapshot taken earlier.

    Raises
    ------
    RoleError
        The snapshot is for a different organization, or names a role that no
        longer exists.
    """
    org = str(organization_id or snapshot.get("organization_id") or "")
    if not org:
        raise RoleError("snapshot has no organization_id and none was given")
    if organization_id and snapshot.get("organization_id") not in (None, str(organization_id)):
        raise RoleError(
            f"snapshot is for organization {snapshot['organization_id']}, "
            f"not {organization_id}"
        )

    current = {r.id: r for r in list_roles(client, org)}
    report = RoleAccessReport(organization_id=org)

    for saved in snapshot.get("roles", []):
        role = current.get(str(saved["id"]))
        if role is None:
            raise RoleError(
                f"role {saved['name']!r} (id {saved['id']}) no longer exists"
            )
        wanted = set(saved["permissions"])
        granted = sorted(wanted - role.permissions)
        revoked = sorted(role.permissions - wanted)
        if not granted and not revoked:
            report.unchanged.append(role.name)
            continue
        if granted:
            report.granted[role.name] = granted
        if revoked:
            report.revoked[role.name] = revoked
        update_role(client, org, role, wanted, verify=verify)

    report.applied = not report.is_noop
    log.info("restored: %s", report.summary())
    return report


def enable_all_data_sources(
    client: RoleClient,
    organization_id: str,
    *,
    role_names: Iterable[str] = (ADMIN_ROLE, AGENT_ROLE),
    dry_run: bool = False,
    verify: bool = True,
    admin_only_flag: str = "rsp_rbac",
) -> RoleAccessReport:
    """Grant every data source to the given roles -- runbook step 8.

    Safe to re-run: a role that already holds the right set is left alone.

    Raises
    ------
    RoleNotFoundError
        One of `role_names` does not exist in this organization.
    RoleWriteError
        The server did not store what was requested.
    """
    catalog = list_permissions(client, organization_id)
    roles = list_roles(client, organization_id)
    report = RoleAccessReport(organization_id=str(organization_id),
                              catalog_size=len(catalog))

    by_name = {r.name: r for r in roles}
    missing = [n for n in role_names if n not in by_name]
    if missing:
        raise RoleNotFoundError(missing, [r.name for r in roles])

    log.info("permission catalog: %d entries", len(catalog))

    for name in role_names:
        role = by_name[name]
        wanted = target_permissions(catalog, name, admin_only_flag=admin_only_flag)
        granted = sorted(wanted - role.permissions)
        revoked = sorted(role.permissions - wanted)

        if not granted and not revoked:
            report.unchanged.append(name)
            log.info("%s already holds the right %d permission(s)", name, len(wanted))
            continue

        if granted:
            report.granted[name] = granted
        if revoked:
            report.revoked[name] = revoked
        log.info("%s: +%d / -%d -> %d permission(s)",
                 name, len(granted), len(revoked), len(wanted))
        for perm in revoked:
            log.warning("%s loses %s (not in the target set)", name, perm)

        if dry_run:
            continue
        update_role(client, organization_id, role, wanted, verify=verify)

    report.applied = not dry_run and not report.is_noop
    return report