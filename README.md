# RapidSOS Sandbox Account Tooling

Automates the RapidSOS Unite sandbox account runbook: creates the account,
attaches a jurisdiction boundary, publishes the revision that activates it,
adds an integration, applies a standard capability set, and enables the
portal's data sources.

All eight runbook steps are covered. The only manual action left is clicking
the confirmation link in the sign-up email, which can only be done by whoever
owns the inbox.

---

## Setup

```
project/
├── smoke_test.py
├── ecc_lookup.py                  # only for --place lookups
├── andromeda/
│   ├── __init__.py
│   ├── authorities.py     capabilities.py   integrations.py
│   ├── jurisdictions.py   places.py         revisions.py
│   ├── roles.py           signup.py         workflows.py
│   └── data/
│       ├── standard_capabilities.json
│       └── *.geojson              # boundary files
└── tests/
```

```powershell
pip install requests pytest
pip install geopandas pandas shapely     # only for --place
python -m pytest tests -q                # expect: all passed
```

Run every command from the project root. Re-run the tests after copying files
in — they import every module and catch a half-updated checkout instantly.

### Tokens

Three different situations:

| Stage | Token |
|---|---|
| `signup`, `confirm` | **none** — these calls are unauthenticated |
| `roles` | `RAPIDSOS_PORTAL_TOKEN` (portal API) |
| everything else | `ANDROMEDA_TOKEN` (Andromeda) |

```powershell
$env:ANDROMEDA_TOKEN='eyJhbGciOi...'          # andromeda.sandbox.rapidsos.com
$env:RAPIDSOS_PORTAL_TOKEN='eyJhbGciOi...'    # api-sandbox.rapidsosportal.com
```

DevTools → click a request → **Headers** → right-click the `authorization`
value → **Copy value**. Single quotes in PowerShell; double quotes mangle it.

Both expire in a few hours. Every run prints how long the token has left.

### Two rules

**Nothing is written without `--apply`.** Every stage is a dry run by default
and prints what it would do.

**`--authority-id` takes a name or a number.** `"MidlandsSND"` is looked up.
Duplicate names are refused rather than guessed at.

---

## Creating an account from scratch

```powershell
# 1. sign up (runbook step 1) -- no token needed
python smoke_test.py --stage signup `
  --email "you+lancaster@rapidsos.com" `
  --agency-name "Lancaster County NE Sandbox" `
  --first-name Ada --last-name Lovelace --apply

# 2. confirm the address -- click the emailed link, or:
python smoke_test.py --stage confirm --confirm-token "<the whole link>" --apply

# 3. account details (runbook step 2)
python smoke_test.py --authority-id "Lancaster County NE Sandbox" `
  --stage account-info --account-id SBX_31109 --country USA --state NE --apply

# 4. boundary, revision, integration, capabilities (steps 4, 7, 5, 6)
python smoke_test.py --authority-id "Lancaster County NE Sandbox" `
  --stage provision --geojson lancaster --apply

# 5. portal data sources (step 8)
python smoke_test.py --authority-id "Lancaster County NE Sandbox" `
  --stage roles --apply
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
python smoke_test.py --stage signup --email "you+lancaster@rapidsos.com" --agency-name "Lancaster NE" --first-name Ada --last-name Lovelace

# create it
python smoke_test.py --stage signup --email "you+lancaster@rapidsos.com" --agency-name "Lancaster NE" --first-name Ada --last-name Lovelace --apply

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
python smoke_test.py --authority-id "MidlandsSND" --stage catalogs
python smoke_test.py --authority-id "MidlandsSND" --stage catalogs --country GBR

# read the current values
python smoke_test.py --authority-id "MidlandsSND" --stage account-info

# set them
python smoke_test.py --authority-id "MidlandsSND" --stage account-info --account-id SBX_0001 --country GBR --state WMD --apply
python smoke_test.py --authority-id "MidlandsSND" --stage account-info --dispatch-type 1 --apply
```

`account_id` can only be set **once**. If it already holds a value the tool
warns, skips that field, and applies the rest.

### From a place name (US counties only)

Resolves a town, city or county to its Census county boundary and derives the
account fields from it. Needs `ecc_lookup.py` and geopandas.

```powershell
# resolve only -- no writes
python smoke_test.py --authority-id "gDTest" --stage place --place "Lancaster County, NE"
python smoke_test.py --authority-id "gDTest" --stage place --place 31109

# account info + boundary + revision + integration + capabilities
python smoke_test.py --authority-id "gDTest" --stage sandbox --place "Lancaster County, NE" --apply

# name the authority after the registered ECC
python smoke_test.py --authority-id "gDTest" --stage sandbox --place 31109 --use-ecc-name --apply

# proceed even when the FCC registry lists no ECCs for the county
python smoke_test.py --authority-id "gDTest" --stage sandbox --place 31109 --allow-unverified-scope --apply
```

A query matching both a county and a city in **different** counties is
refused: "Lincoln, NE" is both Lincoln County and the city of Lincoln, which
sits in Lancaster County. Name the county, or pass the 5-digit GEOID.

### Everything after the account exists (steps 4, 7, 5, 6)

```powershell
# dry run
python smoke_test.py --authority-id "MidlandsSND" --stage provision --geojson my-boundary

# boundary → revision → integration → capabilities
python smoke_test.py --authority-id "MidlandsSND" --stage provision --geojson my-boundary --apply

# a different capability set
python smoke_test.py --authority-id "MidlandsSND" --stage provision --geojson my-boundary --standard andromeda/data/other.json --apply

# the boundary is already live
python smoke_test.py --authority-id "MidlandsSND" --stage provision --skip-jurisdiction --apply

# fail rather than continue if the jurisdiction never reaches Active
python smoke_test.py --authority-id "MidlandsSND" --stage provision --geojson my-boundary --require-active --apply
```

### Jurisdiction boundary (runbook steps 4 and 7)

```powershell
# list the boundary files available
python smoke_test.py --authority-id "MidlandsSND" --stage jurisdiction

# dry run
python smoke_test.py --authority-id "MidlandsSND" --stage jurisdiction --geojson shapefile_processed_GB_WMID

# create AND publish the revision that activates it
python smoke_test.py --authority-id "MidlandsSND" --stage jurisdiction --geojson shapefile_processed_GB_WMID --apply

# create only, publish later
python smoke_test.py --authority-id "MidlandsSND" --stage jurisdiction --geojson my-boundary --apply --no-activate

# a file from outside the data folder
python smoke_test.py --authority-id "MidlandsSND" --stage jurisdiction --geojson C:\path\to\boundary.geojson --apply
```

Check the printed bbox before applying. It is the one line that catches a
boundary in the wrong part of the world.

### Copying a boundary from another account

```powershell
# writes andromeda/data/authority-<id>.geojson
python smoke_test.py --authority-id "SomeOtherAccount" --stage export-boundary

# choose the filename
python smoke_test.py --authority-id "SomeOtherAccount" --stage export-boundary --out andromeda/data/west-midlands.geojson

# when the authority has more than one jurisdiction
python smoke_test.py --authority-id "SomeOtherAccount" --stage export-boundary --jurisdiction-id 3799
```

Read-only, and the file round-trips without editing. Boundaries carry no
environment-specific names, so a production boundary transfers to sandbox
unchanged — add `--base-url` and a token for that environment.

### Publishing a revision (runbook step 7)

```powershell
# what is waiting to be published -- read-only
python smoke_test.py --authority-id "MidlandsSND" --stage pending

# dry run, then publish
python smoke_test.py --authority-id "MidlandsSND" --stage activate
python smoke_test.py --authority-id "MidlandsSND" --stage activate --apply

# pick the revision number yourself
python smoke_test.py --authority-id "MidlandsSND" --stage activate --revision-number 2409 --revision-date 2026-09-24 --apply

# publish even though someone else's changes are in the batch
python smoke_test.py --authority-id "MidlandsSND" --stage activate --allow-other-authorities --apply
```

**Revisions are environment-wide.** The pending revision batches every
authority's jurisdiction changes, and publishing activates all of them. The
tool refuses when the batch holds another authority — see *Working as a team*.

Revision numbers must be unique. The default is day + month; on a collision
the tool retries with a suffix (2409 → 240901) rather than failing.

### Integrations (runbook step 5)

```powershell
python smoke_test.py --authority-id "MidlandsSND" --stage create
python smoke_test.py --authority-id "MidlandsSND" --stage create --apply

# override the name or product
python smoke_test.py --authority-id "MidlandsSND" --stage create --app-name "MidlandsSND Demo" --apply
python smoke_test.py --authority-id "MidlandsSND" --stage create --product "RapidSOS Portal" --apply

# reuse an existing integration of the same name instead of failing
python smoke_test.py --authority-id "MidlandsSND" --stage create --if-exists reuse --apply
```

`consumer_secret` is printed once, at creation, and never again. Store it if
anything downstream needs it.

### Capabilities (runbook step 6)

These need `--integration-id`.

```powershell
# read the current state and snapshot it
python smoke_test.py --authority-id "MidlandsSND" --integration-id 13909 --stage read

# what would change
python smoke_test.py --authority-id "MidlandsSND" --integration-id 13909 --stage plan

# apply
python smoke_test.py --authority-id "MidlandsSND" --integration-id 13909 --stage apply --apply

# a different capability file
python smoke_test.py --authority-id "MidlandsSND" --integration-id 13909 --standard andromeda/data/other.json --stage apply --apply

# does it still match?
python smoke_test.py --authority-id "MidlandsSND" --integration-id 13909 --stage verify

# undo
python smoke_test.py --authority-id "MidlandsSND" --integration-id 13909 --restore snapshots\13909-20260924-162432-before.json --apply
```

Alerts capabilities cannot be enabled where the jurisdiction overlaps one that
already has them; Andromeda answers 500. The tool retries without them and
reports which were skipped.

Capability names differ between environments (`lyft` vs `lyft_sandbox`), so a
production capability file does **not** apply cleanly to a sandbox
integration.

### Role and Access (runbook step 8)

Runs against the **portal** API, so it needs `RAPIDSOS_PORTAL_TOKEN`.

```powershell
# dry run -- also writes a snapshot
python smoke_test.py --authority-id "MidlandsSND" --stage roles

# grant every data source to Admin and Agent
python smoke_test.py --authority-id "MidlandsSND" --stage roles --apply

# undo
python smoke_test.py --authority-id "MidlandsSND" --stage roles --restore-roles snapshots\roles-15516-...-before.json --apply

# a different organization
python smoke_test.py --authority-id "MidlandsSND" --stage roles --organization-id 15516 --apply
```

Admin gets every permission; Agent gets every permission except the
administrative ones. Derived from the catalog, so new permissions are picked
up automatically.

Without this step the capabilities exist in Andromeda but the data sources are
off in the portal, so the Alerts tab stays empty.

If the dry run reports **revocations**, stop. A role holding something outside
the target set means an assumption is wrong.

### Other flags

```powershell
--base-url https://andromeda.rapidsos.com    # a different environment
--portal-base-url ...                        # likewise for the portal
--org "RapidSOS Admin"                       # the x-rapidsos-org header
--header "Authorization: Bearer ..."         # extra header, repeatable
--snapshot-dir backups                       # where snapshots are written
-v                                           # debug logging
```

---

## Working as a team

**Revisions are environment-wide.** Two people publishing at once will
collide, and the second is refused — correctly. Say so in Slack before running
jurisdiction steps. `--allow-other-authorities` publishes someone else's
pending work, so use it knowingly.

**Sign-up addresses cannot be reused.** Use your own alias with a `+tag` per
account.

**Tokens are personal.** Each person uses their own; nothing is shared.

**Keep `andromeda/data/` in version control.** The capability set and boundary
files are shared knowledge, and separate copies are how accounts drift apart.

**Keep these out of it.** `.gitignore` should cover:

```
har/
*.har
snapshots/
token.txt
```

HAR files contain live tokens and personal data; snapshots contain account
configuration.

---

## When something goes wrong

**401** — the token expired. Grab a fresh one. The tool prints how long a
token has left before every run.

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

**Unattended running.** Andromeda signs in through Google with a short-lived
token and no refresh token, so every session needs a token pasted by hand. A
service account would be needed to run this on a schedule.