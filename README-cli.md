# RapidSOS Sandbox Account Tooling

Automates the RapidSOS Unite sandbox account runbook: creates the account,
attaches a jurisdiction boundary, publishes the revision that activates it,
adds an integration, applies a standard capability set, and enables the
portal's data sources.

All eight runbook steps are covered. The only manual action left is clicking
the confirmation link in the sign-up email, which can only be done by whoever
owns the inbox.

This README covers the command line (`smoke_test.py`). For the web page, which
runs the same steps from one form, see [README-web.md](README-web.md).

---

## Setup

```
project/
├── smoke_test.py
├── ecc_lookup.py                  # only for --place lookups
├── requirements.txt
├── src/
│   └── logic/                     # the package: `import logic`
│       ├── auth.py            browser_auth.py   tokens.py
│       ├── authorities.py     capabilities.py   integrations.py
│       ├── jurisdictions.py   places.py         revisions.py
│       ├── roles.py           signup.py         workflows.py
│       └── data/
│           ├── standard_capabilities.json
│           └── *.geojson              # boundary files
├── webapp/                        # the web page -- see README-web.md
└── tests/
```

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium    # only for --stage session --sign-in
python -m pytest tests -q                # expect: all passed
```

Tested on Python 3.9.

Run every command from the project root. Re-run the tests after copying files
in — they import every module and catch a half-updated checkout instantly.

### Tokens

Three different situations:

| Stage | Token |
|---|---|
| `signup`, `confirm` | **none** — these calls are unauthenticated |
| `roles` | `--portal-email`, or `RAPIDSOS_PORTAL_TOKEN` (portal API) |
| everything else | a stored sign-in, or `ANDROMEDA_TOKEN` (Andromeda) |

**Andromeda: sign in once a day** instead of pasting a token.

```powershell
python smoke_test.py --stage session --sign-in
```

A browser window opens. Sign in with Google once; later sign-ins reuse that
browser profile and finish on their own. The session lasts about a day, and
every other stage mints its own access token from it.

```powershell
python smoke_test.py --stage session                        # who, and for how long
python smoke_test.py --stage session --from-har login.har   # without Playwright
python smoke_test.py --stage session --forget               # sign out
```

**Portal (roles): log in as the account.** Accounts the tool creates use the
shared sandbox password, so `--stage roles --portal-email you+tag@rapidsos.com`
needs nothing pasted.

**Or paste tokens.** Either variable, when set, overrides the above:

```powershell
$env:ANDROMEDA_TOKEN='eyJhbGciOi...'          # andromeda.sandbox.rapidsos.com
$env:RAPIDSOS_PORTAL_TOKEN='eyJhbGciOi...'    # api-sandbox.rapidsosportal.com
```

DevTools → click a request → **Headers** → right-click the `authorization`
value → **Copy value**. Single quotes in PowerShell; double quotes mangle it.

Pasted tokens expire in a few hours. Every run prints how long the token has
left.

### Three rules

**Nothing is written without `--apply`.** Every stage is a dry run by default
and prints what it would do.

**`--authority-id` takes a name or a number.** `"Authority123Sandbox"` is looked up.
Duplicate names are refused rather than guessed at.

**Never create an Authority named with space.** For example `"Authrority 123 Sandbox"` will
result in an error. The vaild name for this example would be `"Authority123Sandbox"`. 

---

## Creating an account from scratch

```powershell
# 1. sign up (runbook step 1) -- no token needed
python smoke_test.py --stage signup `
  --email "you+authority@rapidsos.com" `
  --agency-name "Authority123Sandbox" `
  --first-name Ada --last-name Lovelace --apply

# 2. confirm the address -- click the emailed link, or:
python smoke_test.py --stage confirm --confirm-token "<the whole link>" --apply

# 3. account details (runbook step 2)
python smoke_test.py --authority-id "Authority123Sandbox" --stage account-info --account-id SBX_31109 --country USA --state NE --apply

# 4. boundary, revision, integration, capabilities (steps 4, 7, 5, 6)
python smoke_test.py --authority-id "Authority123Sandbox" --stage provision --geojson lancaster --apply

# 5. portal data sources (step 8)
python smoke_test.py --authority-id "Authority123Sandbox" --stage roles --portal-email "you+authority@rapidsos.com" --apply
```

Then check in the browser: the jurisdiction reads **Active**, the capabilities
are set on the integration, and the **Alerts tab appears**. The tool confirms
the API accepted everything; only the UI confirms the account works.

Steps 3 and 4 can be replaced by a single `--stage sandbox --place ...` when
the boundary is a US county — see *From a place name*.

---

## Commands

### Sign-up (runbook step 1)

```powershell
# dry run
python smoke_test.py --stage signup --email "you+authority@rapidsos.com" --agency-name "Authority123Sandbox" --first-name Ada --last-name Lovelace

# create it
python smoke_test.py --stage signup --email "you+lancaster@rapidsos.com" --agency-name "Authority123Sandbox" --first-name Ada --last-name Lovelace --apply

# confirm the address
python smoke_test.py --stage confirm --confirm-token "<link or token>" --apply
```

Notes:

- **The agency name becomes the authority name** in Andromeda. Choose it as
  you want to find it later.
- Every generated account gets the password `AutomatedAccount123!`, so anyone
  on the team can log into one. `--password` overrides.
- **Addresses cannot be reused.** A second account needs a different `+tag`.
- The PSAP call answers **500 but the data lands** — confirmed against a real
  signup. The tool tolerates it and says so. A 4xx still fails, because that
  means the payload was wrong.
- `--confirm-token` accepts the raw token or the whole URL from the email.
- Contact title, phone, population and the three "Other" system fields are
  filled with the runbook's fixed values automatically.

### Account Info (runbook step 2)

```powershell
# what codes exist
python smoke_test.py --authority-id "Authority123Sandbox" --stage catalogs
python smoke_test.py --authority-id "Authority123Sandbox" --stage catalogs --country GBR

# read the current values
python smoke_test.py --authority-id "Authority123Sandbox" --stage account-info

# set them
python smoke_test.py --authority-id "Authority123Sandbox" --stage account-info --account-id SBX_0001 --country GBR --state WMD --apply
```

`--dispatch-type` defaults to `1` (Primary), which every sandbox account needs,
so it is set on every account-info and sandbox run without being asked for.

`account_id` can only be set **once**. If it already holds a value the tool
warns, skips that field, and applies the rest.

### From a place name (US counties only)

Resolves a town, city or county to its Census county boundary and derives the
account fields from it. Needs `ecc_lookup.py` and geopandas.

```powershell
# resolve only -- no writes
python smoke_test.py --authority-id "Authority123Sandbox" --stage place --place "Lancaster County, NE"
python smoke_test.py --authority-id "Authority123Sandbox" --stage place --place 31109

# account info + boundary + revision + integration + capabilities
python smoke_test.py --authority-id "Authority123Sandbox" --stage sandbox --place "Lancaster County, NE" --apply

# name the authority after the registered ECC
python smoke_test.py --authority-id "Authority123Sandbox" --stage sandbox --place 31109 --use-ecc-name --apply

# proceed even when the FCC registry lists no ECCs for the county
python smoke_test.py --authority-id "Authority123Sandbox" --stage sandbox --place 31109 --allow-unverified-scope --apply
```

A query matching both a county and a city in **different** counties is
refused: "Lincoln, NE" is both Lincoln County and the city of Lincoln, which
sits in Lancaster County. Name the county, or pass the 5-digit GEOID.

### Everything after the account exists (steps 4, 7, 5, 6)

```powershell
# dry run
python smoke_test.py --authority-id "Authority123Sandbox" --stage provision --geojson my-boundary

# boundary → revision → integration → capabilities
python smoke_test.py --authority-id "Authority123Sandbox" --stage provision --geojson my-boundary --apply

# a different capability set
python smoke_test.py --authority-id "Authority123Sandbox" --stage provision --geojson my-boundary --standard src/logic/data/other.json --apply

# the boundary is already live
python smoke_test.py --authority-id "Authority123Sandbox" --stage provision --skip-jurisdiction --apply

# fail rather than continue if the jurisdiction never reaches Active
python smoke_test.py --authority-id "Authority123Sandbox" --stage provision --geojson my-boundary --require-active --apply
```

### Jurisdiction boundary (runbook steps 4 and 7)

```powershell
# list the boundary files available
python smoke_test.py --authority-id "Authority123Sandbox" --stage jurisdiction

# dry run
python smoke_test.py --authority-id "Authority123Sandbox" --stage jurisdiction --geojson shapefile

# create AND publish the revision that activates it
python smoke_test.py --authority-id "Authority123Sandbox" --stage jurisdiction --geojson shapefile --apply

# create only, publish later
python smoke_test.py --authority-id "Authority123Sandbox" --stage jurisdiction --geojson my-boundary --apply --no-activate

# a file from outside the data folder
python smoke_test.py --authority-id "Authority123Sandbox" --stage jurisdiction --geojson C:\path\to\boundary.geojson --apply
```

Check the printed bbox before applying. It is the one line that catches a
boundary in the wrong part of the world.

### Copying a boundary from another account

```powershell
# writes src/logic/data/authority-<id>.geojson
python smoke_test.py --authority-id "SomeOtherAccount" --stage export-boundary

# choose the filename
python smoke_test.py --authority-id "SomeOtherAccount" --stage export-boundary --out src/logic/data/shapefile.geojson

# when the authority has more than one jurisdiction
python smoke_test.py --authority-id "SomeOtherAccount" --stage export-boundary --jurisdiction-id 3799
```

Read-only, and the file round-trips without editing. Boundaries carry no
environment-specific names, so a production boundary transfers to sandbox
unchanged — add `--base-url` and a token for that environment.

### Publishing a revision (runbook step 7)

```powershell
# what is waiting to be published -- read-only
python smoke_test.py --authority-id "Authority123Sandbox" --stage pending

# dry run, then publish
python smoke_test.py --authority-id "Authority123Sandbox" --stage activate
python smoke_test.py --authority-id "Authority123Sandbox" --stage activate --apply

# pick the revision number yourself
python smoke_test.py --authority-id "Authority123Sandbox" --stage activate --revision-number 2409 --revision-date 2026-09-24 --apply

# publish even though someone else's changes are in the batch
python smoke_test.py --authority-id "Authority123Sandbox" --stage activate --allow-other-authorities --apply
```

**Revisions are environment-wide.** The pending revision batches every
authority's jurisdiction changes, and publishing activates all of them. The
tool refuses when the batch holds another authority — see *Working as a team*.

Revision numbers must be unique. The default is day + month; on a collision
the tool retries with a suffix (2409 → 240901) rather than failing.

### Integrations (runbook step 5)

```powershell
python smoke_test.py --authority-id "Authority123Sandbox" --stage create
python smoke_test.py --authority-id "Authority123Sandbox" --stage create --apply

# override the name or product
python smoke_test.py --authority-id "Authority123Sandbox" --stage create --app-name "Authority123 Demo" --apply
python smoke_test.py --authority-id "Authority123Sandbox" --stage create --product "RapidSOS Portal" --apply

# reuse an existing integration of the same name instead of failing
python smoke_test.py --authority-id "Authority123Sandbox" --stage create --if-exists reuse --apply
```

`consumer_secret` is printed once, at creation, and never again. Store it if
anything downstream needs it.

### Capabilities (runbook step 6)

These need `--integration-id`.

```powershell
# read the current state and snapshot it
python smoke_test.py --authority-id "Authority123Sandbox" --integration-id 13909 --stage read

# what would change
python smoke_test.py --authority-id "Authority123Sandbox" --integration-id 13909 --stage plan

# apply
python smoke_test.py --authority-id "Authority123Sandbox" --integration-id 13909 --stage apply --apply

# a different capability file
python smoke_test.py --authority-id "Authority123Sandbox" --integration-id 13909 --standard src/logic/data/other.json --stage apply --apply

# does it still match?
python smoke_test.py --authority-id "Authority123Sandbox" --integration-id 13909 --stage verify

# undo
python smoke_test.py --authority-id "Authority123Sandbox" --integration-id 13909 --restore snapshots\13909-20260924-162432-before.json --apply
```

Alerts capabilities cannot be enabled where the jurisdiction overlaps one that
already has them; Andromeda answers 500. The tool retries without them and
reports which were skipped.

Capability names differ between environments (`lyft` vs `lyft_sandbox`), so a
production capability file does **not** apply cleanly to a sandbox
integration.

### Role and Access (runbook step 8)

Runs against the **portal** API, so it needs `--portal-email` (logs in as the
account) or `RAPIDSOS_PORTAL_TOKEN`.

```powershell
# log in as the account instead of pasting a token
python smoke_test.py --authority-id "Authority123Sandbox" --stage roles --portal-email "you+authority@rapidsos.com" --apply

# dry run -- also writes a snapshot
python smoke_test.py --authority-id "Authority123Sandbox" --stage roles

# grant every data source to Admin and Agent
python smoke_test.py --authority-id "Authority123Sandbox" --stage roles --apply

# undo
python smoke_test.py --authority-id "Authority123Sandbox" --stage roles --restore-roles snapshots\roles-15516-...-before.json --apply

# a different organization
python smoke_test.py --authority-id "Authority123Sandbox" --stage roles --organization-id 15516 --apply
```

Admin gets every permission; Agent gets every permission except the
administrative ones. Derived from the catalog, so new permissions are picked
up automatically.

Without this step the capabilities exist in Andromeda but the data sources are
off in the portal, so the Alerts tab stays empty.

If the dry run reports **revocations**, stop. A role holding something outside
the target set means an assumption is wrong.

## Other notes
**The web page and the CLI share the sign-in.** Both use
`.andromeda-session.json` and `.andromeda-browser/`, so signing in through
either works for both. The profile is for one Google account: to sign in as
someone else, delete `.andromeda-browser/` and `.andromeda-session.json`.
See [README-web.md](README-web.md) for the page.

---

## When something goes wrong

**401** — the token expired. Run `--stage session --sign-in` again, or paste
a fresh one. The tool prints how long a token has left before every run.

**400 on register, `{"password": ["Invalid value."]}`** — the API enforces
undocumented complexity rules. The built-in password satisfies them; only a
custom `--password` can hit this.

**400 on register, address in use** — addresses cannot be reused. Use a
different `+tag`.

**500 on the PSAP call** — expected. The data lands anyway; verify the
authority in Andromeda.

**500 on capabilities** — alerts against an overlapping jurisdiction.
Expected; the tool retries without them.

**`Cannot update Account ID once set`** — handled. The tool skips that field
and applies the rest.

**`ModuleNotFoundError: ecc_lookup`** — put `ecc_lookup.py` at the project
root and run from there. Only `--place` needs it.

**`ModuleNotFoundError`** for anything else — run
`python -m pip install -r requirements.txt`.

**`AmbiguousPlaceError`** — the query matches more than one place. Use the
county name or the GEOID.

**`PartialProvisionError`** — a step failed after earlier ones succeeded. The
message lists what completed so a retry can skip it, usually with
`--skip-jurisdiction`.

**Anything unexpected** — every run writes a snapshot to `snapshots/`, and
both `--restore` and `--restore-roles` put things back.

---

## What is not automated

**Clicking the confirmation link.** The token is a server-signed JWT that only
exists in the email, so it cannot be generated. Paste the link into
`--stage confirm` or click it — either way a human with inbox access is
needed.

**Deleting accounts.** There is no teardown path. Generated accounts
accumulate.

**Unattended running.** Andromeda signs in through Google, and the stored
session lasts about a day, so someone has to sign in each day. A service
account would be needed to run this on a schedule.
