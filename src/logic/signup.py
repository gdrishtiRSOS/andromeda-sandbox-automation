"""Create an account through the sign-up wizard's API -- runbook step 1.

Three calls, all on the portal API host and all **unauthenticated**:

    POST /v1/scorpius/user/register   {email, password, first_name, last_name,
                                       organization_name, application}   -> 200
    POST /v1/capstone/psaps/          {name, display_name, state, fcc_id,
                                       contact_*, non_emergency_phone,
                                       population, phone_system, cad_system,
                                       mapping_system}                   -> 500
    POST /v1/scorpius/user/confirm    {token}                            -> 204

Design notes
------------
* **No token is needed.** Captured traffic carries no Authorization header on
  any of the three, so this module runs before anything else and needs no
  credential of its own.

* **The PSAP call answers 500 but the data lands.** Confirmed against a real
  signup: everything submitted appeared on the authority in Andromeda
  afterwards. The status is therefore not a reliable outcome signal and is
  tolerated -- but tolerated loudly, and the caller should verify by finding
  the authority rather than trusting either the 500 or this module. A 4xx is
  *not* tolerated: that means the payload was wrong.

* **Neither reCAPTCHA nor Formsite participates.** Both load in the browser --
  the captcha script and the license-agreement iframe -- but the capture shows
  no captcha token in the register body and no POST to formsite.com at all.
  The signature and checkbox on wizard step 3 are submitted nowhere.

* **The confirmation token cannot be generated.** It is a JWT holding only
  `{email, type: "confirmation"}` -- no expiry, no nonce -- but signed with a
  server secret, so it has to come from the email. `confirm_email` accepts
  either the raw token or the whole link pasted from the message.

* Confirmation is not a prerequisite for the Andromeda work: the authority is
  present and configurable immediately after signup. So the sensible shape is
  create, configure, then ask a human to click the link.

* The password is fixed at `DEFAULT_PASSWORD`. The API enforces complexity
  rules it does not document, and a shared value means anyone on the team can
  log into a generated sandbox account.

* Agency Name becomes the authority's name in Andromeda. There is no Account
  ID here -- that is set separately in runbook step 2.

* **A generated account can log itself in.** `POST .../api-token-auth` with the
  account's email and DEFAULT_PASSWORD returns a token that can read *and*
  write that organization's roles -- verified against a real account. So the
  portal side of runbook step 8 needs no pasted credential: the tool signs in
  as the account it created. Andromeda still does, because it authenticates
  through Google SSO and answers 403 to a Scorpius token from this user.

* Logging in does **not** require the address to be confirmed.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable
from urllib.parse import parse_qs, urlparse

from logic.tokens import read_claims

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULTS",
    "DEFAULT_PASSWORD",
    "PortalSession",
    "PsapDetails",
    "SignupClient",
    "SignupError",
    "SignupResult",
    "confirm_email",
    "create_psap",
    "default_password",
    "log_in",
    "refresh_session",
    "register",
    "sign_up",
    "token_from_link",
]

REGISTER_PATH = "/v1/scorpius/user/register"
LOGIN_PATH = "/v1/scorpius/user/api-token-auth"
REFRESH_PATH = "/v1/scorpius/user/api-token-auth/refresh"
PSAP_PATH = "/v1/capstone/psaps/"
CONFIRM_PATH = "/v1/scorpius/user/confirm"

#: "Other" for each of the three system dropdowns on wizard step 4.
OTHER = 0

#: The password every generated account gets. The API enforces rules it does
#: not publish -- a value derived from the address's +tag was rejected with
#: {"password": ["Invalid value."]}, most likely for lacking an uppercase
#: letter. This one satisfies length, case, digit and symbol, and being fixed
#: means one known password rather than one per account.
#:
#: These are throwaway sandbox accounts, so a shared password is the point:
#: anyone on the team can log in to a generated account without asking who
#: made it. Do not reuse this pattern anywhere real.
DEFAULT_PASSWORD = "AutomatedAccount123!"

#: The values the runbook fixes for a sandbox account. State is required by
#: the API but meaningless here -- runbook step 2 sets the real one.
DEFAULTS: dict[str, Any] = {
    "contact_title": "Automated",
    "contact_phone": "12125551234",
    "non_emergency_phone": "12125551234",
    "population": 12345,
    "state": "AL",
    "fcc_id": "",
    "phone_system": OTHER,
    "cad_system": OTHER,
    "mapping_system": OTHER,
}


class SignupError(Exception):
    """Base for this module."""


@dataclass(frozen=True)
class PsapDetails:
    """The agency details wizard steps 2 and 4 collect."""

    name: str
    contact_name: str
    contact_email: str
    display_name: str | None = None
    contact_title: str = DEFAULTS["contact_title"]
    contact_phone: str = DEFAULTS["contact_phone"]
    non_emergency_phone: str = DEFAULTS["non_emergency_phone"]
    population: int = DEFAULTS["population"]
    state: str = DEFAULTS["state"]
    fcc_id: str = DEFAULTS["fcc_id"]
    phone_system: int = OTHER
    cad_system: int = OTHER
    mapping_system: int = OTHER

    def to_body(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name or self.name,
            "state": self.state,
            "fcc_id": self.fcc_id,
            "contact_name": self.contact_name,
            "contact_title": self.contact_title,
            "contact_email": self.contact_email,
            "contact_phone": self.contact_phone,
            "non_emergency_phone": self.non_emergency_phone,
            "population": self.population,
            "phone_system": self.phone_system,
            "cad_system": self.cad_system,
            "mapping_system": self.mapping_system,
        }


@dataclass
class SignupResult:
    email: str
    agency_name: str
    user_id: str | None = None
    organization_id: str | None = None
    psap_status: str = "not attempted"
    confirmed: bool = False
    raw_user: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def needs_confirmation(self) -> bool:
        return not self.confirmed

    def summary(self) -> str:
        parts = [f"{self.agency_name!r} for {self.email}"]
        if self.user_id:
            parts.append(f"user {self.user_id}")
        if self.organization_id:
            parts.append(f"organization {self.organization_id}")
        parts.append(f"psap {self.psap_status}")
        parts.append("confirmed" if self.confirmed else "awaiting email confirmation")
        return "; ".join(parts)


@runtime_checkable
class SignupClient(Protocol):
    """The portal API client. No authentication is required for these calls."""

    def post(self, path: str, json: Mapping[str, Any]) -> Any: ...


# --------------------------------------------------------------- helpers


def default_password(email: str | None = None) -> str:
    """The password generated accounts are given.

    Fixed rather than derived. A tag-derived password was rejected by the API
    with {"password": ["Invalid value."]} -- it enforces unpublished
    complexity rules -- and a single known value is more useful anyway, since
    any of these throwaway accounts can then be logged into by anyone on the
    team.

    `email` is accepted and ignored, so callers need not special-case it.
    """
    return DEFAULT_PASSWORD


def token_from_link(value: str) -> str:
    """Accept either the raw confirmation token or the whole link.

    The email's link looks like
    https://sandbox.rapidsosportal.com/confirmation-email/?token=eyJ...
    so both forms are useful to paste.
    """
    text = value.strip()
    if not text:
        raise SignupError("no confirmation token given")
    if "://" not in text and "token=" not in text:
        return text
    query = parse_qs(urlparse(text).query)
    token = (query.get("token") or [None])[0]
    if not token:
        match = re.search(r"token=([A-Za-z0-9._~-]+)", text)
        token = match.group(1) if match else None
    if not token:
        raise SignupError(f"no token= found in {value[:60]!r}")
    return token


# ----------------------------------------------------------------- calls


def register(
    client: SignupClient,
    *,
    email: str,
    password: str,
    first_name: str,
    last_name: str,
    organization_name: str,
    application: str = "capstone",
) -> dict[str, Any]:
    """Wizard step 1. Creates the user and the organization.

    Email addresses cannot be reused, so a second signup needs a different
    plus-tag.
    """
    body = {
        "email": email,
        "password": password,
        "first_name": first_name,
        "last_name": last_name,
        "organization_name": organization_name,
        "application": application,
    }
    data = client.post(REGISTER_PATH, body)
    if not isinstance(data, Mapping):
        raise SignupError(f"register returned {type(data).__name__}, expected an object")
    log.info(
        "registered %s as user %s in organization %s",
        email, data.get("id"),
        (data.get("organizations") or [{}])[0].get("id"),
    )
    return dict(data)


def create_psap(
    client: SignupClient,
    details: PsapDetails,
    *,
    tolerate_server_error: bool = True,
) -> str:
    """Wizard steps 2 and 4. Returns a short status word.

    The API answers 500 while still storing the data. That is tolerated, but
    a 4xx is not -- that means the payload itself was rejected.
    """
    try:
        client.post(PSAP_PATH, details.to_body())
    except Exception as exc:
        text = str(exc)
        server_error = bool(re.search(r"\b5\d\d\b", text))
        if not (tolerate_server_error and server_error):
            raise
        log.warning(
            "PSAP creation answered a server error (%s). Known behaviour: the "
            "data is stored anyway. Verify the authority in Andromeda before "
            "relying on it.", text[:160],
        )
        return "500 (data usually lands anyway)"
    log.info("PSAP created for %s", details.name)
    return "ok"


def confirm_email(client: SignupClient, token_or_link: str) -> None:
    """Confirm the address. Accepts the token or the whole emailed link."""
    token = token_from_link(token_or_link)
    client.post(CONFIRM_PATH, {"token": token})
    log.info("email confirmed")


# --------------------------------------------------------- orchestration


def sign_up(
    client: SignupClient,
    *,
    email: str,
    agency_name: str,
    first_name: str,
    last_name: str,
    password: str | None = None,
    organization_name: str | None = None,
    details: PsapDetails | None = None,
    dry_run: bool = False,
    **detail_overrides: Any,
) -> SignupResult:
    """Run the sign-up wizard: register, then create the PSAP.

    Confirmation is deliberately not attempted -- the token only exists in the
    email. The result says whether it is still outstanding.

    `organization_name` defaults to the agency name, which is what the wizard
    submits and what becomes the authority's name in Andromeda.

    Raises
    ------
    SignupError
        Registration returned something unusable, or a password could not be
        derived from the address.
    """
    password = password or default_password(email)
    organization_name = organization_name or agency_name

    if details is None:
        details = PsapDetails(
            name=agency_name,
            contact_name=f"{first_name} {last_name}".strip(),
            contact_email=email,
            **detail_overrides,
        )
    elif detail_overrides:
        raise SignupError("pass either `details` or individual overrides, not both")

    result = SignupResult(email=email, agency_name=agency_name)

    if dry_run:
        log.info(
            "dry run: would register %s (organization %r) and create the PSAP %r",
            email, organization_name, details.name,
        )
        return result

    user = register(
        client,
        email=email,
        password=password,
        first_name=first_name,
        last_name=last_name,
        organization_name=organization_name,
    )
    result.raw_user = user
    result.user_id = str(user.get("id") or "") or None
    orgs = user.get("organizations") or []
    result.organization_id = str(orgs[0].get("id")) if orgs else None

    result.psap_status = create_psap(client, details)

    log.info(
        "signup complete: %s. The address still needs confirming -- open the "
        "email sent to %s and follow step 1 of its instructions.",
        result.summary(), email,
    )
    return result


# ------------------------------------------------------- logging back in


@dataclass(frozen=True)
class PortalSession:
    """A portal token obtained by logging in, rather than pasted."""

    token: str
    refresh_token: str | None = None
    username: str | None = None
    expires_at: dt.datetime | None = None

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "PortalSession":
        token = data.get("token") or data.get("access_token")
        if not token:
            raise SignupError(
                f"no token in the login response; keys were {sorted(data)}"
            )
        claims = read_claims(token)
        expires = None
        if isinstance(claims.get("exp"), (int, float)):
            expires = dt.datetime.fromtimestamp(claims["exp"], tz=dt.timezone.utc)
        return cls(
            token=token,
            refresh_token=data.get("refresh_token"),
            username=claims.get("username") or claims.get("email"),
            expires_at=expires,
        )

    def seconds_left(self) -> float | None:
        if self.expires_at is None:
            return None
        return (self.expires_at - dt.datetime.now(dt.timezone.utc)).total_seconds()

    def __repr__(self) -> str:                      # never print the token
        left = self.seconds_left()
        when = f", {left / 60:.0f} min left" if left is not None else ""
        return f"PortalSession({self.username or 'unknown'}{when})"


def log_in(
    client: SignupClient,
    email: str,
    password: str | None = None,
) -> PortalSession:
    """Get a portal token for an account, by logging in as it.

    `password` defaults to DEFAULT_PASSWORD, which every generated account
    uses. The resulting token is scoped to that account's own organization --
    enough for runbook step 8, not enough for Andromeda.

    The address does not need to be confirmed first.
    """
    data = client.post(LOGIN_PATH, {"email": email, "password": password or DEFAULT_PASSWORD})
    if not isinstance(data, Mapping):
        raise SignupError(f"login returned {type(data).__name__}, expected an object")
    session = PortalSession.from_api(data)
    log.info("logged in to the portal as %s", session.username or email)
    return session


def refresh_session(client: SignupClient, refresh_token: str) -> PortalSession:
    """Trade a refresh token for a new access token."""
    data = client.post(REFRESH_PATH, {"refresh_token": refresh_token})
    if not isinstance(data, Mapping):
        raise SignupError(f"refresh returned {type(data).__name__}, expected an object")
    session = PortalSession.from_api(data)
    log.info("refreshed the portal session")
    return session