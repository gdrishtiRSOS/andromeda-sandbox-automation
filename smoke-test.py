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
on schedule regardless of activity.

Stages
------
  1. Can I read?        --stage read
  2. What would change? --stage plan
  3. Do it              --stage apply --apply
  4. Is it still right? --stage verify
  Undo                  --restore snapshots/<file>-before.json --apply
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

from src.capabilities import (
    CapabilityWriteError,
    apply_standard_capabilities,
    load_standard,
    plan,
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
    p.add_argument("--standard", default="src/data/boiler911_capabilities.json")
    p.add_argument("--stage", choices=["read", "plan", "apply", "verify", "scratch"], default="read")
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

    # ---------------------------------------------------------- scratch
    if args.stage == "scratch":
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        app_name = f"ZZ capability-test {stamp}"
        if not args.apply:
            print(f"[dry-run] would create integration {app_name!r} "
                  f"on authority {args.authority_id}")
            return 0
        created = client.post(
            f"/v1/andromeda/authorities/{args.authority_id}/integrations",
            {"app_name": app_name, "product": "RapidSOS Portal", "owner": app_name},
        )
        print(f"created scratch integration {created['id']} ({app_name})")
        print("re-run with --integration-id", created["id"])
        return 0

    if not args.integration_id:
        sys.exit("--integration-id is required for this stage")

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