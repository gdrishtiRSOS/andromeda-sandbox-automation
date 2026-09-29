"""Routes for the local sandbox-account page.

Thin on purpose: validate input, build clients, call a function from
`logic`, stream what happens. The business logic -- and every refusal --
lives in the package, where the CLI and the tests use it too.

    Sources   GET  /api/capability-sources       (copy another account's set)
              GET  /api/boundary-sources         (copy another account's boundary)
    Phase 1   POST /api/runs (preview) -> POST /api/runs/{id}/signup
    Gate      POST /api/runs/{id}/confirm        (optional: paste the link)
    Phase 2   POST /api/runs/{id}/continue       -> workflows.configure_account
    Progress  GET  /api/runs/{id}/events         (Server-Sent Events)
    Sign-in   GET  /api/session                  (who, for how long)
              POST /api/session/sign-in          -> browser_auth.sign_in
              GET  /api/session/sign-in/{id}/events
              POST /api/session/har              (fallback without Playwright)
              POST /api/session/sign-out

Nothing is pasted. The Andromeda token comes from ANDROMEDA_TOKEN if it is
set, otherwise from the session a sign-in stored (see `webapp.session`); it is
used for the one request or worker that needs it, and never sent to the page,
logged or put in an event. The portal side (roles) needs no credential either:
the app logs in as the account itself -- its email and the shared sandbox
password -- whenever it needs to, and that session is handled the same way.

Because the server now holds the credential, a page on another site must not
be able to drive it: every request that is not a read needs this page's own
Origin (when one is sent) and its X-Sandbox-Page header, which a cross-site
request cannot carry without a preflight this app never answers.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from logic import browser_auth
from logic.auth import SCORPIUS_BASE, TokenCache
from logic.authorities import (
    AmbiguousAuthorityError,
    AuthorityNotFoundError,
    find_authority,
    get_authority,
    list_countries,
    list_states,
)
from logic.capabilities import STANDARD_PATH, CapabilityError, load_standard
from logic.capabilities import plan as plan_capabilities
from logic.integrations import DEFAULT_PRODUCT, list_integrations
from logic.jurisdictions import (
    IngressStatus,
    InvalidGeometryError,
    JurisdictionError,
    bbox,
    export_boundary,
    list_jurisdictions,
    normalize_geojson,
    validate_geojson,
)
from logic.places import AmbiguousPlaceError, PlaceError, PlaceNotFoundError, UnusableBoundaryError
from logic.revisions import OtherAuthoritiesPendingError
from logic.roles import list_roles, restore_roles
from logic.signup import (
    DEFAULTS,
    PSAP_PATH,
    REGISTER_PATH,
    PortalSession,
    SignupError,
    confirm_email,
    default_password,
    log_in,
    sign_up,
)
from logic.tokens import decode_token
from logic.workflows import (
    CONFIGURE_STEPS,
    PartialConfigureError,
    authority_names,
    check_new_account,
    configure_account,
    next_action,
    plan_roles_restore,
    restore_capabilities,
    root_cause,
)
from logic.client import ApiError, AuthError, HttpClient
from webapp.lookup import PlaceLookup, candidate_choices, place_summary
from webapp.runs import QUIET, Run, RunBusyError, Runs, capture_logs
from webapp.session import (
    SIGN_IN_QUIET,
    AndromedaAuth,
    HarRejected,
    NoSession,
    SignInBusy,
    SignInRefused,
    playwright_installed,
)

log = logging.getLogger(__name__)

ANDROMEDA_BASE = "https://andromeda.sandbox.rapidsos.com"
PORTAL_BASE = "https://api-sandbox.rapidsosportal.com"
ORG_HEADER = "RapidSOS Admin"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
#: Both are credentials, and gitignored. One per person, never shared.
SESSION_FILE = PROJECT_ROOT / ".andromeda-session.json"
BROWSER_PROFILE = PROJECT_ROOT / ".andromeda-browser"
#: The header this page sends on every write; see the module docstring.
PAGE_HEADER = "x-sandbox-page"

#: Warn before starting phase 2 when a token has less than this left.
EXPIRY_WARNING_SECONDS = 10 * 60
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
#: A sign-in capture is mostly page assets; a real one is around 10 MB.
MAX_HAR_BYTES = 200 * 1024 * 1024
#: A copied capability set needs an explicit acknowledgement when at least
#: this share of its names are absent from the sandbox catalog.
UNKNOWN_CAPABILITY_WARNING = 0.25
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

ClientFactory = Callable[..., Any]


# ------------------------------------------------------------------ models


class FormIn(BaseModel):
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    agency_name: str = ""
    boundary_id: Optional[str] = None
    account_id: Optional[str] = None
    country: Optional[str] = None
    state: Optional[str] = None
    capabilities_source: str = "default"          # "default" | "copy"
    capabilities_capture_id: Optional[str] = None


class SignupIn(BaseModel):
    acknowledge_capabilities: bool = False


class BoundaryFromIn(BaseModel):
    authority_id: str
    jurisdiction_id: str


class ConfirmIn(BaseModel):
    link: str


class ContinueIn(BaseModel):
    clicked_link: bool = False
    acknowledge_expiry: bool = False
    acknowledge_capabilities: bool = False
    email: Optional[str] = None       # resume only: the account to log in as


class ResumeIn(BaseModel):
    authority: str
    email: Optional[str] = None       # defaults to the authority's contact email
    boundary_id: Optional[str] = None
    account_id: Optional[str] = None
    country: Optional[str] = None
    state: Optional[str] = None
    capabilities_capture_id: Optional[str] = None


class RestoreIn(BaseModel):
    kind: str                         # "capabilities" | "roles"
    apply: bool = False


# ----------------------------------------------------------------- helpers


def _error(code: int, /, **body: Any) -> JSONResponse:
    return JSONResponse(status_code=code, content=body)


def _clean(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip()
    return value or None


def _contact_email(record: Dict[str, Any]) -> Optional[str]:
    """The address sign-up put on the authority -- the account's login."""
    return _clean(str((record.get("attributes") or {}).get("contact_email") or ""))


class LoginFailed(Exception):
    """Logging in as the account did not work; the message says why."""


def _token_report(token: Optional[str]) -> Dict[str, Any]:
    info = decode_token(token)
    left = info.seconds_left()
    return {
        "present": bool(token),
        "ok": info.ok and not info.expired(),
        "problem": info.problem or ("expired -- copy a fresh one" if info.expired() else None),
        "user": info.user,
        "subject": info.subject,
        "expires_at": info.expires_at.isoformat() if info.expires_at else None,
        "seconds_left": left,
        "expiring": left is not None and 0 < left < EXPIRY_WARNING_SECONDS,
    }


def _describe_api_error(exc: Exception, host: str) -> str:
    if isinstance(exc, AuthError):
        again = ("sign in again at the top of the page" if host == "Andromeda"
                 else "press Resume to log in again")
        return f"{host} rejected the token ({exc.status}). It has most likely expired; {again}."
    if isinstance(exc, ApiError):
        return f"{host} answered {exc.status}: {exc.text[:200]}"
    return f"could not reach {host}: {type(exc).__name__}"


def _session_report(session: PortalSession, email: str) -> Dict[str, Any]:
    left = session.seconds_left()
    return {"user": session.username or email,
            "expires_at": session.expires_at.isoformat() if session.expires_at else None,
            "seconds_left": left}


def _snapshot_filename(label: str) -> str:
    """The CLI's names, so `smoke_test.py --restore` works on them too."""
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    kind, _, ident = label.partition("-")
    if kind == "capabilities":
        return f"{ident}-{stamp}-before.json"
    return f"{label}-{stamp}-before.json"


def _configure_result_payload(result) -> Dict[str, Any]:
    integ = result.integration
    caps = result.capabilities
    info = result.account_info
    return {
        "summary": result.summary(),
        "authority": {"id": result.authority_id, "name": result.authority_name,
                      "organization_id": result.organization_id},
        "jurisdiction": ({"id": result.jurisdiction.id,
                          "status": result.jurisdiction.ingress_label}
                         if result.jurisdiction and result.jurisdiction.id else None),
        "revision": result.revision.revision_number if result.revision else None,
        "integration": ({"id": integ.id, "app_name": integ.app_name,
                         "created": integ.created, "consumer_key": integ.consumer_key,
                         "consumer_secret": integ.consumer_secret}
                        if integ and integ.id else None),
        "capabilities": ({"changed": len(caps.changed), "applied": caps.applied,
                          "alerts_skipped": [str(k) for k in caps.alerts_skipped],
                          "unknown": [str(k) for k in caps.missing_from_target]}
                         if caps else None),
        "roles": result.roles.summary() if result.roles else None,
        "account_info": ({"changed": {k: list(v) for k, v in info.changed.items()},
                          "skipped": dict(info.skipped)} if info else None),
        "notes": list(result.notes),
        "existing": result.existing(),
    }


def _capability_counts(body: Any) -> Dict[str, int]:
    entries = (body.get("capabilities") or []) if isinstance(body, dict) else []
    return {"total": len(entries),
            "enabled": sum(1 for e in entries if e.get("authority_enabled")),
            "rsos_enabled": sum(1 for e in entries if e.get("rsos_enabled"))}


def _capability_plan(capture: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """What applying a capability set would do, for Preview. No I/O.

    The new integration does not exist yet, so its live catalog cannot be
    read. The packaged standard set is a captured sandbox catalog of the
    default product, and has the shape `plan()` takes as `live`, so the copied
    set is planned against that: `changed` is how it differs from the standard
    set, `missing_from_target` is what the sandbox does not have.

    Raises CapabilityError when the captured body is not a usable set.
    """
    reference = json.loads(STANDARD_PATH.read_text(encoding="utf-8"))
    if capture is None:
        return {"source": "default", "total": len(reference["capabilities"]),
                "needs_ack": False, "warning": None}

    captured = load_standard(capture["body"])
    _, report = plan_capabilities(reference, captured)
    unknown = [str(k) for k in report.missing_from_target]
    share = len(unknown) / len(captured) if captured else 0.0
    product = capture["integration"].get("product") or ""
    product_mismatch = bool(product) and product != DEFAULT_PRODUCT
    needs_ack = share >= UNKNOWN_CAPABILITY_WARNING

    why = ("Capability names are specific to an environment and a product -- "
           "production has lyft where the sandbox has lyft_sandbox -- and a name the "
           "new integration does not have cannot be set, so it is left out.")
    if product_mismatch:
        why += (f" The source integration is a {product!r} integration; the new one "
                f"will be {DEFAULT_PRODUCT!r}, which has a different catalog.")
    warning = None
    if needs_ack:
        warning = (f"{len(unknown)} of the {len(captured)} capabilities in the source "
                   f"({share:.0%}) are not in the sandbox catalog and will be skipped, "
                   f"so the copy will only be partial. {why}")
    elif unknown or product_mismatch:
        warning = (f"{len(unknown)} of the {len(captured)} capabilities in the source "
                   f"are not in the sandbox catalog and will be skipped. {why}")
    return {
        "source": "copy",
        "from": {"authority": capture["authority"], "integration": capture["integration"],
                 "read_at": capture["read_at"]},
        "total": len(captured),
        "changed": [str(c) for c in report.changed],
        "unknown": unknown,
        "unknown_share": share,
        "left_alone": len(report.missing_from_standard),
        "product_mismatch": product_mismatch,
        "needs_ack": needs_ack,
        "warning": warning,
    }


def _measured(polygon: Dict[str, Any], summary: Dict[str, Any]) -> Dict[str, Any]:
    """Feature count, bbox and payload size -- the same for every source.

    `bytes` is the FeatureCollection as it will be posted, not the size of
    whatever it was read from.
    """
    return {**summary, "features": len(polygon["features"]), "bbox": bbox(polygon),
            "bytes": len(json.dumps(polygon))}


def _jurisdiction_choice(j) -> Dict[str, Any]:
    return {"id": j.id, "status": j.ingress_label, "shapes": len(j.shapes)}


def _step_labels(keys: List[str]) -> List[str]:
    labels = dict(CONFIGURE_STEPS)
    return [labels.get(k, k) for k in keys]


# --------------------------------------------------------------------- app


def create_app(
    *,
    make_client: Optional[ClientFactory] = None,
    places: Optional[PlaceLookup] = None,
    snapshot_dir: Optional[Path] = None,
    sleep: Callable[[float], None] = time.sleep,
    warm_places: bool = True,
    session_cache: Optional[TokenCache] = None,
    sign_in: Optional[Callable[..., Any]] = None,
    env: Optional[Mapping[str, str]] = None,
    can_open_browser: Callable[[], bool] = playwright_installed,
) -> FastAPI:
    """Build the app. Tests pass fakes for the clients, places and sign-in."""
    places = places or PlaceLookup()
    env = os.environ if env is None else env

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if warm_places:
            places.warm()        # Census frames, in the background
        yield

    app = FastAPI(title="Sandbox account", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    # A page on another site must not be able to drive this one via DNS
    # rebinding; only loopback names are served.
    app.add_middleware(TrustedHostMiddleware,
                       allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def this_page_only(request: Request, call_next):
        # the server holds the Andromeda session, so a write must come from
        # this page -- not from a form or fetch on some other site
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            same = origin is None or origin == f"http://{request.headers.get('host')}"
            if not same or request.headers.get(PAGE_HEADER) != "1":
                return _error(403, error="Refused: this request did not come from the "
                                         "sandbox account page.")
        return await call_next(request)

    make_client = make_client or (lambda base, token=None, org=None:
                                  HttpClient(base, token=token, org=org))
    snapshot_dir = snapshot_dir or PROJECT_ROOT / "snapshots"
    runs = Runs()
    boundaries: Dict[str, Dict[str, Any]] = {}
    captures: Dict[str, Dict[str, Any]] = {}      # capability sets read from other accounts
    workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="run")

    andromeda_auth = AndromedaAuth(
        cache=session_cache or TokenCache(SESSION_FILE),
        scorpius=lambda: make_client(SCORPIUS_BASE),
        sign_in=sign_in or browser_auth.sign_in,
        profile_dir=BROWSER_PROFILE,
        env_token=env.get("ANDROMEDA_TOKEN"),
        can_open_browser=can_open_browser,
    )

    app.state.runs = runs
    app.state.boundaries = boundaries
    app.state.captures = captures
    app.state.andromeda_auth = andromeda_auth

    def andromeda_client(token: str):
        return make_client(ANDROMEDA_BASE, token=token, org=ORG_HEADER)

    def portal_client(token: Optional[str] = None):
        # sign-up and confirm are unauthenticated and sent without the org header
        if token is None:
            return make_client(PORTAL_BASE)
        return make_client(PORTAL_BASE, token=token, org=ORG_HEADER)

    def portal_login(email: Optional[str]) -> PortalSession:
        """Log in as the account, for its roles. Raises LoginFailed."""
        if not email:
            raise LoginFailed("No email address to log in to the portal with. Enter "
                              "the account's email address.")
        try:
            return log_in(portal_client(), email)
        except SignupError as exc:
            raise LoginFailed(f"Logging in to the portal as {email} gave an unusable "
                              f"answer: {exc}") from None
        except ApiError as exc:
            raise LoginFailed(
                f"The portal refused to log in as {email} ({exc.status}). Accounts "
                f"this app creates use the shared sandbox password, so check the "
                f"address; an account made some other way may have a different "
                f"password, and its roles have to be set by hand.") from None
        except Exception as exc:
            raise LoginFailed(f"Could not reach the portal to log in: "
                              f"{type(exc).__name__}") from None

    def andromeda_token(why: Optional[str] = None):
        """(token, None), or (None, the response saying how to sign in)."""
        try:
            return andromeda_auth.token(), None
        except NoSession as exc:
            message = f"{why} {exc}" if why else str(exc)
            return None, _error(400, kind="session", state=exc.state, error=message)

    def resolve_authority(client, wanted: str):
        """The authority record, or the error response to send instead."""
        wanted = wanted.strip()
        try:
            if wanted.isdigit():
                return get_authority(client, wanted)
            return find_authority(client, wanted)
        except AuthorityNotFoundError as exc:
            return _error(404, kind="not_found", message=str(exc), near=exc.near)
        except AmbiguousAuthorityError as exc:
            return _error(409, kind="ambiguous", message=str(exc), candidates=[
                {"id": m.get("id"), "name": m.get("name"), "account_id": m.get("account_id"),
                 "organization_id": m.get("organization_id")} for m in exc.matches])
        except Exception as exc:
            return _error(502, error=_describe_api_error(exc, "Andromeda"))

    def capability_choice(source: Optional[str], capture_id: Optional[str]):
        """(capture, plan) for the chosen set, or (None, error message)."""
        capture = None
        if source == "copy" or capture_id:
            capture = captures.get(capture_id or "")
            if capture is None:
                return None, ("find the account to copy from and choose one of its "
                              "integrations")
        try:
            return capture, _capability_plan(capture)
        except CapabilityError as exc:
            return None, f"the chosen capability set cannot be used: {exc}"

    def get_run(run_id: str) -> Run:
        run = runs.get(run_id)
        if run is None:
            raise _NotFound(run_id)
        return run

    @app.exception_handler(_NotFound)
    async def _no_such_run(request: Request, exc: _NotFound):
        return _error(404, error=f"no run {exc.args[0]}; the app may have been "
                                 f"restarted -- use Resume an existing account")

    # ------------------------------------------------------------- page

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html",
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/status")
    def status():
        return {"active_run": runs.active, "place_lookup": places.state,
                "place_problem": places.problem,
                "steps": [{"key": k, "label": v} for k, v in CONFIGURE_STEPS]}

    # ---------------------------------------------------------- sign-in

    @app.get("/api/session")
    def session_status():
        report = andromeda_auth.status()
        report["live"], report["detail"] = "skipped", None
        if report["state"] != "signed_in":
            return report
        # a cheap read, so a session the server has stopped honouring shows now
        try:
            list_countries(andromeda_client(andromeda_auth.token()))
            report["live"] = "ok"
        except NoSession as exc:
            report["state"], report["problem"] = exc.state, str(exc)
        except Exception as exc:
            report["live"] = "failed"
            report["detail"] = _describe_api_error(exc, "Andromeda")
            if isinstance(exc, AuthError):
                report["state"] = "expired"
        return report

    @app.post("/api/session/sign-in", status_code=202)
    def session_sign_in():
        try:
            job = andromeda_auth.start_sign_in()
        except SignInRefused as exc:
            return _error(409, kind=exc.kind, error=str(exc))
        except SignInBusy as exc:
            return _error(409, kind="busy", job_id=exc.job_id,
                          error="A sign-in is already running. Finish it in the "
                                "browser window it opened.")
        return {"job_id": job.id, "status": job.status}

    @app.get("/api/session/sign-in/{job_id}/events")
    async def session_sign_in_events(job_id: str, request: Request, after: int = -1):
        job = andromeda_auth.job(job_id)
        if job is None:
            return _error(404, error="no such sign-in; press Sign in again")
        return _event_stream(job, request, after, SIGN_IN_QUIET)

    @app.post("/api/session/har")
    async def session_har(file: UploadFile = File(...)):
        try:
            raw = await file.read(MAX_HAR_BYTES + 1)
        finally:
            await file.close()
        if len(raw) > MAX_HAR_BYTES:
            return _error(413, error="the file is larger than 200 MB; is it a sign-in capture?")
        try:
            await asyncio.get_running_loop().run_in_executor(None, andromeda_auth.from_har, raw)
        except SignInRefused as exc:
            return _error(409, kind=exc.kind, error=str(exc))
        except HarRejected as exc:
            return _error(422, error=str(exc))
        finally:
            del raw
        return session_status()

    @app.post("/api/session/sign-out")
    def session_sign_out():
        andromeda_auth.sign_out()
        return session_status()

    # ---------------------------------------------------------- places

    @app.get("/api/place")
    def place(q: str, allow_unverified: bool = False):
        try:
            resolved = places.resolve(q, allow_unverified=allow_unverified)
        except AmbiguousPlaceError as exc:
            return _error(409, kind="ambiguous", message=str(exc),
                          candidates=candidate_choices(exc.candidates))
        except PlaceNotFoundError as exc:
            return _error(404, kind="not_found", message=str(exc), near=exc.near)
        except UnusableBoundaryError as exc:
            return _error(422, kind="unverified", message=str(exc),
                          status=exc.status, geoid=exc.geoid)
        except PlaceError as exc:
            return _error(400, kind="error", message=str(exc))

        summary = place_summary(resolved)
        polygon = summary.pop("polygon")
        return store_boundary(polygon, summary)

    def store_boundary(polygon: Dict[str, Any], summary: Dict[str, Any]) -> Dict[str, Any]:
        summary = _measured(polygon, summary)
        boundary_id = uuid.uuid4().hex[:12]
        boundaries[boundary_id] = {"polygon": polygon, "summary": summary}
        return {"boundary_id": boundary_id, **summary}

    @app.post("/api/boundary")
    async def upload(file: UploadFile = File(...)):
        raw = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            return _error(413, error="the file is larger than 15 MB")
        try:
            polygon = normalize_geojson(json.loads(raw.decode("utf-8-sig")))
            validate_geojson(polygon)
        except (ValueError, UnicodeDecodeError) as exc:
            return _error(422, error=f"not valid GeoJSON: {exc}")
        except InvalidGeometryError as exc:
            return _error(422, error=str(exc))
        return store_boundary(polygon, {"source": "file", "label": file.filename})

    # --------------------------------------------------- boundary sources
    #
    # Only an Active jurisdiction is copied. A Verified or Pending one may
    # still change or be withdrawn, so it is refused, never offered.

    def copy_boundary(client, record: Dict[str, Any], jurisdiction) -> Dict[str, Any]:
        """Export one Active jurisdiction into the boundary store, or the error."""
        aid = str(record.get("id"))
        try:
            polygon = export_boundary(client, aid, jurisdiction.id)
        except InvalidGeometryError as exc:
            return _error(422, kind="unusable", message=(
                f"The boundary stored on {record.get('name')} (jurisdiction "
                f"{jurisdiction.id}) is unusable, so it cannot be copied: {exc}"))
        except JurisdictionError as exc:
            return _error(422, kind="error", message=str(exc))
        except Exception as exc:
            return _error(502, error=_describe_api_error(exc, "Andromeda"))
        attributes = record.get("attributes") or {}
        fields = {k: attributes.get(k) for k in ("country", "state") if attributes.get(k)}
        return store_boundary(polygon, {
            "source": "account",
            "label": (f"{record.get('name')} (id {aid}), jurisdiction {jurisdiction.id} "
                      f"({jurisdiction.ingress_label})"),
            "from": {"authority": {"id": aid, "name": record.get("name")},
                     "jurisdiction": _jurisdiction_choice(jurisdiction)},
            "fields": fields,
        })

    def read_jurisdictions(authority: str):
        """(client, record, jurisdictions), or the error response to send."""
        token, refused = andromeda_token("Reading another account's boundary needs "
                                         "Andromeda.")
        if refused:
            return refused
        client = andromeda_client(token)
        record = resolve_authority(client, authority)
        if isinstance(record, JSONResponse):
            return record
        try:
            listed = list_jurisdictions(client, str(record.get("id")))
        except Exception as exc:
            return _error(502, error=_describe_api_error(exc, "Andromeda"))
        return client, record, listed

    @app.get("/api/boundary-sources")
    def boundary_sources(authority: str):
        found = read_jurisdictions(authority)
        if isinstance(found, JSONResponse):
            return found
        client, record, listed = found
        who = f"{record.get('name')} (id {record.get('id')})"
        source = {"id": str(record.get("id")), "name": record.get("name")}
        if not listed:
            return _error(404, kind="no_jurisdiction", authority=source, message=(
                f"{who} has no jurisdiction, so there is no boundary to copy."))

        active = [j for j in listed if j.ingress_status == IngressStatus.ACTIVE]
        not_offered = [_jurisdiction_choice(j) for j in listed if j not in active]
        if not active:
            states = ", ".join(f"{j['id']} ({j['status']})" for j in not_offered)
            return _error(409, kind="not_active", authority=source, not_offered=not_offered,
                          message=(f"{who} has no Active jurisdiction ({states}). Only an "
                                   f"Active boundary is copied: one that is not yet "
                                   f"published may still change or be withdrawn."))
        if len(active) > 1:        # a choice for the user, never made for them
            return {"authority": source, "jurisdictions": [_jurisdiction_choice(j) for j in active],
                    "not_offered": not_offered}
        copied = copy_boundary(client, record, active[0])
        if isinstance(copied, JSONResponse):
            return copied
        return {"authority": source, "boundary": copied, "not_offered": not_offered}

    @app.post("/api/boundary/from-authority")
    def boundary_from_authority(body: BoundaryFromIn):
        found = read_jurisdictions(body.authority_id)
        if isinstance(found, JSONResponse):
            return found
        client, record, listed = found
        chosen = next((j for j in listed if j.id == str(body.jurisdiction_id)), None)
        if chosen is None:
            return _error(404, kind="error", message=(
                f"{record.get('name')} has no jurisdiction {body.jurisdiction_id}."))
        if chosen.ingress_status != IngressStatus.ACTIVE:
            return _error(409, kind="not_active", message=(
                f"Jurisdiction {chosen.id} is {chosen.ingress_label}, not Active. Only an "
                f"Active boundary is copied."))
        copied = copy_boundary(client, record, chosen)
        if isinstance(copied, JSONResponse):
            return copied
        return {"authority": {"id": str(record.get("id")), "name": record.get("name")},
                "boundary": copied}

    @app.get("/api/catalogs/countries")
    def countries():
        token, refused = andromeda_token()
        if refused:
            return refused
        try:
            return list_countries(andromeda_client(token))
        except Exception as exc:
            return _error(502, error=_describe_api_error(exc, "Andromeda"))

    @app.get("/api/catalogs/countries/{code}/states")
    def states(code: str):
        token, refused = andromeda_token()
        if refused:
            return refused
        try:
            return list_states(andromeda_client(token), code)
        except Exception as exc:
            return _error(502, error=_describe_api_error(exc, "Andromeda"))

    # ------------------------------------------------ capability sources

    @app.get("/api/capability-sources")
    def capability_sources(authority: str):
        token, refused = andromeda_token("Reading another account's capabilities needs "
                                         "Andromeda.")
        if refused:
            return refused
        client = andromeda_client(token)
        record = resolve_authority(client, authority)
        if isinstance(record, JSONResponse):
            return record
        aid = str(record.get("id"))
        read_at = dt.datetime.now().isoformat(timespec="seconds")
        source = {"id": aid, "name": record.get("name")}
        out = []
        try:
            for integ in list_integrations(client, aid):
                body = client.get(f"/v1/andromeda/authorities/{aid}"
                                  f"/integrations/{integ.id}/capabilities")
                capture_id = uuid.uuid4().hex[:12]
                about = {"id": integ.id, "app_name": integ.app_name, "product": integ.product}
                captures[capture_id] = {"body": body, "authority": source,
                                        "integration": about, "read_at": read_at}
                out.append({"capture_id": capture_id, **about, **_capability_counts(body)})
        except Exception as exc:
            return _error(502, error=_describe_api_error(exc, "Andromeda"))
        return {"authority": {**source, "account_id": record.get("account_id")},
                "integrations": out, "read_at": read_at}

    # ---------------------------------------------------------- preview

    @app.post("/api/runs")
    def preview(body: FormIn):
        form = {k: _clean(v) for k, v in body.model_dump().items()}
        errors: Dict[str, str] = {}
        if not form["email"] or not _EMAIL.match(form["email"]):
            errors["email"] = "enter an email address"
        for name, label in (("first_name", "first name"), ("last_name", "last name"),
                            ("agency_name", "agency name")):
            if not form[name]:
                errors[name] = f"enter the {label}"
        boundary = boundaries.get(form["boundary_id"] or "")
        if boundary is None:
            errors["boundary"] = "look up a place or upload a .geojson file"
        if not form["country"]:
            errors["country"] = "enter the country code, e.g. USA"
        if not form["state"]:
            errors["state"] = "enter the state code, e.g. NE"
        capture, capabilities = capability_choice(form["capabilities_source"],
                                                  form["capabilities_capture_id"])
        if isinstance(capabilities, str):
            errors["capabilities"] = capabilities
        if errors:
            return _error(422, errors=errors)

        checks = _preview_checks(form, andromeda_auth.token_or_none())
        blocked = any(c["status"] == "blocked" for c in checks)
        plan = {
            "signup": {
                "email": form["email"],
                "agency_name": form["agency_name"],
                "contact": f"{form['first_name']} {form['last_name']}",
                "password": default_password(),
                "fixed": {k: DEFAULTS[k] for k in ("contact_title", "contact_phone",
                                                   "population")},
            },
            "boundary": boundary["summary"],
            "account": {"account_id": form["account_id"], "country": form["country"],
                        "state": form["state"]},
            "capabilities": capabilities,
            "steps": [{"key": k, "label": v} for k, v in CONFIGURE_STEPS],
        }
        run = runs.new("new", "previewed", form=form, boundary_id=form["boundary_id"],
                       boundary=boundary["summary"], plan=plan, blocked=blocked,
                       authority=form["agency_name"],
                       capture_id=form["capabilities_capture_id"] if capture else None,
                       capabilities=capabilities)
        return {"run_id": run.id, "plan": plan, "checks": checks, "can_create": not blocked}

    def _preview_checks(form: Dict[str, Any], token: Optional[str]) -> List[Dict[str, str]]:
        if not token:
            return [{"id": "andromeda", "status": "skipped",
                     "message": "Sign in to Andromeda to check that the agency name is "
                                "free and that nobody else's work is waiting to be "
                                "published. Creating the account does not need it."}]
        client = andromeda_client(token)
        out: List[Dict[str, str]] = []
        try:
            checks = check_new_account(client, form["agency_name"])
        except Exception as exc:
            return [{"id": "andromeda", "status": "warn",
                     "message": f"Checks skipped: {_describe_api_error(exc, 'Andromeda')}"}]

        if checks.name_is_free:
            out.append({"id": "name", "status": "ok",
                        "message": f"No authority is called {form['agency_name']!r} yet."})
        else:
            ids = ", ".join(str(m.get("id")) for m in checks.name_taken_by)
            out.append({"id": "name", "status": "blocked",
                        "message": f"An authority called {form['agency_name']!r} already "
                                   f"exists (id {ids}). Choose a different agency name."})
        if checks.others_pending:
            names = ", ".join(f"{n} ({i})" for i, n in checks.others_pending.items())
            out.append({"id": "batch", "status": "warn",
                        "message": f"The environment-wide revision is holding changes for "
                                   f"{names}. Publishing would activate their work too, so "
                                   f"the boundary step will refuse until it is cleared. Ask "
                                   f"the team before you continue."})
        else:
            out.append({"id": "batch", "status": "ok",
                        "message": "Nobody else's changes are waiting to be published."})

        try:
            known = list_countries(client)
            if known and form["country"] not in known:
                out.append({"id": "country", "status": "blocked",
                            "message": f"{form['country']!r} is not a country code "
                                       f"Andromeda knows."})
            elif form["state"]:
                regions = list_states(client, form["country"])
                if regions and form["state"] not in regions:
                    out.append({"id": "state", "status": "blocked",
                                "message": f"{form['state']!r} is not a state or region "
                                           f"of {form['country']}."})
        except Exception as exc:
            out.append({"id": "codes", "status": "warn",
                        "message": f"Could not check the country and state codes: "
                                   f"{_describe_api_error(exc, 'Andromeda')}"})
        return out

    # ---------------------------------------------------------- phase 1

    @app.post("/api/runs/{run_id}/signup", status_code=202)
    def signup(run_id: str, body: Optional[SignupIn] = None):
        run = get_run(run_id)
        if run.kind != "new" or run.status != "previewed":
            return _error(409, error=f"this run is {run.status}; preview again to create "
                                     f"another account")
        if run.blocked:
            return _error(409, error="the preview found a problem that must be fixed first")
        if (run.capabilities or {}).get("needs_ack"):
            if not (body and body.acknowledge_capabilities):
                return _error(409, kind="capabilities", error=run.capabilities["warning"])
            run.capabilities_ack = True
        try:
            runs.start(run)
        except RunBusyError as exc:
            return _error(409, error=str(exc), active_run=exc.active)
        run.set_status("signing_up")
        workers.submit(_phase1, run)
        return {"status": run.status}

    def _phase1(run: Run) -> None:
        form = run.form
        try:
            with capture_logs(run):
                run.emit("step", step="signup", status="running",
                         detail=f"registering {form['email']}")
                result = sign_up(
                    portal_client(),
                    email=form["email"], agency_name=form["agency_name"],
                    first_name=form["first_name"], last_name=form["last_name"],
                )
            run.signup = {"user_id": result.user_id,
                          "organization_id": result.organization_id,
                          "psap_status": result.psap_status}
            run.organization_id = result.organization_id
            if result.psap_status != "ok":
                run.emit("note", step="signup",
                         message="The agency-details call answered 500. That is expected "
                                 "-- the data lands anyway, and the next phase checks.")
            run.emit("step", step="signup", status="done", detail=result.summary())
            run.set_status("awaiting_email")
        except Exception as exc:
            run.error = _signup_failure(exc)
            run.emit("failed", **run.error)
            run.set_status("signup_failed")
        finally:
            runs.finish()

    def _signup_failure(exc: Exception) -> Dict[str, Any]:
        failure = {"step": "signup", "refused": False, "message": str(exc),
                   "exists": [], "not_done": ["everything"], "advice": ""}
        if isinstance(exc, ApiError) and exc.path == REGISTER_PATH and 400 <= exc.status < 500:
            failure["refused"] = True
            failure["advice"] = ("Registration was refused. Addresses cannot be reused: "
                                 "if this one was used before, add a different +tag "
                                 "(you+lancaster2@...) and Preview again.")
        elif isinstance(exc, ApiError) and exc.path == PSAP_PATH:
            failure["exists"] = ["the user and organization"]
            failure["not_done"] = ["the agency details"]
            failure["advice"] = ("The account was registered but its agency details were "
                                 "rejected. Do not sign up again with this address; ask "
                                 "the team to finish it by hand in the portal.")
        elif isinstance(exc, SignupError):
            failure["advice"] = "Check the form and Preview again."
        else:
            failure["advice"] = ("Nothing may have been created, or the user may exist. "
                                 "Try signing in to the portal with this address before "
                                 "retrying; if it works, use Resume instead.")
        return failure

    # -------------------------------------------------------------- gate

    @app.post("/api/runs/{run_id}/confirm")
    def confirm(run_id: str, body: ConfirmIn):
        run = get_run(run_id)
        try:
            confirm_email(portal_client(), body.link)
        except SignupError as exc:
            return _error(400, error=str(exc))
        except ApiError as exc:
            return _error(400, error=f"The confirmation was refused ({exc.status}). The "
                                     f"link may already have been used, or was copied "
                                     f"incompletely.")
        run.email_confirmed = "link"
        run.emit("note", step="signup", message="Email address confirmed.")
        return {"confirmed": True}

    # ---------------------------------------------------------- phase 2

    @app.post("/api/runs/{run_id}/continue", status_code=202)
    def continue_(run_id: str, body: ContinueIn):
        run = get_run(run_id)
        if run.status not in ("awaiting_email", "awaiting_continue", "failed"):
            return _error(409, error=f"this run is {run.status}")
        if run.kind == "new" and not (run.email_confirmed or body.clicked_link):
            return _error(409, kind="gate",
                          error=f"Confirm the email sent to {run.form.get('email')} first: "
                                f"click its link, or paste the link here.")
        if body.clicked_link and not run.email_confirmed:
            run.email_confirmed = "clicked"

        if run.kind == "resume" and _clean(body.email):
            run.form["email"] = _clean(body.email)
        if (run.capabilities or {}).get("needs_ack") and not run.capabilities_ack:
            if not body.acknowledge_capabilities:
                return _error(409, kind="capabilities", error=run.capabilities["warning"])
            run.capabilities_ack = True

        token, refused = andromeda_token()
        if refused:
            return refused
        report = _token_report(token)

        # a cheap read, so a bad token fails now, not mid-run
        try:
            list_countries(andromeda_client(token))
        except Exception as exc:
            return _error(400, kind="session", state="failed",
                          error=_describe_api_error(exc, "Andromeda"))

        # the portal side: log in as the account rather than asking for a token
        email = run.form.get("email")
        try:
            session = portal_login(email)
        except LoginFailed as exc:
            return _error(400, kind="login", email=email, error=str(exc))
        if run.organization_id:
            try:
                list_roles(portal_client(session.token), run.organization_id)
            except Exception as exc:
                return _error(400, kind="login", email=email, error=(
                    f"Logged in as {email}, but that login cannot read the roles of "
                    f"organization {run.organization_id}: "
                    f"{_describe_api_error(exc, 'the portal')}. Is this the right "
                    f"account's address?"))

        expiring = {"andromeda": report["seconds_left"]} if report["expiring"] else {}
        if expiring and not body.acknowledge_expiry:
            return _error(409, kind="expiring", seconds_left=expiring)

        try:
            runs.start(run)
        except RunBusyError as exc:
            return _error(409, error=str(exc), active_run=exc.active)
        run.error = None
        run.emit("note", step="account",
                 message=f"Logged in to the portal as {session.username or email}, for "
                         f"the roles step. No portal token is needed.")
        run.set_status("configuring")
        workers.submit(_phase2, run, token, session.token)
        return {"status": run.status, "portal_login": _session_report(session, email)}

    def _phase2(run: Run, andromeda_token: str, portal_token: str) -> None:
        andromeda = andromeda_client(andromeda_token)
        portal = portal_client(portal_token)
        del andromeda_token, portal_token

        boundary = boundaries.get(run.boundary_id or "")
        capture = captures.get(run.capture_id) if run.capture_id else None

        def on_step(outcome) -> None:
            if outcome.status == "note":
                run.emit("note", step=outcome.step, message=outcome.detail)
            else:
                run.emit("step", step=outcome.step, status=outcome.status,
                         detail=outcome.detail)

        def save_snapshot(label: str, body) -> str:
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            path = snapshot_dir / _snapshot_filename(label)
            path.write_text(json.dumps(body, indent=2), encoding="utf-8")
            run.snapshots[label.partition("-")[0]] = {"label": label, "path": str(path)}
            run.emit("note", step=label.partition("-")[0],
                     message=f"Snapshot saved before writing: {path.name}")
            return str(path)

        try:
            standard = load_standard(capture["body"]) if capture else None
            if capture:
                run.emit("note", step="capabilities", message=(
                    f"Capabilities are copied from {capture['authority']['name']} "
                    f"(id {capture['authority']['id']}), integration "
                    f"{capture['integration']['app_name']!r}, read at "
                    f"{capture['read_at']}."))
            with capture_logs(run):
                result = configure_account(
                    andromeda, portal, run.authority,
                    organization_id=run.organization_id,
                    polygon=boundary["polygon"] if boundary else None,
                    account_id=run.form.get("account_id"),
                    country=run.form.get("country"),
                    state=run.form.get("state"),
                    standard=standard,
                    save_snapshot=save_snapshot,
                    on_step=on_step,
                    sleep=sleep,
                )
            run.authority_id = result.authority_id
            run.organization_id = result.organization_id
            run.result = _configure_result_payload(result)
            unknown = run.result["capabilities"] and run.result["capabilities"]["unknown"]
            if capture and unknown:
                message = (f"{len(unknown)} copied capabilities do not exist on this "
                           f"integration and were left alone: {', '.join(unknown)}")
                run.result["notes"].append(message)
                run.emit("note", step="capabilities", message=message)
            run.emit("done", **run.result)
            run.set_status("done")
        except PartialConfigureError as err:
            run.authority_id = err.result.authority_id or run.authority_id
            run.organization_id = err.result.organization_id or run.organization_id
            run.error = _configure_failure(err, andromeda)
            run.emit("failed", **run.error)
            run.set_status("failed")
        except Exception as exc:  # a bug, not a refusal -- say so plainly
            log.exception("phase 2 failed unexpectedly")
            run.error = {"step": None, "refused": False, "message": str(exc),
                         "exists": [], "not_done": [],
                         "advice": "Unexpected error. Press Resume to retry; completed "
                                   "steps are detected and skipped."}
            run.emit("failed", **run.error)
            run.set_status("failed")
        finally:
            runs.finish()

    def _configure_failure(err: PartialConfigureError, andromeda) -> Dict[str, Any]:
        cause = root_cause(err)
        failure: Dict[str, Any] = {
            "step": err.failed_step,
            "refused": err.refused,
            "message": str(cause),
            "exists": err.result.existing(),
            "not_done": _step_labels(err.result.not_done),
            "advice": next_action(err),
            "partial": {"integration": (_configure_result_payload(err.result)
                                        .get("integration"))},
        }
        if isinstance(cause, AuthError):
            failure["advice"] = ("A token was rejected -- it has probably expired. "
                                 "If it was Andromeda's, sign in again at the top of "
                                 "the page; the portal login is renewed automatically. "
                                 "Then press Resume.")
        if isinstance(cause, OtherAuthoritiesPendingError):
            names = authority_names(andromeda, cause.others)
            failure["others"] = names
            failure["message"] = (
                "Refused: publishing the revision would also publish pending work for "
                + ", ".join(f"{n} (id {i})" for i, n in names.items()) + ".")
        if isinstance(cause, AmbiguousAuthorityError):
            failure["candidates"] = [
                {"id": m.get("id"), "name": m.get("name"),
                 "account_id": m.get("account_id"),
                 "organization_id": m.get("organization_id")} for m in cause.matches]
        return failure

    # ----------------------------------------------------------- resume

    @app.post("/api/resume")
    def resume(body: ResumeIn):
        token, refused = andromeda_token()
        if refused:
            return refused
        capture, capabilities = capability_choice(None, _clean(body.capabilities_capture_id))
        if isinstance(capabilities, str):
            return _error(422, kind="capabilities", error=capabilities)
        record = resolve_authority(andromeda_client(token), body.authority)
        if isinstance(record, JSONResponse):
            return record

        boundary = boundaries.get(body.boundary_id or "")
        email = _clean(body.email) or _contact_email(record)
        form = {"account_id": _clean(body.account_id), "country": _clean(body.country),
                "state": _clean(body.state), "email": email}
        run = runs.new("resume", "awaiting_continue", form=form,
                       boundary_id=body.boundary_id if boundary else None,
                       boundary=boundary["summary"] if boundary else None,
                       authority=str(record.get("id")),
                       authority_id=str(record.get("id")),
                       organization_id=str(record.get("organization_id") or "") or None,
                       capture_id=_clean(body.capabilities_capture_id) if capture else None,
                       capabilities=capabilities)
        run.plan = {"steps": [{"key": k, "label": v} for k, v in CONFIGURE_STEPS]}
        return {"run_id": run.id, "email": email, "capabilities": capabilities, "authority": {
            "id": record.get("id"), "name": record.get("name"),
            "account_id": record.get("account_id"),
            "organization_id": record.get("organization_id")}}

    # ------------------------------------------------------------- undo

    @app.post("/api/runs/{run_id}/restore")
    def restore(run_id: str, body: RestoreIn):
        run = get_run(run_id)
        saved = run.snapshots.get(body.kind)
        if saved is None:
            return _error(404, error=f"no {body.kind} snapshot was taken in this run")
        snapshot = json.loads(Path(saved["path"]).read_text(encoding="utf-8"))
        if body.kind == "capabilities":
            token, refused = andromeda_token("Undoing capabilities needs Andromeda.")
            if refused:
                return refused
        else:
            try:
                token = portal_login(run.form.get("email")).token
            except LoginFailed as exc:
                return _error(400, kind="login", error=str(exc))

        if body.apply:
            try:
                runs.start(run)
            except RunBusyError as exc:
                return _error(409, error=str(exc), active_run=exc.active)
        try:
            if body.kind == "capabilities":
                integration_id = saved["label"].partition("-")[2]
                report = restore_capabilities(andromeda_client(token), run.authority_id,
                                              integration_id, snapshot,
                                              dry_run=not body.apply)
                changes = [str(c) for c in report.changed]
                applied = report.applied
            else:
                portal = portal_client(token)
                if body.apply:
                    report = restore_roles(portal, snapshot,
                                           organization_id=run.organization_id)
                else:
                    report = plan_roles_restore(portal, snapshot, run.organization_id)
                changes = ([f"{r}: +{', '.join(n)}" for r, n in sorted(report.granted.items())]
                           + [f"{r}: -{', '.join(n)}" for r, n in sorted(report.revoked.items())])
                applied = report.applied
        except Exception as exc:
            return _error(502, error=str(exc))
        finally:
            if body.apply:
                runs.finish()
        if body.apply:
            run.emit("note", step=body.kind,
                     message=f"Undone from {Path(saved['path']).name}: "
                             f"{len(changes)} change(s).")
        return {"kind": body.kind, "applied": applied, "changes": changes,
                "snapshot": Path(saved["path"]).name}

    # --------------------------------------------------------- progress

    @app.get("/api/runs/{run_id}")
    def run_state(run_id: str):
        return {**get_run(run_id).public(), "active_run": runs.active}

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, request: Request, after: int = -1):
        return _event_stream(get_run(run_id), request, after, QUIET)

    return app


class _NotFound(Exception):
    pass


def _event_stream(run: Run, request: Request, after: int, quiet) -> StreamingResponse:
    """A run's events as Server-Sent Events, from `after` or Last-Event-ID.

    Closes once the run is in a `quiet` status and everything is sent; the
    page reopens it when something starts again.
    """
    last = request.headers.get("last-event-id")
    position = int(last) if last and last.isdigit() else after

    async def stream():
        nonlocal position
        yield "retry: 2000\n\n"
        idle = 0.0
        while True:
            batch = run.events_after(position)
            for event in batch:
                position = event["id"]
                yield (f"id: {event['id']}\nevent: {event['type']}\n"
                       f"data: {json.dumps(event)}\n\n")
            if not batch:
                if run.status in quiet:
                    return            # nothing running; the page reopens on demand
                if await request.is_disconnected():
                    return
                idle += 0.2
                if idle >= 15:
                    idle = 0.0
                    yield ": keep-alive\n\n"
            await asyncio.sleep(0.2)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store"})


app = create_app()
