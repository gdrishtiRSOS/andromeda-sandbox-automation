"""Apply the standard capability set to an Andromeda integration.

Single responsibility: given an authority and an integration that already
exist, make that integration's capability flags match the organisation's
standard set. Creating the account, creating the integration, authentication
and orchestration all belong to the caller.

Design notes
------------
* The caller owns the HTTP client. This module talks to it through the
  `AndromedaClient` protocol so it can be swapped for a fake in tests and so
  auth/retry/logging policy stays in one place in the wider project.
* `plan()` is pure -- no I/O. All the interesting logic is testable without a
  network or a fixture server.
* Overlay, not replace. The live catalog is the authority on which
  capability_types exist for an integration; we copy flags onto it rather than
  PATCHing the standard file wholesale. A capability added upstream since the
  standard was captured is left alone instead of being silently dropped.
* Match on (name, category). Positional matching is also unsafe; the API does not guarantee ordering.
* Idempotent. Re-running against an already-correct integration issues no
  PATCH and reports zero changes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

log = logging.getLogger(__name__)

__all__ = [
    "ALERTS_CAPABILITIES",
    "AndromedaClient",
    "CapabilityKey",
    "CapabilityReport",
    "DriftPolicy",
    "FlagChange",
    "apply_standard_capabilities",
    "load_standard",
    "plan",
    "CapabilityError",
    "CapabilityDriftError",
    "CapabilityWriteError",
]

STANDARD_PATH = Path(__file__).parent / "data" / "standard_capabilities.json"

# Alerts cannot be enabled where the authority's jurisdiction overlaps another
# that already has them. Andromeda returns a 500 rather than a validation
# error, and the PATCH is atomic, so nothing is written when it fails. We use
# that: attempt the full set, and on failure retry without these.
#alerts_notification and alertus are deliberately absent.

ALERTS_CAPABILITIES: frozenset[tuple[str, int]] = frozenset({
    ("alerts", 2),
    ("alerts_active_assailant", 0),
    ("alerts_fire", 0),
    ("alerts_law_enforcement", 0),
    ("alerts_medical", 0),
    ("alerts_test_mode", 0),
    ("alerts_train_incident", 0),
})


# --------------------------------------------------------------------- types


class DriftPolicy(str, Enum):
    """What to do when target and standard disagree about which capabilities exist."""

    WARN = "warn"    # log and continue -- right for provisioning
    RAISE = "raise"  # fail loudly -- right for CI drift checks


class CapabilityError(Exception):
    """Base for this module."""


class CapabilityDriftError(CapabilityError):
    def __init__(self, missing_from_standard, missing_from_target):
        self.missing_from_standard = list(missing_from_standard)
        self.missing_from_target = list(missing_from_target)
        super().__init__(
            f"capability drift: {len(self.missing_from_standard)} in target but not "
            f"in standard, {len(self.missing_from_target)} in standard but not in target"
        )


class CapabilityWriteError(CapabilityError):
    def __init__(self, mismatched):
        self.mismatched = list(mismatched)
        super().__init__(f"{len(self.mismatched)} capabilities did not persist as requested")


@dataclass(frozen=True, order=True)
class CapabilityKey:
    name: str
    category: int

    def __str__(self) -> str:
        return f"{self.name}(cat {self.category})"


@dataclass(frozen=True)
class FlagChange:
    key: CapabilityKey
    before: tuple[bool, bool]
    after: tuple[bool, bool]

    def __str__(self) -> str:
        return (
            f"{self.key}: authority {self.before[0]}->{self.after[0]}, "
            f"rsos {self.before[1]}->{self.after[1]}"
        )


@dataclass
class CapabilityReport:
    integration_id: str
    live_count: int
    standard_count: int
    changed: list[FlagChange] = field(default_factory=list)
    missing_from_standard: list[CapabilityKey] = field(default_factory=list)
    missing_from_target: list[CapabilityKey] = field(default_factory=list)
    applied: bool = False
    alerts_skipped: list[CapabilityKey] = field(default_factory=list)
    fallback_used: bool = False
    first_attempt_error: str | None = None

    @property
    def has_drift(self) -> bool:
        return bool(self.missing_from_standard or self.missing_from_target)

    @property
    def is_noop(self) -> bool:
        return not self.changed

    def summary(self) -> str:
        parts = [f"integration {self.integration_id}: {len(self.changed)} change(s)"]
        if self.alerts_skipped:
            parts.append(f"{len(self.alerts_skipped)} alerts skipped (jurisdiction overlap)")
        if self.has_drift:
            parts.append(
                f"drift {len(self.missing_from_standard)}/{len(self.missing_from_target)}"
            )
        parts.append("applied" if self.applied else "not applied")
        return "; ".join(parts)


@runtime_checkable
class AndromedaClient(Protocol):
    """The subset of the project's Andromeda client this module needs.

    Implementations own auth, retries, timeouts and base URL, and should raise
    on non-2xx rather than returning an error body.
    """

    def get(self, path: str) -> Any: ...

    def patch(self, path: str, json: Mapping[str, Any]) -> Any: ...


# ----------------------------------------------------------------- internals


def _key(entry: Mapping[str, Any]) -> CapabilityKey:
    ct = entry["capability_type"]
    return CapabilityKey(ct["name"], ct["category"])


def _flags(entry: Mapping[str, Any]) -> tuple[bool, bool]:
    return bool(entry["authority_enabled"]), bool(entry["rsos_enabled"])


def _capabilities_path(authority_id: str, integration_id: str) -> str:
    return (
        f"/v1/andromeda/authorities/{authority_id}"
        f"/integrations/{integration_id}/capabilities"
    )


# -------------------------------------------------------------- public logic


def load_standard(
    source: str | Path | Mapping[str, Any] | None = None,
) -> dict[CapabilityKey, tuple[bool, bool]]:
    """Load the standard set into a lookup keyed by (name, category).

    `source` may be a path, an already-parsed mapping, or None to use the
    packaged default. Raises on duplicate keys, which would make the standard
    ambiguous.
    """
    if source is None:
        source = STANDARD_PATH
    if isinstance(source, (str, Path)):
        with open(source) as fh:
            doc = json.load(fh)
    else:
        doc = source

    try:
        entries: Iterable[Mapping[str, Any]] = doc["capabilities"]
    except (TypeError, KeyError) as exc:
        raise CapabilityError("standard set must be an object with a 'capabilities' list") from exc

    out: dict[CapabilityKey, tuple[bool, bool]] = {}
    for entry in entries:
        key = _key(entry)
        if key in out:
            raise CapabilityError(f"duplicate capability in standard set: {key}")
        out[key] = _flags(entry)
    return out


def plan(
    live: Mapping[str, Any],
    standard: Mapping[CapabilityKey, tuple[bool, bool]],
    *,
    integration_id: str = "",
) -> tuple[dict[str, Any], CapabilityReport]:
    """Compute the PATCH body and a report. Pure -- does no I/O.

    Returns the full capability list with flags overlaid, ready to PATCH. The
    input is not mutated.
    """
    body = json.loads(json.dumps(live))  # deep copy; payloads are plain JSON
    report = CapabilityReport(
        integration_id=integration_id,
        live_count=len(body["capabilities"]),
        standard_count=len(standard),
    )

    seen: set[CapabilityKey] = set()
    for entry in body["capabilities"]:
        key = _key(entry)
        seen.add(key)

        if key not in standard:
            report.missing_from_standard.append(key)
            continue

        before = _flags(entry)
        want = standard[key]
        if want != before:
            report.changed.append(FlagChange(key, before, want))
        entry["authority_enabled"], entry["rsos_enabled"] = want

    report.missing_from_target = sorted(set(standard) - seen)
    return body, report


def _verify(sent: Mapping[str, Any], returned: Mapping[str, Any]) -> None:
    wanted = {_key(e): _flags(e) for e in sent["capabilities"]}
    got = {_key(e): _flags(e) for e in returned["capabilities"]}
    mismatched = [
        (key, wanted[key], got[key])
        for key in wanted
        if key in got and wanted[key] != got[key]
    ]
    if mismatched:
        for key, want, have in mismatched:
            log.error("capability %s: requested %s, server stored %s", key, want, have)
        raise CapabilityWriteError(mismatched)


def _without(body: Mapping[str, Any], live: Mapping[str, Any],
             keys: Iterable[CapabilityKey]) -> dict[str, Any]:
    """Copy of `body` with `keys` reverted to whatever the live catalog had."""
    drop = set(keys)
    current = {_key(e): _flags(e) for e in live["capabilities"]}
    out = json.loads(json.dumps(body))
    for entry in out["capabilities"]:
        key = _key(entry)
        if key in drop and key in current:
            entry["authority_enabled"], entry["rsos_enabled"] = current[key]
    return out


def apply_standard_capabilities(
    client: AndromedaClient,
    authority_id: str,
    integration_id: str,
    *,
    standard: Mapping[CapabilityKey, tuple[bool, bool]] | None = None,
    drift_policy: DriftPolicy = DriftPolicy.WARN,
    dry_run: bool = False,
    verify: bool = True,
    alerts_fallback: bool = True,
    alerts_capabilities: Iterable[tuple[str, int]] = ALERTS_CAPABILITIES,
) -> CapabilityReport:
    """Make an integration's capabilities match the standard set.

    Safe to re-run: if the integration already matches, no PATCH is sent.

    Alerts fallback
    ---------------
    Alerts capabilities cannot be enabled where the authority's jurisdiction
    overlaps one that already has them; Andromeda answers with a 500 rather
    than a validation error. The PATCH is atomic, so a failed attempt writes
    nothing. When `alerts_fallback` is on and the first attempt included
    alerts changes, the write is retried with those capabilities left at their
    current values, and the report records what was skipped.

    If the retry also fails, the original error is raised -- the failure was
    not about alerts.

    Raises
    ------
    CapabilityDriftError
        When `drift_policy` is RAISE and the catalogs disagree.
    CapabilityWriteError
        When `verify` is on and the server did not store what was requested.
    """
    if standard is None:
        standard = load_standard()

    path = _capabilities_path(authority_id, integration_id)
    live = client.get(path)

    body, report = plan(live, standard, integration_id=integration_id)

    for key in report.missing_from_standard:
        log.warning(
            "capability %s exists on integration %s but not in the standard set; left unchanged",
            key, integration_id,
        )
    for key in report.missing_from_target:
        log.warning(
            "capability %s is in the standard set but not offered by integration %s",
            key, integration_id,
        )
    if report.has_drift and drift_policy is DriftPolicy.RAISE:
        raise CapabilityDriftError(report.missing_from_standard, report.missing_from_target)

    if report.is_noop:
        log.info("integration %s already matches the standard set", integration_id)
        return report

    if dry_run:
        log.info(
            "dry run: %d change(s) not applied to integration %s",
            len(report.changed), integration_id,
        )
        return report

    for change in report.changed:
        log.debug("%s", change)

    alerts_keys = {CapabilityKey(n, c) for n, c in alerts_capabilities}
    alerts_in_play = [c.key for c in report.changed if c.key in alerts_keys]

    try:
        result = client.patch(path, body)
    except Exception as exc:  # client owns its exception type
        if not (alerts_fallback and alerts_in_play):
            raise
        report.first_attempt_error = f"{type(exc).__name__}: {exc}"
        log.warning(
            "PATCH failed with %d alerts change(s) in the body (%s); "
            "retrying without them -- the jurisdiction likely overlaps one that "
            "already has alerts",
            len(alerts_in_play), report.first_attempt_error,
        )
        retry_body = _without(body, live, alerts_in_play)
        try:
            result = client.patch(path, retry_body)
        except Exception:
            log.error("retry without alerts also failed; the problem is not alerts")
            raise exc from None

        body = retry_body
        report.fallback_used = True
        report.alerts_skipped = sorted(alerts_in_play)
        report.changed = [c for c in report.changed if c.key not in set(alerts_in_play)]
        for key in report.alerts_skipped:
            log.warning("left %s unchanged: cannot be enabled on this authority", key)

    report.applied = True

    if verify:
        _verify(body, result)

    log.info(
        "applied %d capability change(s) to integration %s%s",
        len(report.changed), integration_id,
        f" ({len(report.alerts_skipped)} alerts skipped)" if report.alerts_skipped else "",
    )
    return report