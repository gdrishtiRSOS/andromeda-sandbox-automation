#!/usr/bin/env python3
"""
Everything about smoke_test.py can be found under README
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

try:
    import requests  # noqa: F401 -- checked here for the friendly message
except ImportError:
    sys.exit("needs requests:  pip install requests")

# the package lives in src/logic; make it importable from wherever this is run
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from logic.auth import (
    SCORPIUS_BASE,
    AuthError,
    TokenCache,
    refresh_token_from_har,
)
from logic.authorities import (
    DEFAULT_DISPATCH_TYPE,
    AccountInfoError,
    AmbiguousAuthorityError,
    AuthorityNotFoundError,
    get_authority,
    list_countries,
    list_states,
    resolve_authority_id,
    update_account_info,
)
from logic.client import ApiError, HttpClient
from logic.capabilities import (
    CapabilityWriteError,
    apply_standard_capabilities,
    load_standard,
    plan,
)
from logic.integrations import (
    DEFAULT_PRODUCT,
    ExistsPolicy,
    IntegrationError,
    create_integration,
)
from logic.jurisdictions import (
    JurisdictionError,
    available_boundaries,
    bbox,
    export_boundary,
    list_jurisdictions,
    load_geojson,
)
from logic.signup import (
    SignupError,
    confirm_email,
    default_password,
    log_in,
    sign_up,
)
from logic.roles import (
    RoleError,
    enable_all_data_sources,
    list_permissions,
    list_roles,
    restore_roles,
    snapshot_roles,
)
from logic.places import PlaceError
from logic.tokens import decode_claims
from logic.revisions import (
    RevisionError,
    activate_jurisdiction,
    default_revision_number,
    get_pending,
)
from logic.workflows import (
    NotInPendingBatchError,
    PartialActivationError,
    PartialProvisionError,
    attach_and_activate,
    create_sandbox_account,
    provision_authority,
)

DEFAULT_BASE = "https://andromeda.sandbox.rapidsos.com"
PORTAL_BASE = "https://api-sandbox.rapidsosportal.com"
DEFAULT_ORG = "RapidSOS Admin"


def exit_on_auth_error(exc: ApiError) -> None:
    """A rejected token ends the run with advice. Exiting, rather than raising,
    also keeps the modules' retry and fallback handlers from catching it."""
    sys.exit(
        f"{exc.status} on {exc.method} {exc.path}\n"
        f"Authorization header sent: {'yes' if exc.token_sent else 'NO -- none was set'}\n"
        f"Server said: {exc.text[:200]}\n"
        "Most likely the token expired. Re-copy the authorization header "
        "value from a fresh DevTools request."
    )


def cli_client(base_url: str, *, org: str | None = None, token: str | None = None,
               cookie: str | None = None, headers: list[str] = ()) -> HttpClient:
    """The shared client, exiting on a rejected token. `headers` are raw
    --header values, "Name: value"."""
    extra = {}
    for h in headers:
        name, _, value = h.partition(":")
        if not value:
            sys.exit(f"bad --header {h!r}, expected 'Name: value'")
        extra[name.strip()] = value.strip()
    return HttpClient(base_url, org=org, token=token, cookie=cookie, headers=extra,
                      on_auth_error=exit_on_auth_error)


def caps_path(authority_id: str, integration_id: str) -> str:
    return f"/v1/andromeda/authorities/{authority_id}/integrations/{integration_id}/capabilities"


def snapshot(obj, integration_id: str, label: str, out_dir: str) -> str:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = Path(out_dir) / f"{integration_id}-{stamp}-{label}.json"
    path.write_text(json.dumps(obj, indent=2))
    return str(path)


def show(report) -> None:
    print(f"\nlive catalog : {report.live_count}")
    print(f"standard set : {report.standard_count}")
    print(f"changes      : {len(report.changed)}")
    for change in report.changed[:40]:
        print(f"  {change}")
    if len(report.changed) > 40:
        print(f"  ... and {len(report.changed) - 40} more")

    if report.missing_from_standard:
        print(f"\nin Andromeda but not in the standard file ({len(report.missing_from_standard)}) "
              "-- left untouched:")
        for key in report.missing_from_standard:
            print(f"  {key}")
    if report.missing_from_target:
        print(f"\nin the standard file but not offered by this integration "
              f"({len(report.missing_from_target)}):")
        for key in report.missing_from_target:
            print(f"  {key}")


def inspect_token(token: str | None) -> None:
    """Print what the token says about itself. A JWT's payload is base64, not
    encrypted, so expiry and identity can be read without the signing key."""
    if not token:
        print("token     : NOT SET")
        return

    raw = token.strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()

    bad = [c for c in ('"', "^", " ", "'") if c in raw]
    if bad:
        print(f"token     : MALFORMED -- contains {bad!r}; you copied shell escaping too")
        return

    # A JWT is three base64url segments: A-Z a-z 0-9 - _ and optional = padding.
    allowed = set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=."
    )
    illegal = sorted({c for c in raw if c not in allowed})
    if illegal:
        print(f"token     : CORRUPT -- contains characters a JWT cannot hold: {illegal!r}")
        print("            The value was mangled in transit (shell redirection, line "
              "wrapping, or a truncated copy). Re-copy it.")
        return

    parts = raw.split(".")
    print(f"token     : {len(raw)} chars, {len(parts)} segments, ends {raw[-8:]!r}")
    if len(parts) != 3:
        print("            NOT A JWT -- expected 3 dot-separated segments; likely truncated")
        return

    try:
        claims = decode_claims(raw)
    except ValueError:
        print("            payload could not be decoded; token is corrupt")
        return

    if "username" in claims:
        print(f"            user: {claims['username']} (id {claims.get('sub')})")
    exp = claims.get("exp")
    if exp:
        expires = dt.datetime.fromtimestamp(exp)
        left = expires - dt.datetime.now()
        secs = left.total_seconds()
        if secs <= 0:
            print(f"            EXPIRED {-secs/60:.0f} min ago (at {expires:%H:%M:%S}) "
                  "-- get a fresh one")
        else:
            print(f"            valid for {secs/60:.0f} more min (until {expires:%H:%M:%S})")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--authority-id", default=None,
                   help="the numeric id, or the authority's name "
                        "(looked up automatically)")
    p.add_argument("--integration-id", help="omit with --stage scratch to create one")
    p.add_argument("--standard", default="src/logic/data/standard_capabilities.json")
    p.add_argument("--stage",
                   choices=list(STAGES),
                   default="read", help="'scratch' is an alias for 'create'")
    p.add_argument("--revision-number", type=int,
                   help="override the generated revision number (default: day + month)")
    p.add_argument("--revision-date", help="YYYY-MM-DD (default: today)")
    p.add_argument("--geojson",
                   help="boundary for --stage jurisdiction: a name in src/logic/data/ "
                        "(with or without .geojson) or a full path")
    p.add_argument("--organization-id",
                   help="portal org id for --stage roles; derived from the "
                        "authority when omitted")
    p.add_argument("--portal-base-url", default=PORTAL_BASE)
    p.add_argument("--restore-roles", help="a role snapshot JSON file to write back")
    p.add_argument("--sign-in", action="store_true",
                   help="sign in through a browser and store the session")
    p.add_argument("--browser-profile", default=".andromeda-browser",
                   help="where the browser keeps its Google session")
    p.add_argument("--show-browser", action="store_true",
                   help="always open a visible window, even on a repeat sign-in")
    p.add_argument("--from-har", help="a Google sign-in HAR to take the refresh token from")
    p.add_argument("--refresh-token", help="store this refresh token")
    p.add_argument("--forget", action="store_true", help="delete the stored session")
    p.add_argument("--session-file", default=".andromeda-session.json")
    p.add_argument("--portal-email",
                   help="log in as this account for --stage roles instead of "
                        "pasting RAPIDSOS_PORTAL_TOKEN")
    p.add_argument("--portal-password",
                   help="with --portal-email; defaults to the shared sandbox password")
    p.add_argument("--jurisdiction-id",
                   help="which jurisdiction to export, when an authority has several")
    p.add_argument("--out", help="where --stage export-boundary writes the file")
    p.add_argument("--email", help="sign-up address, e.g. you+lancaster@rapidsos.com")
    p.add_argument("--agency-name", help="becomes the authority name in Andromeda")
    p.add_argument("--first-name")
    p.add_argument("--last-name")
    p.add_argument("--password",
                   help="defaults to the shared sandbox password")
    p.add_argument("--confirm-token",
                   help="the confirmation token, or the whole emailed link")
    p.add_argument("--place", help='what to create, e.g. "Lincoln, Nebraska" or 48477')
    p.add_argument("--use-ecc-name", action="store_true",
                   help="name the authority after the registered ECC")
    p.add_argument("--allow-unverified-scope", action="store_true",
                   help="proceed even when the FCC registry lists no ECCs")
    p.add_argument("--account-id", help="free-text Account ID, e.g. GD_0209")
    p.add_argument("--country", help="country code, e.g. IRL or USA")
    p.add_argument("--state", help="state/region code, e.g. LK or TX")
    p.add_argument("--dispatch-type", type=int, default=DEFAULT_DISPATCH_TYPE,
                   help=f"defaults to {DEFAULT_DISPATCH_TYPE}, which every "
                        "sandbox account uses")
    p.add_argument("--keep-dispatch-type", action="store_true",
                   help="leave the existing dispatch type alone")
    p.add_argument("--skip-jurisdiction", action="store_true",
                   help="--stage provision: the boundary is already live")
    p.add_argument("--require-active", action="store_true",
                   help="--stage provision: fail if the jurisdiction never reaches Active")
    p.add_argument("--no-activate", action="store_true",
                   help="--stage jurisdiction: stop after creating it, do not publish")
    p.add_argument("--allow-other-authorities", action="store_true",
                   help="publish even when the batch holds other authorities' changes")
    p.add_argument("--product", default=DEFAULT_PRODUCT, help="product for --stage create")
    p.add_argument("--app-name", help="override the generated app name")
    p.add_argument("--if-exists", choices=[e.value for e in ExistsPolicy], default="error")
    p.add_argument("--apply", action="store_true", help="required for any write")
    p.add_argument("--restore", help="a before-snapshot to write back")
    p.add_argument("--base-url", default=DEFAULT_BASE)
    p.add_argument("--org", default=DEFAULT_ORG)
    p.add_argument("--header", action="append", default=[])
    p.add_argument("--snapshot-dir", default="snapshots")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


# ---------------------------------------------------------------- errors


class Stop(Exception):
    """A stage cannot go on. `main` prints the message and exits 1."""


# Refusals the modules raise. `main` prints them as "<Type>: <message>";
# the ones in BARE_REFUSALS already read as a sentence and print as they are.
REFUSALS = (AccountInfoError, AuthError, JurisdictionError, PlaceError,
            RevisionError, RoleError, SignupError)
BARE_REFUSALS = (IntegrationError, NotInPendingBatchError)


@contextmanager
def explain_api_error(prefix: str, hint: str = ""):
    """Report an ApiError from this block as "<prefix>: <error>", then `hint`,
    where the stage has more to say than `main`'s "API ERROR"."""
    try:
        yield
    except ApiError as exc:
        raise Stop(f"{prefix}: {exc}" + (f"\n\n{hint}" if hint else "")) from exc


def stopped(exc: PartialProvisionError, gap: str = "") -> str:
    return (f"{gap}STOPPED at {exc.failed_step}\n"
            f"  cause: {exc.cause}\n"
            f"  completed: {', '.join(exc.result.steps) or 'nothing'}")


def print_bbox(polygon) -> None:
    box = bbox(polygon)
    print(f"bbox       : lon {box[0]:.4f}..{box[2]:.4f}  "
          f"lat {box[1]:.4f}..{box[3]:.4f}")


def print_boundaries_and_exit(stage: str) -> None:
    known = available_boundaries()
    if known or stage == "provision":
        listing = "\n  ".join(p.stem for p in known) if known else "(none)"
        needs = "--geojson, or --skip-jurisdiction" if stage == "provision" else "--geojson"
        sys.exit(f"--stage {stage} needs {needs}.\n"
                 f"Available in src/logic/data:\n  {listing}")
    sys.exit(f"--stage {stage} needs --geojson <name or path>. "
             "src/logic/data holds no .geojson files yet.")


# --------------------------------------------------------------- session


def stage_session(args, _client) -> int:
    cache = TokenCache(args.session_file)

    if args.forget:
        cache.clear()
        print(f"removed {args.session_file}")
        return 0

    if args.sign_in:
        from logic.browser_auth import BrowserAuthError, PlaywrightMissing, sign_in
        try:
            result = sign_in(
                profile_dir=args.browser_profile,
                headless=False if args.show_browser else None,
                on_status=lambda m: print(f"  {m}"),
            )
        except PlaywrightMissing as exc:
            raise Stop(str(exc)) from exc
        except BrowserAuthError as exc:
            raise Stop(f"sign-in failed: {exc}") from exc
        cache.save_refresh_token(result.refresh_token)
        print("stored the session")
    elif args.from_har:
        token = refresh_token_from_har(args.from_har)
        cache.save_refresh_token(token)
        print(f"took the refresh token from {args.from_har}")
    elif args.refresh_token:
        cache.save_refresh_token(args.refresh_token)
        print("stored the refresh token")

    if not cache.refresh_token:
        print("No session stored. To start one:\n")
        print("    python smoke_test.py --stage session --sign-in\n")
        print("A browser opens; sign in with Google once. After that it")
        print("reuses that sign-in, so later runs need nothing from you.\n")
        print("Or, without a browser: sign in with DevTools recording,")
        print("save the HAR, and run")
        print("    python smoke_test.py --stage session --from-har login.har")
        return 1

    with explain_api_error("refresh failed",
                           "The refresh token has probably expired. Sign in again and "
                           "re-run with --from-har."):
        session = cache.access_token(cli_client(SCORPIUS_BASE), force=args.apply)

    left = session.seconds_left()
    print(f"signed in : {session.username or 'unknown'}")
    if left:
        print(f"token     : valid for {left / 60:.0f} min")
    print(f"stored in : {args.session_file}  (do not commit this)")
    print("\nOther stages will now mint their own token; no pasting needed.")
    return 0


# ------------------------------------------------------ signup / confirm


def unauthenticated_portal(args) -> HttpClient:
    print(f"portal    : {args.portal_base_url}")
    print("auth      : none (these calls are unauthenticated)\n")
    return cli_client(args.portal_base_url)


def stage_confirm(args, _client) -> int:
    portal = unauthenticated_portal(args)
    if not args.confirm_token:
        sys.exit("--stage confirm needs --confirm-token "
                 "(the token, or the whole link from the email)")
    if not args.apply:
        from logic.signup import token_from_link
        print(f"token     : ...{token_from_link(args.confirm_token)[-12:]}")
        print("\n[dry-run] nothing sent. Re-run with --apply.")
        return 0
    confirm_email(portal, args.confirm_token)
    print("email confirmed.")
    return 0


def stage_signup(args, _client) -> int:
    portal = unauthenticated_portal(args)
    missing = [n for n, v in (("--email", args.email),
                              ("--agency-name", args.agency_name),
                              ("--first-name", args.first_name),
                              ("--last-name", args.last_name)) if not v]
    if missing:
        sys.exit(f"--stage signup needs {', '.join(missing)}")

    password = args.password or default_password(args.email)

    print(f"email     : {args.email}")
    print(f"agency    : {args.agency_name}   (becomes the authority name)")
    print(f"contact   : {args.first_name} {args.last_name}")
    print(f"password  : {password}")
    print()

    with explain_api_error("API ERROR", "Addresses cannot be reused -- try a different +tag."):
        result = sign_up(
            portal,
            email=args.email,
            agency_name=args.agency_name,
            first_name=args.first_name,
            last_name=args.last_name,
            password=password,
            dry_run=not args.apply,
        )

    if not args.apply:
        print("[dry-run] nothing sent. Re-run with --apply.")
        return 0

    print(result.summary())
    print()
    print("Next:")
    print(f"  1. open the email sent to {args.email} and follow step 1 of its")
    print("     instructions, or run:")
    print("       --stage confirm --confirm-token \"<the link>\" --apply")
    print(f"  2. configure the account:")
    print(f'       --authority-id "{args.agency_name}" --stage provision '
          f"--geojson <file> --apply")
    return 0


# ------------------------------------------------------------- Andromeda


def andromeda_client(args) -> HttpClient:
    """Say which credential is in use, build the client, and turn an
    authority name into its id."""
    token = os.environ.get("ANDROMEDA_TOKEN")
    print(f"base URL  : {args.base_url}")

    if not token:
        cache = TokenCache(args.session_file)
        if cache.refresh_token:
            try:
                session = cache.access_token(cli_client(SCORPIUS_BASE))
                token = session.token
                left = session.seconds_left()
                print(f"token     : minted for {session.username or 'you'}"
                      + (f", valid for {left / 60:.0f} min" if left else ""))
            except (AuthError, ApiError) as exc:
                print(f"token     : could not be minted ({exc})")
                print("            sign in again and run --stage session --from-har")
        else:
            inspect_token(token)
    else:
        inspect_token(token)
    if os.environ.get("ANDROMEDA_COOKIE"):
        print("cookie    : set (not needed for bearer auth)")
    for h in args.header:
        print(f"header    : {h.split(':')[0].strip()} (from --header)")
    print()

    client = cli_client(
        args.base_url, org=args.org, token=token,
        cookie=os.environ.get("ANDROMEDA_COOKIE"), headers=args.header,
    )

    if args.authority_id and not str(args.authority_id).strip().isdigit():
        wanted = args.authority_id
        with explain_api_error(f"API ERROR while looking up {wanted!r}"):
            try:
                args.authority_id = resolve_authority_id(client, wanted)
            except (AuthorityNotFoundError, AmbiguousAuthorityError) as exc:
                raise Stop(f"{type(exc).__name__}: {exc}") from exc
        print(f"authority : {wanted!r} -> id {args.authority_id}\n")

    return client


def stage_create(args, client) -> int:
    integration = create_integration(
        client, args.authority_id,
        app_name=args.app_name,
        product=args.product,
        if_exists=ExistsPolicy(args.if_exists),
        dry_run=not args.apply,
    )

    if not args.apply:
        print(f"[dry-run] would create {integration.app_name!r} "
              f"(product {integration.product}) on authority {args.authority_id}")
        return 0

    print(f"integration id : {integration.id}")
    print(f"app_name       : {integration.app_name}")
    print(f"product        : {integration.product}")
    if integration.consumer_key:
        print(f"consumer_key   : {integration.consumer_key}")
    if integration.consumer_secret:
        print(f"consumer_secret: {integration.consumer_secret}")
        print("  (returned only at creation -- store it now if anything needs it)")
    print(f"\nnext:  --integration-id {integration.id} --stage plan")
    return 0


def stage_catalogs(args, client) -> int:
    countries = list_countries(client)
    print(f"countries ({len(countries)}):")
    for code, name in sorted(countries.items()):
        print(f"  {code:6} {name}")
    if args.country:
        states = list_states(client, args.country)
        print(f"\nstates/regions in {args.country} ({len(states)}):")
        for code, name in sorted(states.items()):
            print(f"  {code:6} {name}")
    else:
        print("\npass --country CODE to list its states/regions")
    return 0


def stage_account_info(args, client) -> int:
    current = get_authority(client, args.authority_id)
    attrs = current.get("attributes") or {}
    print(f"authority   : {current.get('name')} (id {current.get('id')})")
    print(f"account_id  : {current.get('account_id')!r}")
    print(f"dispatch    : {current.get('dispatch_type')!r}")
    print(f"country     : {attrs.get('country')!r}")
    print(f"state       : {attrs.get('state')!r}")
    print(f"org id      : {current.get('organization_id')}")
    print()

    dispatch_type = None if args.keep_dispatch_type else args.dispatch_type
    if dispatch_type is not None:
        print(f"dispatch   : will be set to {dispatch_type}")

    try:
        report = update_account_info(
            client, args.authority_id,
            account_id=args.account_id,
            country=args.country,
            state=args.state,
            dispatch_type=dispatch_type,
            dry_run=not args.apply,
        )
    except AccountInfoError as exc:     # without the blank line this stage's API errors get
        raise Stop(f"{type(exc).__name__}: {exc}") from exc

    for name, (before, after) in sorted(report.changed.items()):
        print(f"  {name}: {before!r} -> {after!r}")
    for name, reason in sorted(report.skipped.items()):
        print(f"  {name}: SKIPPED -- {reason}")
    if report.is_noop:
        print("already up to date; nothing sent.")
        return 0
    if not args.apply:
        print("\n[dry-run] nothing sent. Re-run with --apply.")
        return 0
    print(f"\n{report.summary()}")
    return 0


def stage_export_boundary(args, client) -> int:
    polygon = export_boundary(client, args.authority_id, args.jurisdiction_id)

    print(f"features   : {len(polygon['features'])}")
    print_bbox(polygon)
    print(f"size       : {len(json.dumps(polygon))} bytes")

    out = args.out or str(
        Path("src/logic/data") / f"authority-{args.authority_id}.geojson"
    )
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(polygon, indent=2), encoding="utf-8")
    print(f"\nwritten to : {out}")
    print(f"use it with: --geojson {Path(out).stem}")
    return 0


# ------------------------------------------------------------------ roles


def portal_client(args) -> HttpClient:
    portal_token = os.environ.get("RAPIDSOS_PORTAL_TOKEN")
    print("portal    : " + args.portal_base_url)

    if args.portal_email:
        # A generated account can log itself in and configure its own
        # roles -- no pasted credential needed.
        with explain_api_error("login failed",
                               "Check the address and password. Accounts the tool "
                               "creates use the shared sandbox password."):
            session = log_in(cli_client(args.portal_base_url),
                             args.portal_email, args.portal_password)
        portal_token = session.token
        left = session.seconds_left()
        print(f"auth      : logged in as {session.username or args.portal_email}"
              + (f", valid for {left / 60:.0f} min" if left else ""))
    elif portal_token:
        inspect_token(portal_token)
    else:
        sys.exit(
            "--stage roles needs a portal credential. Either:\n"
            "  --portal-email you+tag@rapidsos.com   (logs in; no paste needed)\n"
            "or paste a token copied from api-sandbox.rapidsosportal.com:\n"
            "  $env:RAPIDSOS_PORTAL_TOKEN='<token>'"
        )
    print()
    return cli_client(args.portal_base_url, org=args.org, token=portal_token)


def stage_roles(args, client) -> int:
    portal = portal_client(args)

    org_id = args.organization_id
    if not org_id:
        with explain_api_error("API ERROR reading the authority"):
            record = get_authority(client, args.authority_id)
        org_id = str(record.get("organization_id") or "")
        if not org_id:
            sys.exit("the authority has no organization_id; pass --organization-id")
        print(f"authority {args.authority_id} -> organization {org_id}\n")

    if args.restore_roles:
        snap = json.loads(Path(args.restore_roles).read_text())
        print(f"restoring from {args.restore_roles}")
        for saved in snap.get("roles", []):
            print(f"  {saved['name']}: {len(saved['permissions'])} permission(s)")
        if not args.apply:
            print("\n[dry-run] nothing sent. Re-run with --apply.")
            return 0
        report = restore_roles(portal, snap, organization_id=org_id)
        print(f"\n{report.summary()}")
        return 0

    catalog = list_permissions(portal, org_id)
    roles = list_roles(portal, org_id)
    print(f"catalog   : {len(catalog)} permission(s), "
          f"{sum(1 for p in catalog if p.get('rsp_rbac'))} administrative")
    print("roles     :")
    for role in roles:
        print(f"  {role}")

    snap = snapshot_roles(portal, org_id)
    snap_path = snapshot(snap, f"roles-{org_id}", "before", args.snapshot_dir)
    print(f"snapshot  : {snap_path}")
    print()

    report = enable_all_data_sources(portal, org_id, dry_run=not args.apply)

    for role, names in sorted(report.granted.items()):
        print(f"  {role}: +{len(names)}  {', '.join(names[:6])}"
              + (" ..." if len(names) > 6 else ""))
    for role, names in sorted(report.revoked.items()):
        print(f"  {role}: -{len(names)}  {', '.join(names)}")
        print("    !! these would be REMOVED. Stop and check why the role has them.")
    for role in report.unchanged:
        print(f"  {role}: already correct")

    print(f"\n{report.summary()}")
    if not args.apply:
        print("[dry-run] nothing sent. Re-run with --apply.")
    else:
        print(f"undo with:  --stage roles --restore-roles {snap_path} --apply")
        print("Then check the portal: Admin -> Role and Access, and that the "
              "Alerts tab appears.")
    return 0


# ------------------------------------------------------- place / sandbox


def show_place(args) -> None:
    """Resolve --place and print what it matched. Shared by place and sandbox."""
    if not args.place:
        sys.exit('--stage %s needs --place, e.g. --place "Lincoln, Nebraska"'
                 % args.stage)
    try:
        from logic.places import account_fields, resolve_place, to_andromeda_polygon
    except ImportError as exc:
        sys.exit(f"place lookup needs the geospatial extras: {exc}\n"
                 "  pip install geopandas pandas shapely")

    place = resolve_place(args.place, require_status=not args.allow_unverified_scope)

    fields = account_fields(place, use_ecc_name=args.use_ecc_name)
    if args.account_id:
        fields["account_id"] = args.account_id
    polygon = to_andromeda_polygon(place)

    print(f"query      : {args.place!r}")
    print(f"resolved   : {place}")
    print(f"scope      : {place.status} ({len(place.eccs)} ECC(s) registered)")
    for ecc in place.eccs:
        print(f"               {ecc.get('name')} (FCC {ecc.get('fcc_psap_id')})")
    print_bbox(polygon)
    print(f"polygon    : {len(json.dumps(polygon))} bytes")
    print()
    print("account fields:")
    for key in ("authority_name", "account_id", "country", "state"):
        print(f"  {key:16}: {fields[key]!r}")


def stage_place(args, _client) -> int:
    show_place(args)
    print("\n[place] lookup only. Use --stage sandbox to provision.")
    return 0


def stage_sandbox(args, client) -> int:
    show_place(args)
    print()
    try:
        account = create_sandbox_account(
            client, args.place,
            authority=args.authority_id,
            account_id=args.account_id,
            use_ecc_name=args.use_ecc_name,
            require_status=not args.allow_unverified_scope,
            standard=load_standard(args.standard),
            product=args.product,
            app_name=args.app_name,
            if_exists=ExistsPolicy(args.if_exists),
            allow_other_authorities=args.allow_other_authorities,
            dry_run=not args.apply,
        )
    except PartialProvisionError as exc:
        raise Stop(stopped(exc)) from exc

    if not args.apply:
        print("[dry-run] nothing sent. Re-run with --apply.")
        return 0

    for step in account.provision.steps:
        print(f"  done: {step}")
    print(f"\n{account.summary()}")
    return 0


# -------------------------------------------------------------- provision


def stage_provision(args, client) -> int:
    polygon = None
    if not args.skip_jurisdiction:
        if not args.geojson:
            print_boundaries_and_exit("provision")
        polygon = load_geojson(args.geojson)
        print(f"boundary   : {len(polygon['features'])} feature(s)")
        print_bbox(polygon)
    else:
        print("boundary   : skipped (--skip-jurisdiction)")

    print(f"product    : {args.product}")
    print(f"app_name   : {args.app_name or '(generated from the authority name)'}")
    print(f"standard   : {args.standard}")
    print()

    try:
        standard = load_standard(args.standard)
    except Exception as exc:
        raise Stop(f"could not load the standard set: {exc}") from exc

    try:
        result = provision_authority(
            client, args.authority_id, polygon=polygon,
            skip_jurisdiction=args.skip_jurisdiction,
            app_name=args.app_name,
            product=args.product,
            if_exists=ExistsPolicy(args.if_exists),
            standard=standard,
            revision_number=args.revision_number,
            revision_date=args.revision_date,
            allow_other_authorities=args.allow_other_authorities,
            require_active=args.require_active,
            dry_run=not args.apply,
        )
    except PartialProvisionError as exc:
        lines = [stopped(exc, gap="\n")]
        if exc.result.jurisdiction and exc.result.jurisdiction.id:
            lines.append(f"  jurisdiction {exc.result.jurisdiction.id} exists")
        if exc.result.integration and exc.result.integration.id:
            lines.append(f"  integration {exc.result.integration.id} exists -- "
                         f"retry capabilities with "
                         f"--integration-id {exc.result.integration.id} --stage apply --apply")
        raise Stop("\n".join(lines)) from exc

    if not args.apply:
        print("[dry-run] nothing sent. Re-run with --apply.")
        return 0

    print()
    for step in result.steps:
        print(f"  done: {step}")
    print(f"\n{result.summary()}")
    if result.integration and result.integration.consumer_secret:
        print(f"\nconsumer_key   : {result.integration.consumer_key}")
        print(f"consumer_secret: {result.integration.consumer_secret}")
        print("  (returned only at creation -- store it now if anything needs it)")
    if result.capabilities and result.capabilities.alerts_skipped:
        print(f"\nalerts skipped ({len(result.capabilities.alerts_skipped)}): "
              "the jurisdiction overlaps one that already has them")
    return 0


# ----------------------------------------------------------- jurisdiction


def stage_jurisdiction(args, client) -> int:
    if not args.geojson:
        print_boundaries_and_exit("jurisdiction")
    polygon = load_geojson(args.geojson)

    box = bbox(polygon)
    print(f"boundary   : {len(polygon['features'])} feature(s)")
    print(f"bbox       : lon {box[0]:.4f}..{box[2]:.4f}  lat {box[1]:.4f}..{box[3]:.4f}")
    print("             (check that looks like the right part of the world)")
    print(f"activate   : {'no (--no-activate)' if args.no_activate else 'yes'}")

    existing = list_jurisdictions(client, args.authority_id)
    print(f"existing   : {len(existing)} jurisdiction(s) on authority {args.authority_id}")
    for j in existing:
        print(f"  {j}")

    try:
        result = attach_and_activate(
            client, args.authority_id, polygon=polygon,
            activate=not args.no_activate,
            revision_number=args.revision_number,
            revision_date=args.revision_date,
            allow_other_authorities=args.allow_other_authorities,
            dry_run=not args.apply,
        )
    except PartialActivationError as exc:
        raise Stop(f"\nPARTIAL: {exc}\n"
                   f"  the jurisdiction exists as id {exc.jurisdiction.id}\n"
                   f"  activate it later with:  --stage activate --apply") from exc

    if not args.apply:
        steps = "create the jurisdiction as Verified, then set it to Pending"
        if not args.no_activate:
            steps += ", then create and publish a revision"
        print(f"\n[dry-run] would {steps}. Nothing sent.")
        return 0

    print(f"\n{result.summary()}")
    if not result.activated:
        print("\nnext:  --stage activate --apply   (publishes the revision "
              "that makes it Active)")
    else:
        print(f"\nConfirm in the UI that authority {args.authority_id}'s "
              "jurisdiction is now Active.")
    return 0


# ------------------------------------------------------ pending / activate


def show_pending(args, client):
    """Print the pending revision's batch, marking this authority's entries."""
    pending = get_pending(client)

    print(f"pending revision : {pending.id}")
    print(f"revision_number  : {pending.revision_number}")
    print(f"revision_date    : {pending.revision_date}")
    print(f"batch            : {len(pending.created)} created, "
          f"{len(pending.modified)} modified, {len(pending.deleted)} deleted")
    print(f"authorities      : {pending.authority_ids or '(none)'}")

    for entry in pending.entries:
        mine = str(entry.get("authority_id")) == str(args.authority_id)
        print(f"  {'>' if mine else ' '} authority {entry.get('authority_id')} "
              f"jurisdiction {entry.get('id')}: "
              f"ingress={entry.get('ingress_status')} "
              f"egress={entry.get('egress_status')} "
              f"shapes={len(entry.get('shapes') or [])}"
              f"{'   <-- yours' if mine else ''}")

    others = [a for a in pending.authority_ids if a != str(args.authority_id)]
    if others:
        print(f"\n!! the batch also holds changes for {', '.join(others)}.")
        print("   Publishing would activate their work too. Use "
              "--allow-other-authorities only if that is intended.")
    return pending


def stage_pending(args, client) -> int:
    show_pending(args, client)
    number = args.revision_number or default_revision_number()
    print(f"\n[pending] read-only. Activating would stamp this as "
          f"revision number {number}.")
    return 0


def stage_activate(args, client) -> int:
    pending = show_pending(args, client)
    if not args.apply:
        print()
    result = activate_jurisdiction(
        client, args.authority_id,
        revision_number=args.revision_number,
        revision_date=args.revision_date,
        allow_other_authorities=args.allow_other_authorities,
        dry_run=not args.apply,
        pending=pending,
    )

    if not args.apply:
        print(f"[dry-run] would publish revision {result.published_revision_id} "
              f"as number {result.revision_number} ({result.revision_date})")
        print("[dry-run] nothing sent. Re-run with --apply.")
        return 0

    print(f"\n{result.summary()}")
    print("\nNote: the new pending revision may still list the same "
          "jurisdiction -- that is expected, not a failure.")
    print(f"Confirm in the UI that authority {args.authority_id}'s "
          "jurisdiction is now Active.")
    return 0


# ----------------------------------------------------------- capabilities


def restorable(handler):
    """--restore takes over any capabilities stage, as it always has."""
    def run(args, client) -> int:
        if not args.restore:
            return handler(args, client)
        body = json.loads(Path(args.restore).read_text())
        if not args.apply:
            print(f"[dry-run] would restore {len(body['capabilities'])} capabilities "
                  f"from {args.restore}")
            return 0
        client.patch(caps_path(args.authority_id, args.integration_id), body)
        print(f"restored integration {args.integration_id} from {args.restore}")
        return 0
    return run


def read_live(args, client):
    """Read and snapshot the live capabilities; every capabilities stage starts here."""
    live = client.get(caps_path(args.authority_id, args.integration_id))
    path = snapshot(live, args.integration_id, "before", args.snapshot_dir)
    enabled = sum(1 for c in live["capabilities"] if c["authority_enabled"] or c["rsos_enabled"])
    print(f"read OK: {len(live['capabilities'])} capabilities, {enabled} currently enabled")
    print(f"snapshot: {path}")
    return live, path


def stage_read(args, client) -> int:
    read_live(args, client)
    return 0


def stage_verify(args, client) -> int:
    live, _ = read_live(args, client)
    _, report = plan(live, load_standard(args.standard), integration_id=args.integration_id)
    if report.is_noop:
        print("\nMATCHES the standard set.")
        return 0
    print(f"\nDOES NOT match: {len(report.changed)} difference(s)")
    show(report)
    return 1


def stage_plan(args, client) -> int:
    live, _ = read_live(args, client)
    body, report = plan(live, load_standard(args.standard), integration_id=args.integration_id)
    show(report)
    planned = snapshot(body, args.integration_id, "planned-body", args.snapshot_dir)
    print(f"\n[plan] nothing sent. Body we would PATCH: {planned}")
    return 0


def stage_apply(args, client) -> int:
    live, path = read_live(args, client)
    standard = load_standard(args.standard)
    if not args.apply:
        sys.exit("--stage apply also needs --apply. Run --stage plan first.")

    body, _ = plan(live, standard, integration_id=args.integration_id)
    planned = snapshot(body, args.integration_id, "planned-body", args.snapshot_dir)
    print(f"body to be sent: {planned}")

    with explain_api_error("\nAPI ERROR", f"restore with:  --restore {path} --apply"):
        try:
            report = apply_standard_capabilities(
                client, args.authority_id, args.integration_id, standard=standard
            )
        except CapabilityWriteError as exc:
            lines = [f"\nWRITE FAILED: {exc}"]
            lines += [f"  {key}: asked {want}, got {got}" for key, want, got in exc.mismatched]
            lines.append(f"\nrestore with:  --restore {path} --apply")
            raise Stop("\n".join(lines)) from exc

    show(report)
    if report.alerts_skipped:
        print(f"\nalerts skipped ({len(report.alerts_skipped)}) -- this authority's "
              "jurisdiction overlaps one that already has them:")
        for key in report.alerts_skipped:
            print(f"  {key}")
        print(f"  first attempt failed with: {report.first_attempt_error}")
    print(f"\n{report.summary()}")
    print(f"rollback:  --restore {path} --apply")
    return 0


# --------------------------------------------------------------- dispatch


@dataclass(frozen=True)
class Stage:
    run: Callable[[argparse.Namespace, Optional[HttpClient]], int]
    andromeda: bool = True       # needs --authority-id and an Andromeda client
    integration: bool = False    # needs --integration-id
    gap: str = "\n"              # printed before an error, as the stage always has


# In the order argparse lists them.
STAGES = {
    "read": Stage(restorable(stage_read), integration=True),
    "plan": Stage(restorable(stage_plan), integration=True),
    "apply": Stage(restorable(stage_apply), integration=True),
    "verify": Stage(restorable(stage_verify), integration=True),
    "create": Stage(stage_create),
    "scratch": Stage(stage_create),
    "pending": Stage(stage_pending),
    "activate": Stage(stage_activate),
    "jurisdiction": Stage(stage_jurisdiction),
    "provision": Stage(stage_provision),
    "account-info": Stage(stage_account_info),
    "catalogs": Stage(stage_catalogs),
    "place": Stage(stage_place, gap=""),
    "sandbox": Stage(stage_sandbox, gap=""),
    "roles": Stage(stage_roles, gap=""),
    "export-boundary": Stage(stage_export_boundary, gap=""),
    "signup": Stage(stage_signup, andromeda=False, gap=""),
    "confirm": Stage(stage_confirm, andromeda=False, gap=""),
    "session": Stage(stage_session, andromeda=False, gap=""),
}


def main() -> int:
    args = build_parser().parse_args()
    stage = STAGES[args.stage]

    if stage.andromeda and not args.authority_id:
        sys.exit("--authority-id is required for this stage")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )

    try:
        client = andromeda_client(args) if stage.andromeda else None
        if stage.integration and not args.integration_id:
            sys.exit("--integration-id is required for this stage")
        return stage.run(args, client)
    except Stop as exc:
        print(exc)
    except ApiError as exc:
        print(f"{stage.gap}API ERROR: {exc}")
    except BARE_REFUSALS as exc:
        print(f"{stage.gap}{exc}")
    except REFUSALS as exc:
        print(f"{stage.gap}{type(exc).__name__}: {exc}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
