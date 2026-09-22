#!/usr/bin/env python3
"""Smoke-test `capabilities.py` against a real Andromeda integration.

Throwaway harness for validating the module by hand. The real project should
supply its own client; this one exists so you can exercise the module before
that client is written.

Safety: does nothing destructive unless you pass --apply. Every run that
touches an integration writes a before-snapshot you can restore from.

Setup
-----
Andromeda uses a bearer token. In DevTools, right-click an Andromeda API
request -> Copy as cURL, and take the value of the `authorization` header:

    export ANDROMEDA_TOKEN='eyJhbGciOi...'      # with or without "Bearer "

The token is short-lived -- its `exp` claim is a fixed timestamp, so it dies
on schedule regardless of activity. If a command 401s after an earlier one
worked, grab a fresh one.

Never paste the token on the command line; it ends up in shell history.

Stages
------
  Capabilities (runbook step 6)
    1. Can I read?        --stage read
    2. What would change? --stage plan
    3. Do it              --stage apply --apply
    4. Is it still right? --stage verify
    Undo                  --restore snapshots/<file>-before.json --apply

  Integrations (runbook step 5)
    --stage create                 dry run
    --stage create --apply         create it

  Everything (runbook steps 4, 7, 5, 6)
    --stage provision --geojson dublin                  dry run
    --stage provision --geojson dublin --apply          boundary, activate,
                                                        integration, capabilities
    --stage provision --skip-jurisdiction --apply       boundary already live

  Jurisdiction boundary + activation (runbook steps 4 and 7)
    Boundary files live in andromeda/data/ and can be named without the path
    or extension: --geojson dublin finds andromeda/data/dublin.geojson.
    A full path also works. Omit --geojson to list what is available.

    --stage jurisdiction --geojson dublin                    dry run
    --stage jurisdiction --geojson dublin --apply            create AND publish
    --stage jurisdiction --geojson dublin --apply --no-activate   create only

  Jurisdiction activation (runbook step 7)
    --stage pending                inspect the batch, read-only
    --stage activate               dry run
    --stage activate --apply       create + publish the revision

  --stage pending and --stage activate do not need --integration-id.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("needs requests:  pip install requests")

from andromeda.capabilities import (
    CapabilityWriteError,
    apply_standard_capabilities,
    load_standard,
    plan,
)
from andromeda.integrations import (
    DEFAULT_PRODUCT,
    ExistsPolicy,
    IntegrationError,
    IntegrationExistsError,
    ProductNotFoundError,
    create_integration,
)
from andromeda.jurisdictions import (
    InvalidGeometryError,
    JurisdictionError,
    attach_jurisdiction,
    available_boundaries,
    bbox,
    list_jurisdictions,
    load_geojson,
)
from andromeda.revisions import (
    RevisionError,
    activate_jurisdiction,
    default_revision_number,
    get_pending,
)
from andromeda.workflows import (
    NotInPendingBatchError,
    PartialActivationError,
    PartialProvisionError,
    attach_and_activate,
    provision_authority,
)

DEFAULT_BASE = "https://andromeda.sandbox.rapidsos.com"
DEFAULT_ORG = "RapidSOS Admin"


class ApiError(RuntimeError):
    def __init__(self, status, text, method, path):
        self.status = status
        self.text = text
        super().__init__(f"{status} on {method} {path}: {text[:300]}")


class SimpleClient:
    """Minimal implementation of the AndromedaClient protocol."""

    def __init__(self, base_url: str, org: str, cookie: str | None, headers: list[str],
                 token: str | None = None):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"x-rapidsos-org": org, "Accept": "application/json"})
        if token:
            # tolerate the value being pasted with or without the "Bearer " prefix
            token = token.strip()
            if not token.lower().startswith("bearer "):
                token = f"Bearer {token}"
            self.s.headers["Authorization"] = token
        if cookie:
            self.s.headers["Cookie"] = cookie
        for h in headers:
            name, _, value = h.partition(":")
            if not value:
                sys.exit(f"bad --header {h!r}, expected 'Name: value'")
            self.s.headers[name.strip()] = value.strip()

    def _call(self, method: str, path: str, **kw):
        r = self.s.request(method, f"{self.base}{path}", timeout=60, **kw)
        if r.status_code in (401, 403):
            sent = self.s.headers.get("Authorization")
            sys.exit(
                f"{r.status_code} on {method} {path}\n"
                f"Authorization header sent: {'yes' if sent else 'NO -- none was set'}\n"
                f"Server said: {r.text[:200]}\n"
                "Most likely the token expired. Re-copy the authorization header "
                "value from a fresh DevTools request."
            )
        if not r.ok:
            raise ApiError(r.status_code, r.text, method, path)
        return r.json() if r.content else None

    def get(self, path: str):
        return self._call("GET", path)

    def patch(self, path: str, json):
        return self._call("PATCH", path, json=json)

    # not part of the protocol -- only used to create a scratch integration
    def post(self, path: str, json):
        return self._call("POST", path, json=json)


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

    import base64

    try:
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(pad))
    except Exception:
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


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--authority-id", required=True)
    p.add_argument("--integration-id", help="omit with --stage scratch to create one")
    p.add_argument("--standard", default="andromeda/data/standard_capabilities.json")
    p.add_argument("--stage",
                   choices=["read", "plan", "apply", "verify", "create", "scratch",
                            "pending", "activate", "jurisdiction", "provision"],
                   default="read", help="'scratch' is an alias for 'create'")
    p.add_argument("--revision-number", type=int,
                   help="override the generated revision number (default: day + month)")
    p.add_argument("--revision-date", help="YYYY-MM-DD (default: today)")
    p.add_argument("--geojson",
                   help="boundary for --stage jurisdiction: a name in andromeda/data/ "
                        "(with or without .geojson) or a full path")
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
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )

    token = os.environ.get("ANDROMEDA_TOKEN")
    print(f"base URL  : {args.base_url}")
    inspect_token(token)
    if os.environ.get("ANDROMEDA_COOKIE"):
        print("cookie    : set (not needed for bearer auth)")
    for h in args.header:
        print(f"header    : {h.split(':')[0].strip()} (from --header)")
    print()

    client = SimpleClient(
        args.base_url, args.org,
        os.environ.get("ANDROMEDA_COOKIE"), args.header, token,
    )

    # ---------------------------------------------------------- create
    if args.stage in ("create", "scratch"):
        try:
            integration = create_integration(
                client, args.authority_id,
                app_name=args.app_name,
                product=args.product,
                if_exists=ExistsPolicy(args.if_exists),
                dry_run=not args.apply,
            )
        except (IntegrationExistsError, ProductNotFoundError, IntegrationError) as exc:
            print(f"\n{exc}")
            return 1
        except ApiError as exc:
            print(f"\nAPI ERROR: {exc}")
            return 1

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

    if args.stage not in ("pending", "activate", "jurisdiction", "provision") \
            and not args.integration_id:
        sys.exit("--integration-id is required for this stage")

    # --------------------------------------------------------- provision
    if args.stage == "provision":
        polygon = None
        if not args.skip_jurisdiction:
            if not args.geojson:
                known = available_boundaries()
                listing = "\n  ".join(p.stem for p in known) if known else "(none)"
                sys.exit(f"--stage provision needs --geojson, or --skip-jurisdiction.\n"
                         f"Available in andromeda/data:\n  {listing}")
            try:
                polygon = load_geojson(args.geojson)
            except (InvalidGeometryError, JurisdictionError) as exc:
                print(f"\n{type(exc).__name__}: {exc}")
                return 1
            box = bbox(polygon)
            print(f"boundary   : {len(polygon['features'])} feature(s)")
            print(f"bbox       : lon {box[0]:.4f}..{box[2]:.4f}  "
                  f"lat {box[1]:.4f}..{box[3]:.4f}")
        else:
            print("boundary   : skipped (--skip-jurisdiction)")

        print(f"product    : {args.product}")
        print(f"app_name   : {args.app_name or '(generated from the authority name)'}")
        print(f"standard   : {args.standard}")
        print()

        try:
            standard = load_standard(args.standard)
        except Exception as exc:
            print(f"could not load the standard set: {exc}")
            return 1

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
            print(f"\nSTOPPED at {exc.failed_step}")
            print(f"  cause: {exc.cause}")
            print(f"  completed: {', '.join(exc.result.steps) or 'nothing'}")
            if exc.result.jurisdiction and exc.result.jurisdiction.id:
                print(f"  jurisdiction {exc.result.jurisdiction.id} exists")
            if exc.result.integration and exc.result.integration.id:
                print(f"  integration {exc.result.integration.id} exists -- "
                      f"retry capabilities with "
                      f"--integration-id {exc.result.integration.id} --stage apply --apply")
            return 1
        except ApiError as exc:
            print(f"\nAPI ERROR: {exc}")
            return 1

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

    # ------------------------------------------------------ jurisdiction
    if args.stage == "jurisdiction":
        if not args.geojson:
            known = available_boundaries()
            if known:
                listing = "\n  ".join(p.stem for p in known)
                sys.exit(f"--stage jurisdiction needs --geojson.\n"
                         f"Available in andromeda/data:\n  {listing}")
            sys.exit("--stage jurisdiction needs --geojson <name or path>. "
                     "andromeda/data holds no .geojson files yet.")
        try:
            polygon = load_geojson(args.geojson)
        except (InvalidGeometryError, JurisdictionError) as exc:
            print(f"\n{type(exc).__name__}: {exc}")
            return 1

        box = bbox(polygon)
        print(f"boundary   : {len(polygon['features'])} feature(s)")
        print(f"bbox       : lon {box[0]:.4f}..{box[2]:.4f}  lat {box[1]:.4f}..{box[3]:.4f}")
        print("             (check that looks like the right part of the world)")
        print(f"activate   : {'no (--no-activate)' if args.no_activate else 'yes'}")

        try:
            existing = list_jurisdictions(client, args.authority_id)
        except ApiError as exc:
            print(f"\nAPI ERROR: {exc}")
            return 1
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
            print(f"\nPARTIAL: {exc}")
            print(f"  the jurisdiction exists as id {exc.jurisdiction.id}")
            print(f"  activate it later with:  --stage activate --apply")
            return 1
        except NotInPendingBatchError as exc:
            print(f"\n{exc}")
            return 1
        except (InvalidGeometryError, JurisdictionError) as exc:
            print(f"\n{type(exc).__name__}: {exc}")
            return 1
        except ApiError as exc:
            print(f"\nAPI ERROR: {exc}")
            return 1

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

    # ------------------------------------------------- pending / activate
    if args.stage in ("pending", "activate"):
        try:
            pending = get_pending(client)
        except ApiError as exc:
            print(f"\nAPI ERROR: {exc}")
            return 1

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

        if args.stage == "pending":
            number = args.revision_number or default_revision_number()
            print(f"\n[pending] read-only. Activating would stamp this as "
                  f"revision number {number}.")
            return 0

        if not args.apply:
            print()
        try:
            result = activate_jurisdiction(
                client, args.authority_id,
                revision_number=args.revision_number,
                revision_date=args.revision_date,
                allow_other_authorities=args.allow_other_authorities,
                dry_run=not args.apply,
                pending=pending,
            )
        except RevisionError as exc:
            print(f"\n{type(exc).__name__}: {exc}")
            return 1
        except ApiError as exc:
            print(f"\nAPI ERROR: {exc}")
            return 1

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

    # ---------------------------------------------------------- restore
    if args.restore:
        body = json.loads(Path(args.restore).read_text())
        if not args.apply:
            print(f"[dry-run] would restore {len(body['capabilities'])} capabilities "
                  f"from {args.restore}")
            return 0
        client.patch(caps_path(args.authority_id, args.integration_id), body)
        print(f"restored integration {args.integration_id} from {args.restore}")
        return 0

    # ---------------------------------------------------------- read
    live = client.get(caps_path(args.authority_id, args.integration_id))
    path = snapshot(live, args.integration_id, "before", args.snapshot_dir)
    enabled = sum(1 for c in live["capabilities"] if c["authority_enabled"] or c["rsos_enabled"])
    print(f"read OK: {len(live['capabilities'])} capabilities, {enabled} currently enabled")
    print(f"snapshot: {path}")
    if args.stage == "read":
        return 0

    standard = load_standard(args.standard)

    # ---------------------------------------------------------- verify
    if args.stage == "verify":
        _, report = plan(live, standard, integration_id=args.integration_id)
        if report.is_noop:
            print("\nMATCHES the standard set.")
            return 0
        print(f"\nDOES NOT match: {len(report.changed)} difference(s)")
        show(report)
        return 1

    # ---------------------------------------------------------- plan
    if args.stage == "plan":
        body, report = plan(live, standard, integration_id=args.integration_id)
        show(report)
        planned = snapshot(body, args.integration_id, "planned-body", args.snapshot_dir)
        print(f"\n[plan] nothing sent. Body we would PATCH: {planned}")
        return 0

    # ---------------------------------------------------------- apply
    if not args.apply:
        sys.exit("--stage apply also needs --apply. Run --stage plan first.")

    body, _ = plan(live, standard, integration_id=args.integration_id)
    planned = snapshot(body, args.integration_id, "planned-body", args.snapshot_dir)
    print(f"body to be sent: {planned}")

    try:
        report = apply_standard_capabilities(
            client, args.authority_id, args.integration_id, standard=standard
        )
    except CapabilityWriteError as exc:
        print(f"\nWRITE FAILED: {exc}")
        for key, want, got in exc.mismatched:
            print(f"  {key}: asked {want}, got {got}")
        print(f"\nrestore with:  --restore {path} --apply")
        return 1
    except ApiError as exc:
        print(f"\nAPI ERROR: {exc}")
        print(f"\nrestore with:  --restore {path} --apply")
        return 1

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


if __name__ == "__main__":
    raise SystemExit(main())