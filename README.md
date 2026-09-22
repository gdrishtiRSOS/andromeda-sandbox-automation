# configure-capabilities

Apply a standard capability set to an Andromeda integration.

Given an authority and an integration that already exist, this makes that
integration's capability flags match a standard set — either the packaged
`boiler911` file or one you drop in `src/data`.

It is safe to re-run: if the integration already matches, no PATCH is sent.

## Requirements

```
pip install requests
```

## 1. Get a token

Andromeda uses a bearer token. In DevTools, right-click an Andromeda API
request → **Copy as cURL**, and take the value of the `authorization` header:

```bash
export ANDROMEDA_TOKEN='eyJhbGciOi...'      # with or without "Bearer "
```

The token is short-lived — its `exp` claim is a fixed timestamp, so it dies on
schedule regardless of activity. Every run prints how long it has left; if you
get a 401/403, re-copy it from a fresh DevTools request.

## 2. Run the stages

`smoke-test.py` drives the module. It does nothing destructive unless you pass
`--apply`, and every run that touches an integration writes a before-snapshot
to `snapshots/` that you can restore from.

```bash
# 1. Can I read?
python smoke-test.py --authority-id AUTH --integration-id INT --stage read

# 2. What would change?  (nothing is sent)
python smoke-test.py --authority-id AUTH --integration-id INT --stage plan

# 3. Do it
python smoke-test.py --authority-id AUTH --integration-id INT --stage apply --apply

# 4. Is it still right?
python smoke-test.py --authority-id AUTH --integration-id INT --stage verify

# Undo
python smoke-test.py --authority-id AUTH --integration-id INT \
  --restore snapshots/<file>-before.json --apply
```

## Using your own file instead of boiler911

Put a JSON file in `src/data/` and point `--standard` at it:

```bash
python smoke-test.py --authority-id AUTH --integration-id INT \
  --standard src/data/my_capabilities.json --stage plan
```

The file is an object with a `capabilities` list, same shape the API returns:

```json
{
  "capabilities": [
    {
      "authority_enabled": true,
      "rsos_enabled": true,
      "capability_type": {
        "name": "CCInform-Sandbox",
        "display_name": "CCInform-Sandbox",
        "category": 1
      }
    }
  ]
}
```

Capabilities are matched on `(name, category)`, not position — the API does not
guarantee ordering. Duplicate keys are rejected, since they would make the
standard ambiguous.

The easiest way to author one is to run `--stage read` against an integration
that is already configured the way you want and edit the snapshot it writes.

## What it does and does not touch

* **Overlay, not replace.** The live catalog is the authority on which
  capability types exist for an integration; flags are copied onto it rather
  than PATCHing the standard file wholesale. A capability added upstream since
  the standard was captured is left alone instead of being silently dropped.
* Capabilities in Andromeda but **not in the standard file** are left untouched
  (reported as drift).
* Capabilities in the standard file but **not offered by the integration** are
  reported and skipped.

## Alerts fallback

Alerts capabilities cannot be enabled where the authority's jurisdiction
overlaps another that already has them — Andromeda answers with a 500 rather
than a validation error. The PATCH is atomic, so a failed attempt writes
nothing.

So the full set is attempted first; if that fails and alerts changes were in the
body, the write is retried with those capabilities left at their current values,
and the run reports what was skipped. If the retry also fails, the original
error is raised — the failure was not about alerts.


