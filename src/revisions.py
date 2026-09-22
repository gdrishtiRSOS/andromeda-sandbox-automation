"""Activate a jurisdiction by creating and publishing a revision.

Runbook step 7.

    GET  /v1/andromeda/revisions/pending   -> the open revision and its batch
    POST /v1/andromeda/revisions/pending   {revision_number, revision_date} -> 202
    POST /v1/andromeda/revisions/active    (no body)                        -> 202

Design notes
------------
* **Revisions are environment-wide.** Neither path takes an authority id. The
  pending revision batches jurisdiction changes across every authority, and
  publishing activates the whole batch. This module therefore inspects the
  batch first and refuses to publish when it contains authorities other than
  the one asked for, unless explicitly allowed.

* **Publishing does not empty the queue.** Observed directly: jurisdiction
  4261 (ingress_status 2) was in the batch before publishing and still in the
  new pending revision afterwards, unchanged. The runbook deliberately leaves
  ingress at Pending (step 4.6), so the diff keeps reporting it. Never loop
  until the queue is empty -- it will not terminate.

* The success signal is that the pending revision's **id changes** and its
  revision_number resets to null. 1986 (number 2209) -> 2019 (number null).

* Both POSTs return 202 with an empty body, so neither tells you anything;
  state has to be read back.

* revision_number is operator-chosen but **unique across the environment** --
  reusing one gives 409 "GeofenceRevision record with revision_number N
  already exists". Both observed values follow day + zero-padded month: 2 Sep
  -> 209, 22 Sep -> 2209, which collides when activating twice in a day, so
  create_revision retries with a suffixed number (220901, 220902, ...).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

log = logging.getLogger(__name__)

__all__ = [
    "PendingRevision",
    "RevisionResult",
    "RevisionClient",
    "RevisionError",
    "NothingToPublishError",
    "RevisionNumberConflictError",
    "AuthorityNotPendingError",
    "OtherAuthoritiesPendingError",
    "PublishNotConfirmedError",
    "activate_jurisdiction",
    "create_revision",
    "default_revision_number",
    "is_number_conflict",
    "revision_number_candidates",
    "get_pending",
    "get_verified",
    "publish_revision",
]

PENDING_PATH = "/v1/andromeda/revisions/pending"
ACTIVE_PATH = "/v1/andromeda/revisions/active"


class RevisionError(Exception):
    """Base for this module."""


class NothingToPublishError(RevisionError):
    """The pending revision holds no changes at all."""


class AuthorityNotPendingError(RevisionError):
    def __init__(self, authority_id: str, present: list[str]):
        self.authority_id = authority_id
        self.present = present
        super().__init__(
            f"authority {authority_id} has no pending jurisdiction changes"
            + (f"; the batch holds {', '.join(present)}" if present else "")
        )


class OtherAuthoritiesPendingError(RevisionError):
    def __init__(self, authority_id: str, others: list[str]):
        self.authority_id = authority_id
        self.others = others
        super().__init__(
            f"the pending revision also holds changes for {', '.join(others)}. "
            f"Publishing would activate their work too. Pass "
            f"allow_other_authorities=True only if that is intended."
        )


class PublishNotConfirmedError(RevisionError):
    def __init__(self, revision_id: str):
        self.revision_id = revision_id
        super().__init__(
            f"pending revision is still {revision_id} after publishing; "
            "the publish may not have taken effect"
        )


class RevisionNumberConflictError(RevisionError):
    def __init__(self, tried: list[int]):
        self.tried = list(tried)
        super().__init__(
            "every candidate revision number is already taken: "
            + ", ".join(str(n) for n in tried)
        )


@dataclass(frozen=True)
class PendingRevision:
    id: str
    revision_number: int | None
    revision_date: str | None
    created: list[dict[str, Any]] = field(default_factory=list)
    modified: list[dict[str, Any]] = field(default_factory=list)
    deleted: list[dict[str, Any]] = field(default_factory=list)
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "PendingRevision":
        return cls(
            id=str(data.get("id", "")),
            revision_number=data.get("revision_number"),
            revision_date=data.get("revision_date"),
            created=list(data.get("created") or []),
            modified=list(data.get("modified") or []),
            deleted=list(data.get("deleted") or []),
            raw=data,
        )

    @property
    def entries(self) -> list[dict[str, Any]]:
        return [*self.created, *self.modified, *self.deleted]

    @property
    def authority_ids(self) -> list[str]:
        """Every authority whose changes are in this batch, in order, deduped."""
        seen: list[str] = []
        for entry in self.entries:
            aid = entry.get("authority_id")
            if aid is not None and str(aid) not in seen:
                seen.append(str(aid))
        return seen

    @property
    def is_empty(self) -> bool:
        return not self.entries

    def entries_for(self, authority_id: str) -> list[dict[str, Any]]:
        return [e for e in self.entries if str(e.get("authority_id")) == str(authority_id)]

    def __str__(self) -> str:
        return (
            f"revision {self.id} (number {self.revision_number}), "
            f"{len(self.created)} created / {len(self.modified)} modified / "
            f"{len(self.deleted)} deleted, authorities {self.authority_ids or '[]'}"
        )


@dataclass(frozen=True)
class RevisionResult:
    published_revision_id: str
    revision_number: int
    revision_date: str
    authority_ids: list[str]
    next_pending_id: str

    def summary(self) -> str:
        return (
            f"published revision {self.published_revision_id} "
            f"(number {self.revision_number}, {self.revision_date}) "
            f"for {', '.join(self.authority_ids)}; "
            f"next pending is {self.next_pending_id}"
        )


@runtime_checkable
class RevisionClient(Protocol):
    def get(self, path: str) -> Any: ...

    def post(self, path: str, json: Mapping[str, Any] | None = None) -> Any: ...


# ------------------------------------------------------------------ reads


def get_pending(client: RevisionClient) -> PendingRevision:
    return PendingRevision.from_api(client.get(PENDING_PATH))


def get_verified(client: RevisionClient) -> PendingRevision:
    """The verified view. Observed empty even with a pending batch present."""
    return PendingRevision.from_api(client.get("/v1/andromeda/revisions/verified"))


def default_revision_number(date: dt.date | None = None) -> int:
    """Day followed by zero-padded month, matching observed usage.

    >>> default_revision_number(dt.date(2026, 9, 2))
    209
    >>> default_revision_number(dt.date(2026, 9, 22))
    2209
    """
    date = date or dt.date.today()
    return int(f"{date.day}{date.month:02d}")


def revision_number_candidates(base: int, attempts: int = 20):
    """The base number, then the base with a two-digit suffix.

    Numbers are unique environment-wide, so activating twice in one day hits a
    conflict on the date-derived number. Suffixing keeps the date prefix
    readable: 2209, 220901, 220902 -- the same convention used by hand in app
    names like "GD_test_2209_2".
    """
    yield base
    for n in range(1, attempts):
        yield base * 100 + n


def is_number_conflict(exc: BaseException) -> bool:
    """Whether an exception looks like "that revision number is taken".

    The client owns its exception type, so this inspects the message. Pass
    `conflict_check=` to create_revision if your client exposes a status code.
    """
    text = str(exc).lower()
    return "409" in text or "already exists" in text


# ----------------------------------------------------------------- writes


def create_revision(
    client: RevisionClient,
    *,
    revision_number: int | None = None,
    revision_date: dt.date | str | None = None,
    retry_on_conflict: bool = True,
    max_attempts: int = 20,
    conflict_check=is_number_conflict,
) -> tuple[int, str]:
    """Stamp the open pending revision with a number and date. Returns both.

    Revision numbers are unique across the environment -- reusing one gives
    409 "GeofenceRevision record with revision_number N already exists". When
    that happens and `retry_on_conflict` is on, successive candidates are
    tried. An explicit `revision_number` is used as the base, so a conflict on
    it also retries.

    Raises
    ------
    RevisionNumberConflictError
        Every candidate was taken.
    """
    if isinstance(revision_date, dt.date):
        date_str = revision_date.isoformat()
        date_obj = revision_date
    elif isinstance(revision_date, str):
        date_str = revision_date
        date_obj = dt.date.fromisoformat(revision_date)
    else:
        date_obj = dt.date.today()
        date_str = date_obj.isoformat()

    base = revision_number if revision_number is not None else default_revision_number(date_obj)

    if not retry_on_conflict:
        client.post(PENDING_PATH, {"revision_number": base, "revision_date": date_str})
        log.info("created revision number %s dated %s", base, date_str)
        return base, date_str

    tried: list[int] = []
    for number in revision_number_candidates(base, max_attempts):
        tried.append(number)
        try:
            client.post(PENDING_PATH, {"revision_number": number, "revision_date": date_str})
        except Exception as exc:
            if not conflict_check(exc):
                raise
            log.info("revision number %s is already taken, trying the next", number)
            continue
        if len(tried) > 1:
            log.info(
                "created revision number %s dated %s (%d earlier candidate(s) taken)",
                number, date_str, len(tried) - 1,
            )
        else:
            log.info("created revision number %s dated %s", number, date_str)
        return number, date_str

    raise RevisionNumberConflictError(tried)


def publish_revision(client: RevisionClient) -> None:
    """Publish the stamped pending revision. Returns 202 with no body."""
    client.post(ACTIVE_PATH, None)
    log.info("publish requested")


# ---------------------------------------------------------- orchestration


def activate_jurisdiction(
    client: RevisionClient,
    authority_id: str,
    *,
    revision_number: int | None = None,
    revision_date: dt.date | str | None = None,
    allow_other_authorities: bool = False,
    dry_run: bool = False,
    pending: PendingRevision | None = None,
    retry_on_conflict: bool = True,
) -> RevisionResult:
    """Create and publish a revision so the authority's jurisdiction goes active.

    Checks the batch before writing anything, because publishing is
    environment-wide and cannot be undone per authority.

    Pass `pending` when the caller has already read it, so the checks and the
    publish act on the same snapshot rather than two reads that could differ.

    Raises
    ------
    NothingToPublishError
        The pending revision is empty.
    AuthorityNotPendingError
        This authority has nothing in the batch -- likely the jurisdiction was
        never created, or it was already published.
    OtherAuthoritiesPendingError
        The batch includes other authorities and `allow_other_authorities` is
        off.
    PublishNotConfirmedError
        The pending revision id did not change after publishing.
    """
    if pending is None:
        pending = get_pending(client)
    log.info("pending %s", pending)

    if pending.is_empty:
        raise NothingToPublishError(
            f"pending revision {pending.id} holds no changes; "
            "create the jurisdiction first (runbook step 4)"
        )

    mine = pending.entries_for(authority_id)
    if not mine:
        raise AuthorityNotPendingError(authority_id, pending.authority_ids)

    others = [a for a in pending.authority_ids if a != str(authority_id)]
    if others:
        if not allow_other_authorities:
            raise OtherAuthoritiesPendingError(authority_id, others)
        log.warning(
            "publishing will also activate changes for %s", ", ".join(others)
        )

    for entry in mine:
        log.info(
            "  jurisdiction %s: ingress_status=%s egress_status=%s, %d shape(s)",
            entry.get("id"), entry.get("ingress_status"), entry.get("egress_status"),
            len(entry.get("shapes") or []),
        )

    if dry_run:
        number = revision_number if revision_number is not None else default_revision_number()
        log.info(
            "dry run: would stamp revision %s as number %s and publish it",
            pending.id, number,
        )
        return RevisionResult(
            published_revision_id=pending.id,
            revision_number=number,
            revision_date=str(revision_date or dt.date.today()),
            authority_ids=pending.authority_ids,
            next_pending_id="",
        )

    number, date_str = create_revision(
        client,
        revision_number=revision_number,
        revision_date=revision_date,
        retry_on_conflict=retry_on_conflict,
    )
    publish_revision(client)

    after = get_pending(client)
    if after.id == pending.id:
        raise PublishNotConfirmedError(pending.id)

    # The queue does NOT empty -- entries with ingress_status Pending reappear
    # in the new revision. That is expected; do not treat it as failure.
    log.info(
        "published revision %s; next pending is %s%s",
        pending.id, after.id,
        f" (still holding {len(after.entries)} entr(y/ies))" if not after.is_empty else "",
    )

    return RevisionResult(
        published_revision_id=pending.id,
        revision_number=number,
        revision_date=date_str,
        authority_ids=pending.authority_ids,
        next_pending_id=after.id,
    )