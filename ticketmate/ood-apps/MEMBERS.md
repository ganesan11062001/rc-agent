# RC Copilot — getting started

RC Copilot drafts replies to ServiceNow tickets with a private AI model that
runs in your own Slurm job. Drafts are grounded in rc.northeastern.edu and
rc-docs, with links to the exact pages, and checked against live cluster facts
(accounts, jobs, nodes). **You review every draft; nothing is sent or saved
for you.**

Setup takes about 10 minutes, once. After that, each day is: launch a session,
draft, review.

---

## Before you start

You need:

- an Explorer account in the RC group (you can read
  `/projects/rc/projects/ticketmate`);
- Open OnDemand with the **Develop** menu (sandbox apps) — if you don't see
  *Develop* in OOD's top bar, ask the OOD admins to enable app development for
  your account;
- Google Chrome, on the computer you use for ServiceNow.

---

## Step 1 — Link the app into your OOD account (once, 1 minute)

Open a terminal on Explorer (OOD → *Clusters* → *Explorer Shell Access*, or
`ssh` to a login node) and run:

```bash
/projects/rc/projects/rc-agent/rc-copilot/install.sh
```

This creates one symlink, `~/ondemand/dev/rc-agent`, pointing at the shared
app. Nothing is copied, so there is nothing to keep up to date: every new
session uses the current version. If you already had an `rc-copilot` sandbox
app, it is moved aside to `rc-copilot.backup-<date>`, not deleted.

Check it:

```bash
ls -l ~/ondemand/dev/rc-agent
# rc-copilot -> /projects/rc/projects/rc-agent/rc-copilot/current/rc-copilot
```

## Step 2 — Install the browser extension (once, 3 minutes)

1. **Get the zip onto your computer.** It is
   `/projects/rc/projects/rc-agent/rc-copilot/rc-copilot-extension.zip`.
   Easiest: OOD → *Files* → *Home Directory*, type
   `/projects/rc/projects/rc-agent/rc-copilot` into the path bar, select the
   zip, **Download**. (Or `scp` it from a login node.)
2. **Unzip it** into a folder you will keep, e.g.
   `Documents/rc-copilot-extension`. Chrome loads it from there, so don't
   delete the folder.
3. In Chrome, open `chrome://extensions`, turn on **Developer mode** (top
   right), click **Load unpacked**, and choose the unzipped folder.
4. Optional: click the puzzle-piece icon in Chrome's toolbar and pin
   **RC Copilot**.

You should see *RC Copilot* with a version number (e.g. 0.9.7) on
`chrome://extensions`.

## Step 3 — Launch a session (each time, ~3 minutes)

1. In Open OnDemand: **Develop → My Sandbox Apps (Development) → RC Copilot →
   Launch**.
2. The defaults are right for most days: *Qwen2.5-7B AWQ* on *any free GPU*,
   1 hour, `gpu-interactive`. For stronger drafts choose **GLM-4.7-Flash** with
   an **A100 80 GB or H200** GPU (it may queue longer). Sessions on
   `gpu-interactive` can last up to 2 hours.
3. Click **Launch** and wait for the card to say **Running**.

## Step 4 — Connect the extension (each session, automatic)

Open **My Interactive Sessions** in OOD once the session is Running. The
extension reads the session's address and access token from its card. There is
nothing to copy or paste.

## Step 5 — Draft a reply

1. Open a ticket in ServiceNow (the ticket form itself, not a list).
2. Click the red **Draft reply** button in the bottom-right corner of the page
   to open the RC Copilot panel. It shows *Connected* — or, for a new session, the model and the knowledge
   graph loading; you can draft as soon as the model is ready.
3. Optional: type **instructions** for this reply, or click a quick one:
   *Follow up / close*, *Ask for missing details*, *Shorter*,
   *Step-by-step*, *Mark resolved*.
4. Check **Write the draft into**: *Work notes* (internal, the default),
   *Additional comments* (the researcher sees it) or *Both*.
5. Click **Draft reply** (or Ctrl+Enter). A draft takes 5–30 seconds.

The panel is half the page; use **⅓ / ½ / ⅔** in its header or drag its left
edge to resize it (double-click the edge for half again). It remembers your
choice.

## Step 6 — Review, edit, send

The draft is written into the field you chose, **unsaved**. In the panel:

- **Check before sending** — what the model could not confirm. Read it first.
- **Draft reply** — edit it here and click **Insert**, or edit it in
  ServiceNow directly. **Copy** puts it on the clipboard.
- One click away: *What's going on & steps for you*, *Cluster lookups* (the
  read-only commands it ran, e.g. `sacct -j …`, and what they found), *RC pages
  cited* (links), *Quality checks*, *Rate this draft*.

When it is right, click **Update** in ServiceNow yourself. Rating drafts
(*Rate this draft*) is how RC Copilot gets better.

---

## Keeping it up to date

- **The app** updates itself: every new session uses the current release.
  `cat /projects/rc/projects/rc-agent/rc-copilot/current/VERSION` shows it.
- **The extension** does not. When a new version is announced, download the
  zip again (Step 2.1), unzip it **over the same folder**, and click the reload
  icon on RC Copilot in `chrome://extensions`.

---

## If something goes wrong

| You see | What it means / what to do |
| --- | --- |
| *Waiting for the session…* | Normal for the first minute or two of a new session; it retries by itself. |
| *Nothing is answering at d4054:24980* | After five minutes of waiting: the session ended or failed. Open **output.log** from the session card, then launch a new session. |
| *Got the OOD login page instead of JSON* | Your OOD sign-in expired. Open OOD in a tab, sign in, click Retry. |
| *Not connected* / no session found | Open **My Interactive Sessions** again with the session Running. |
| Knowledge graph *Failed* | Drafts still work, without RC page links. Send the maintainer the session's `rc_kg.log` (same folder as `output.log`). |
| *No ticket fields on this page* | Open the ticket form itself, then reload the page. |
| The draft went into the wrong field | Change **Write the draft into** and draft again; nothing was saved. |
| *Developer mode* is greyed out | Your Chrome profile is managed; ask IT, or use a profile that allows unpacked extensions. |
| No *Develop* menu in OOD | Ask the OOD admins to enable app development for your account. |

Each session's logs are in
`~/ondemand/data/sys/dashboard/batch_connect/dev/rc-copilot/output/<session-id>/`
(`output.log`, `vllm.log`, `rc_kg.log`).

---

## What is private, and to whom

| | where it lives | who can read it |
|---|---|---|
| ticket text, prompts, drafts | your Slurm job; logs in `<repo>/traces/$USER/`, or `/scratch/$USER/rc_copilot_traces` if you cannot write the repository (mode 700) | you |
| session access token | the OOD session card (owner only) | you |
| model caches | `/scratch/$USER/rc_copilot_cache` | you |
| the app, models, knowledge graph | `/projects/rc/projects/ticketmate` | RC team, read-only |

Nothing leaves the cluster: the model, the knowledge-graph search and the
cluster lookups all run inside your job. The lookups are read-only — nothing
RC Copilot runs can change, update or delete anything.

---

## Uninstall

```bash
/projects/rc/projects/rc-agent/rc-copilot/install.sh --uninstall
```

removes the symlink; remove the extension on `chrome://extensions`.
