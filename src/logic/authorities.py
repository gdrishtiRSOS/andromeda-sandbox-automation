"""Complete an authority's Account Info.

    GET /v1/andromeda/authorities/{authorityId}
    PUT /v1/andromeda/authorities/{authorityId}
        {id, name, display_name, account_id, dispatch_type, organization_id,
         attributes: {contact_name, contact_email, contact_phone, contact_title,
                      country, state, non_emergency_phone, population,
                      phone_system, cad_system, mapping_system}}

Design notes
------------
* **It is a PUT, so the whole object is replaced.** The current record is read
  first and the requested fields laid over it. Building the body from scratch
  would blank every field the caller did not mention -- including
  organization_id, which links the authority to its Scorpius org.

* `account_id` is the free-text Account ID from the runbook (e.g. "SAND_0209"),
  not the numeric `authorityId` that appears in every URL. Two different
  identifiers with confusingly similar names; this module only ever puts the
  free-text one in `account_id`.

* `dispatch_type` defaults to DEFAULT_DISPATCH_TYPE because every sandbox
  account uses the same one. Setting it again is a no-op, so this costs
  nothing on an account that already has it. `dispatch_type=None` leaves the
  field untouched.

* Country and state are codes -- "IRL", "LK" -- served by the dropdown
  endpoints. They are validated against those catalogs before writing, because
  an unrecognised code is accepted by the API and quietly produces an account
  that looks configured.

* There is no server-side search for authorities. The list endpoint is
  `GET /v1/andromeda/authorities?page=N&limit=M` returning
  `{authorities: [...], total_records: N}`, newest first, and the UI filters
  in the browser. `find_authority` therefore pages through them -- about three
  requests at limit=500 for ~1300 records -- and reads every page rather than
  stopping at the first hit, so duplicate names are detected instead of one
  being picked arbitrarily.

* Saving also triggers a background T911 credential check against a different
  host. Nothing to do with this module; noted so it is not mistaken for our
  traffic when reading a capture.
"""

from __future__ import annotations

import difflib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Protocol, runtime_checkable

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_DISPATCH_TYPE",
    "IMMUTABLE_ONCE_SET",
    "AccountInfoReport",
    "AmbiguousAuthorityError",
    "AuthorityNotFoundError",
    "AuthorityClient",
    "AccountInfoError",
    "UnknownCountryError",
    "UnknownStateError",
    "find_authority",
    "get_authority",
    "iter_authorities",
    "resolve_authority_id",
    "list_countries",
    "list_states",
    "update_account_info",
]

#: Fields that live directly on the authority record.
TOP_LEVEL_FIELDS = ("account_id", "dispatch_type", "name", "display_name")

#: Sandbox accounts always use dispatch type 1. The runbook requires one to be
#: attached and never varies it, so callers get it by default rather than
#: having to remember. Pass `dispatch_type=None` to leave the field alone.
DEFAULT_DISPATCH_TYPE = 1

#: Fields the API refuses to change once they hold a value. Attempting one
#: fails the whole PUT with 400 "Cannot update <Label> once set.", so they are
#: dropped before writing rather than losing the other fields with them.
IMMUTABLE_ONCE_SET = ("account_id",)

#: The labels those 400s use, mapped back to field names, so a rejection that
#: slips past the pre-check can still be retried without the offending field.
REJECTION_LABELS = {
    "account id": "account_id",
    "dispatch type": "dispatch_type",
    "organization": "organization_id",
}

#: Fields that live under `attributes`.
ATTRIBUTE_FIELDS = (
    "country", "state", "contact_name", "contact_email", "contact_phone",
    "contact_title", "non_emergency_phone", "population", "phone_system",
    "cad_system", "mapping_system",
)


class AccountInfoError(Exception):
    """Base for this module."""


class UnknownCountryError(AccountInfoError):
    def __init__(self, code: str, available: Mapping[str, str]):
        self.code = code
        self.available = dict(available)
        listing = ", ".join(f"{c} ({n})" for c, n in sorted(available.items()))
        super().__init__(f"no country code {code!r}. Available: {listing}")


class UnknownStateError(AccountInfoError):
    def __init__(self, code: str, country: str, available: Mapping[str, str]):
        self.code = code
        self.country = country
        self.available = dict(available)
        listing = ", ".join(sorted(available)) or "(none returned)"
        super().__init__(
            f"no state/region code {code!r} in {country}. Available: {listing}"
        )


class AuthorityNotFoundError(AccountInfoError):
    def __init__(self, name: str, near: list[str], searched: int):
        self.name = name
        self.near = list(near)
        self.searched = searched
        message = f"no authority named {name!r} among {searched} searched"
        if near:
            message += ". Did you mean: " + ", ".join(repr(n) for n in near)
        super().__init__(message)


class AmbiguousAuthorityError(AccountInfoError):
    """More than one authority matched. Never guess -- configuring the wrong
    one would apply an entire account's settings to somebody else's."""

    def __init__(self, name: str, matches: list[dict]):
        self.name = name
        self.matches = list(matches)
        listing = ", ".join(
            f"id {m.get('id')} (account_id {m.get('account_id')!r})" for m in matches
        )
        super().__init__(
            f"{len(matches)} authorities are named {name!r}: {listing}. "
            f"Pass the numeric id instead."
        )


@dataclass
class AccountInfoReport:
    authority_id: str
    changed: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    applied: bool = False
    skipped: dict[str, str] = field(default_factory=dict)
    fallback_used: bool = False

    @property
    def is_noop(self) -> bool:
        return not self.changed

    def summary(self) -> str:
        parts = []
        if self.changed:
            parts.append(
                f"{len(self.changed)} field(s) "
                f"{'updated' if self.applied else 'to update'} "
                f"({', '.join(sorted(self.changed))})"
            )
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped ({', '.join(sorted(self.skipped))})")
        if not parts:
            parts.append("already up to date")
        return f"authority {self.authority_id}: " + "; ".join(parts)


@runtime_checkable
class AuthorityClient(Protocol):
    def get(self, path: str) -> Any: ...

    def put(self, path: str, json: Mapping[str, Any]) -> Any: ...


# ------------------------------------------------------------------ reads


def get_authority(client: AuthorityClient, authority_id: str) -> dict[str, Any]:
    return client.get(f"/v1/andromeda/authorities/{authority_id}")


def iter_authorities(
    client: AuthorityClient,
    *,
    limit: int = 500,
    max_pages: int = 50,
) -> Iterator[dict[str, Any]]:
    """Every authority, a page at a time.

    There is no server-side search: the UI loads a page and filters it in the
    browser. So finding one by name means paging through them. The list comes
    back newest-first, which makes a freshly created authority cheap to reach,
    but every page is still read so duplicates can be detected.
    """
    seen = 0
    for page in range(1, max_pages + 1):
        payload = client.get(f"/v1/andromeda/authorities?page={page}&limit={limit}")
        if isinstance(payload, Mapping):
            batch = payload.get("authorities") or payload.get("results") or []
            total = payload.get("total_records")
        else:
            batch = payload or []
            total = None

        if not batch:
            return
        yield from batch
        seen += len(batch)

        if total is not None:
            # Trust the count over the page size: the server may cap `limit`
            # below what we asked for, and a short page would otherwise look
            # like the end of the list.
            if seen >= total:
                return
        elif len(batch) < limit:
            return

    log.warning("stopped after %d pages (%d authorities); raise max_pages if the "
                "environment has more", max_pages, seen)


def find_authority(
    client: AuthorityClient,
    name: str | None = None,
    *,
    account_id: str | None = None,
    case_sensitive: bool = False,
    limit: int = 500,
) -> dict[str, Any]:
    """Look up one authority by name (or account_id). Returns its record.

    Matching is exact -- not a substring -- and case-insensitive by default.
    `display_name` counts as well as `name`, since the two can differ.

    Raises
    ------
    AuthorityNotFoundError
        Nothing matched. Close names are suggested, which catches typos.
    AmbiguousAuthorityError
        Several matched. Never resolved by guessing.
    """
    if not name and not account_id:
        raise AccountInfoError("pass a name or an account_id")

    def norm(value: Any) -> str:
        text = "" if value is None else str(value)
        return text if case_sensitive else text.casefold()

    wanted_name = norm(name) if name else None
    wanted_account = norm(account_id) if account_id else None

    matches: list[dict[str, Any]] = []
    all_names: list[str] = []
    searched = 0

    for record in iter_authorities(client, limit=limit):
        searched += 1
        record_name = record.get("name")
        if record_name:
            all_names.append(str(record_name))

        if wanted_name is not None:
            if norm(record_name) == wanted_name or norm(record.get("display_name")) == wanted_name:
                matches.append(record)
                continue
        if wanted_account is not None and norm(record.get("account_id")) == wanted_account:
            matches.append(record)

    if len(matches) == 1:
        found = matches[0]
        log.info(
            "resolved %r to authority %s (account_id %r)",
            name or account_id, found.get("id"), found.get("account_id"),
        )
        return found
    if len(matches) > 1:
        raise AmbiguousAuthorityError(name or account_id or "", matches)

    near = difflib.get_close_matches(str(name or account_id), all_names, n=5, cutoff=0.6)
    raise AuthorityNotFoundError(str(name or account_id), near, searched)


def resolve_authority_id(
    client: AuthorityClient,
    name_or_id: str,
    *,
    limit: int = 500,
) -> str:
    """Accept either a numeric id or a name, and return the id.

    Lets callers take '4958' or 'gDTest' without caring which they were given.
    """
    text = str(name_or_id).strip()
    if text.isdigit():
        return text
    return str(find_authority(client, text, limit=limit)["id"])


def _codes_from(payload: Any) -> dict[str, str]:
    """Pull {code: name} out of a dropdown response.

    The catalog's exact shape is not captured anywhere, so several plausible
    ones are handled. An unrecognised shape yields an empty mapping, which
    callers treat as "cannot validate" rather than "nothing exists".
    """
    if isinstance(payload, Mapping):
        for key in ("countries", "states", "results", "data", "items"):
            if key in payload:
                return _codes_from(payload[key])
        # already a {code: name} mapping?
        if payload and all(isinstance(v, str) for v in payload.values()):
            return {str(k): v for k, v in payload.items()}
        return {}

    if not isinstance(payload, list):
        return {}

    out: dict[str, str] = {}
    for item in payload:
        if isinstance(item, str):
            out[item] = item
            continue
        if not isinstance(item, Mapping):
            continue
        code = next(
            (item[k] for k in ("code", "alpha3", "alpha_3", "iso3", "abbreviation", "id")
             if item.get(k)),
            None,
        )
        name = next(
            (item[k] for k in ("name", "display_name", "label") if item.get(k)),
            None,
        )
        if code is not None:
            out[str(code)] = str(name if name is not None else code)
    return out


def list_countries(client: AuthorityClient) -> dict[str, str]:
    """Country code -> name, from the dropdown endpoint."""
    return _codes_from(client.get("/v1/andromeda/country"))


def list_states(client: AuthorityClient, country_code: str) -> dict[str, str]:
    """State/region code -> name for one country."""
    return _codes_from(client.get(f"/v1/andromeda/country/{country_code}"))


# ----------------------------------------------------------------- write


def _rejected_field(message: str) -> str | None:
    """The field name a 'Cannot update X once set' error refers to, if any."""
    lowered = str(message).lower()
    if "cannot update" not in lowered:
        return None
    for label, name in REJECTION_LABELS.items():
        if label in lowered:
            return name
    return None


def _plan(current: Mapping[str, Any], updates: Mapping[str, Any]) -> tuple[dict, dict]:
    """Overlay `updates` onto a copy of `current`. Returns (body, changes)."""
    body = json.loads(json.dumps(dict(current)))
    body.setdefault("attributes", {})
    changes: dict[str, tuple[Any, Any]] = {}

    for name, value in updates.items():
        if value is None:
            continue
        if name in TOP_LEVEL_FIELDS:
            before = body.get(name)
            if before != value:
                changes[name] = (before, value)
            body[name] = value
        elif name in ATTRIBUTE_FIELDS:
            before = body["attributes"].get(name)
            if before != value:
                changes[name] = (before, value)
            body["attributes"][name] = value
        else:
            raise AccountInfoError(
                f"unknown field {name!r}; expected one of "
                f"{', '.join(sorted(TOP_LEVEL_FIELDS + ATTRIBUTE_FIELDS))}"
            )

    # revision fields are server-owned; never send them back
    body.pop("revision_introduced", None)
    body.pop("revision_last_modified", None)
    return body, changes


def update_account_info(
    client: AuthorityClient,
    authority_id: str,
    *,
    account_id: str | None = None,
    dispatch_type: int | None = DEFAULT_DISPATCH_TYPE,
    country: str | None = None,
    state: str | None = None,
    validate: bool = True,
    dry_run: bool = False,
    verify: bool = True,
    skip_immutable: bool = True,
    **attributes: Any,
) -> AccountInfoReport:
    """Fill in the Account Info tab.

    Only the fields you pass are changed; everything else on the record is
    preserved. Extra keyword arguments set `attributes` fields such as
    `contact_name`, `contact_email` or `population`.

    `dispatch_type` defaults to DEFAULT_DISPATCH_TYPE, which every sandbox
    account uses. Pass `None` to leave whatever is already there.

    Fields the API refuses to change once set -- `account_id` is one -- are
    dropped with a warning rather than failing the whole update, and listed in
    the report's `skipped`. Pass `skip_immutable=False` to let the 400 through.

    Raises
    ------
    UnknownCountryError, UnknownStateError
        The code is not in the dropdown catalog. Skipped when `validate` is
        off, or when the catalog cannot be read.
    AccountInfoError
        An unrecognised field name was passed.
    """
    updates: dict[str, Any] = {
        "account_id": account_id,
        "dispatch_type": dispatch_type,
        "country": country,
        "state": state,
        **attributes,
    }

    if validate and (country is not None or state is not None):
        countries = list_countries(client)
        if country is not None and countries:
            if country not in countries:
                raise UnknownCountryError(country, countries)
            log.debug("country %s is %s", country, countries[country])
        elif country is not None:
            log.warning("could not read the country catalog; skipping validation")

        if state is not None:
            lookup_country = country
            if lookup_country is None:
                lookup_country = (get_authority(client, authority_id)
                                  .get("attributes", {}).get("country"))
            if lookup_country:
                states = list_states(client, lookup_country)
                if states and state not in states:
                    raise UnknownStateError(state, lookup_country, states)
                if not states:
                    log.warning("could not read the state catalog for %s; "
                                "skipping validation", lookup_country)

    current = get_authority(client, authority_id)
    body, changes = _plan(current, updates)
    report = AccountInfoReport(authority_id=str(authority_id), changed=changes)

    # Drop fields the API will refuse. Doing this before the write keeps the
    # rest of the update, rather than losing everything to one 400.
    if skip_immutable:
        for name in IMMUTABLE_ONCE_SET:
            if name in changes and current.get(name):
                before, after = changes.pop(name)
                body[name] = before
                report.skipped[name] = (
                    f"already set to {before!r}; the API refuses to change it"
                )
                log.warning(
                    "not changing %s: already set to %r (wanted %r). "
                    "Andromeda refuses to update it once set.",
                    name, before, after,
                )

    for name, (before, after) in sorted(changes.items()):
        log.info("  %s: %r -> %r", name, before, after)

    if report.is_noop:
        log.info("authority %s already has these values", authority_id)
        return report

    if dry_run:
        log.info("dry run: %d field(s) not written", len(changes))
        return report

    try:
        result = client.put(f"/v1/andromeda/authorities/{authority_id}", body)
    except Exception as exc:
        name = _rejected_field(exc) if skip_immutable else None
        if name is None or name not in changes:
            raise
        # The value changed between our read and the write, or the field is
        # immutable in a way the pre-check does not know about.
        before, after = changes.pop(name)
        body[name] = current.get(name)
        report.skipped[name] = f"rejected by the API: {exc}"
        report.fallback_used = True
        log.warning("%s was rejected (%s); retrying without it", name, exc)
        if not changes:
            log.info("nothing left to update once %s is dropped", name)
            return report
        result = client.put(f"/v1/andromeda/authorities/{authority_id}", body)

    report.applied = True

    if verify and isinstance(result, Mapping):
        stored = dict(result)
        attrs = stored.get("attributes") or {}
        mismatched = []
        for name, (_, wanted) in changes.items():
            got = stored.get(name) if name in TOP_LEVEL_FIELDS else attrs.get(name)
            if got != wanted:
                mismatched.append((name, wanted, got))
        if mismatched:
            for name, wanted, got in mismatched:
                log.error("field %s: requested %r, server stored %r", name, wanted, got)
            raise AccountInfoError(
                f"{len(mismatched)} field(s) did not persist as requested: "
                + ", ".join(n for n, _, _ in mismatched)
            )

    log.info("updated %d field(s) on authority %s", len(changes), authority_id)
    return report