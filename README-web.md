# Sandbox Account Page

A web page that creates a RapidSOS Unite sandbox account. You fill in one form,
confirm your email, and it does the rest: account details, jurisdiction
boundary, revision, integration, capabilities, and portal Role & Access.

It runs on your own computer. Nothing is hosted, and nobody else can reach it.

For the command-line version, see [README.md](README.md).

---

## One-time setup

Someone comfortable with a terminal does this once per computer.

1. Install **Python 3.9 or newer** from python.org. On Windows, tick
   **Add Python to PATH** in the installer.
2. From the project folder, run:

   ```powershell
   python -m pip install -r requirements.txt
   python -m playwright install chromium
   python -m pytest tests -q        # expect: all passed
   ```

You don't need to paste a token. The page signs in to Andromeda for you (see
below), and it logs in to the portal as the new account itself.

---

## Starting it

Double-click **`start.bat`** on Windows, or **`start.sh`** on Mac/Linux.

A terminal window opens, and then your browser opens at
`http://127.0.0.1:8765/`. **Leave the terminal window open** while you use the
page. Closing it stops the app.

If the window says *"Some pieces are not installed yet"*, run the command it
prints, or redo the one-time setup.

If port 8765 is already in use, set `SANDBOX_UI_PORT` to another number before
starting.

---

## Using the page

### 1. Sign in

Press **Sign in to Andromeda**. The first time, a browser window opens: sign
in with your RapidSOS Google account, then come back to the page. After that,
signing in finishes on its own in a few seconds. A sign-in lasts about a day.

You only need to sign in before the later steps. Creating the account itself
doesn't need it.

**Without Playwright**, the page offers a fallback: record a sign-in in Chrome
DevTools (Network tab, **Preserve log**), save it with **Save all as HAR with
content**, and upload the file. Delete the HAR afterwards, because it holds
your live sign-in.

### 2. The account

| Field | Notes |
|---|---|
| Email | Must never have been used before. Add a `+tag` per account, e.g. `you+authority@rapidsos.com` |
| First / last name | The contact on the account |
| Agency/Authority name | Becomes the authority name in Andromeda. Pick one you'll recognise later |
| Boundary | A US place (town, city, county or 5-digit GEOID), an uploaded `.geojson` file, or a copy of another account's boundary |
| Account ID | Optional. Filled in from the place when there is one |
| Country / state | Filled in from the place. Required otherwise |
| Capabilities | The standard sandbox set, or a copy of another account's |

When you look up a place, the page shows the matched county, its ECCs and the
bounding box before anything is created. **Check the bounding box**: it is the
quickest way to catch a boundary in the wrong part of the world. If a name
matches more than one place (for example, "Lincoln, NE" is both a county and
a city in another county), the page lists the options for you to pick from.

Press **Preview** to see what will happen. Nothing is written yet. Then press
**Create**.

### 3. Confirm your email

The page shows the address it sent the confirmation to. Open that email and
click the link. Or paste the link into the page and press **Confirm**.

### 4. Continue

Press **Continue**. The remaining six steps run without further input, and
each one shows up as it finishes:

1. Account info
2. Boundary
3. Publish revision
4. Integration
5. Capabilities
6. Roles

The whole run takes about a minute. When it's done, check the account in the
browser: the jurisdiction reads **Active** and the **Alerts tab appears**.

Every account the tool creates uses the shared sandbox password, so anyone on
the team can log into it.

---

## Messages that are not problems

The page reports these, but they don't mean anything went wrong:

- **PSAP creation returned 500.** This is expected. The data lands anyway.
- **Alerts capabilities skipped.** The boundary overlaps another account that
  already has alerts, and Andromeda won't allow both. Everything else applies.
- **Account ID skipped.** It can only be set once, and it already has a value.
  The other fields still apply.

---

## When something goes wrong

**The page refuses to publish the revision.** Publishing a revision activates
*every* authority's pending jurisdiction changes, not just yours. If someone
else has work waiting, the page names them and stops. Ask them in Slack, and
try again once their changes are published. The page can't override this.

**"Another run is already writing."** Only one run can write at a time,
because two would collide on the same revision. Wait for the other run to
finish.

**A step failed partway through.** The page lists what already exists and
what doesn't. Fix the cause, then use **Resume an existing account** (the link
at the top of the page). Resuming finds the authority by name, detects the
steps that are already done, and skips them.

**You closed the tab while checking your email.** Use **Resume an existing
account** with the agency name you entered. You don't need to start again,
and you can't anyway, because the email address can't be reused.

**Sign-in expired.** Press **Sign in to Andromeda** again.

**You want to undo capabilities or roles.** Both are snapshotted before
they're written. At the end of a run, **Undo capabilities** and **Undo roles**
show what the undo would change before asking you to confirm.

---

## Sign-in files: never share them

Signing in creates two files in the project folder:

- `.andromeda-session.json`: your Andromeda sign-in
- `.andromeda-browser/`: the browser profile, holding your Google session

Both are **your** credentials. They are gitignored. Never commit, copy or send
them. **Sign out** forgets the Andromeda sign-in but keeps the browser
profile, so the next sign-in is the same Google account again. To sign in as
someone else, stop the app and delete both.

If `ANDROMEDA_TOKEN` is set in the environment when the app starts, the app
uses that token instead of the sign-in. It can't be renewed, so when it
expires, set a fresh one and restart.
