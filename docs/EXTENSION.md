# RC Copilot — ServiceNow extension over Open OnDemand

Draft a reply to a ServiceNow ticket using a private LLM running in your own
Slurm allocation, without a tunnel and without the ticket leaving Northeastern.

One click on the ticket page reads the ticket, sends it to a model running in
your job, and writes the draft into **work notes** for you to review.

```
ServiceNow tab                  Chrome                       Northeastern
┌────────────────┐  message   ┌──────────────┐   https     ┌──────────┐  proxy  ┌────────────────────┐
│  content.js    │───────────>│ background.js│────────────>│ OOD      │────────>│ Slurm job          │
│  [Draft reply] │<───────────│  session     │<────────────│ /rnode/  │<────────│  rc-copilot :PORT  │
└────────────────┘   draft    └──────────────┘             └──────────┘         │  vLLM   127.0.0.1  │
                                                                                └────────────────────┘
```

Nothing is ever saved to the ticket automatically. The draft lands in the field
unsaved; a human reviews it and clicks Update.

---

## Layout

```
extension/              Chrome MV3 extension
  manifest.json           popup + service worker + content scripts
  background.js           the only place that fetches; owns sessions
  content.js              reads the ticket, writes the draft
  content.css             id-scoped styles (ServiceNow's CSS is broad)
  ood-connect.js          auto-configures from the OOD session card
  popup.html/js/css       status, target field, manual fallback
  check.py                static checks -- run before packaging
  icons/

ood-apps/               Open OnDemand interactive apps
  rc-copilot/             the real thing: vLLM + backend in one GPU job
  capture-test/           no model; stores what is sent, echoes it back

cluster/                Slurm launchers and helpers
  serve_stack.sbatch      vLLM + backend, one job (sbatch route)
  capture.sbatch          capture server, CPU only
  capture_server.py       same API as the backend, stores instead of drafting
```

The backend itself is [`../backend`](../backend) — this directory adds the
browser half and the cluster launcher.

---

## Setup

### 1. Confirm the OOD path works (5 minutes, no GPU)

Do this before involving a model. It isolates the proxy from everything else.
`capture.sbatch` runs on the CPU `short` partition, so it schedules immediately,
and it speaks the real API — same `/api/health`, `/api/login`, `/api/draft`,
same auth module — so a pass here means the extension needs no changes.

```bash
cd cluster
sbatch capture.sbatch
tail -f logs/rc-capture-<jobid>.out
```

Copy the printed URL into a browser tab while signed into OOD. JSON means the
whole path works. If this fails, nothing below will work either.

### 2. Start the real stack

```bash
sbatch serve_stack.sbatch
tail -f logs/rc-copilot-<jobid>.out
```

It starts vLLM on loopback, waits for the weights to load (minutes — it polls
rather than guessing), starts rc-copilot on `0.0.0.0`, and prints the URL.

The access token is **not** printed. Read it from your home directory:

```bash
cat ~/.rc_copilot/token
```

### 3. Load the extension

`chrome://extensions` → Developer mode → **Load unpacked** → select
`extension/`. It's a folder picker, so `manifest.json` appears greyed out —
that's correct; select the folder.

### 4. Configure

Click the icon. Paste the URL and the token, leave the target as **Work notes**,
hit **Save**, then **Test Connection**. That checks two separate things: that
OOD reaches the backend, and that the token is accepted.

### 5. Use it

Open a ticket. A red **Draft reply** button appears bottom-right. Click it —
the panel shows a confidence badge, the caveats, and the draft, and the draft is
written into work notes unsaved.

---

## Security model

`rc-copilot` binds `0.0.0.0` because the OOD web node must reach it across the
network. **That also means every other user on the cluster can reach that
port.** Three things follow, and they're why the pieces are shaped this way:

- **An access token is required**, minted per job. `/api/draft` returns 401
  without it. It's exchanged for an HMAC-signed session token that the server
  refuses once expired — the deadline is enforced server-side, not by the
  browser.
- **The token never touches `/projects`**, which is group-readable. It's written
  to `~/.rc_copilot/token` at mode 600, passed to the app through the
  environment, and the startup banner deliberately does not echo it, because
  Slurm job logs land on the shared filesystem.
- **vLLM binds loopback only.** Only rc-copilot needs it, so nothing else on the
  cluster can reach the model directly.

Sessions default to 3 hours (`SESSION_HOURS`). The extension holds the session
in `chrome.storage.session`, which is memory-only and cleared when the browser
closes, and re-signs in automatically on a 401.

**Both the URL and the token change every time the job is resubmitted.**

---

## The failure you'll hit first

OOD sits behind university SSO. The extension sends `credentials: "include"` so
it reuses your browser's OOD session — but when that session has expired, OOD
answers **HTTP 200 with an HTML login page**, not an error. Code that assumes
JSON reports a parse failure and sends you hunting in the wrong place.

`background.js` checks the content type and says exactly that: *"Got the OOD
login page instead of JSON."*

**Keep a tab signed into OOD.**

---

## Two ServiceNow details this depends on

Northeastern's instance is at **`service.northeastern.edu`** — a custom
domain, not `*.service-now.com`. The content script matches both, because a
manifest matching only `service-now.com` never activates there at all.

**Classic renders the form in an iframe** (`#gsft_main`), so the content script
declares `all_frames: true` and only draws its button in a frame that actually
contains ticket fields.

**Next Experience hides fields in shadow DOM**, where `querySelector` can't
reach them. Fields are found with a shadow-piercing walk and matched on id,
name, `aria-label`, placeholder and label text rather than on selectors that
only work in one UI.

**Assigning `.value` is not enough.** ServiceNow's client scripts and its
unsaved-changes tracker only react to events, so every write goes through the
native value setter and then dispatches `input` and `change`.

---

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| "Got the OOD login page" | Sign in to OOD in a tab |
| 404 | Job ended, or stale node/port. Resubmit, repaste both values |
| 502 / 503 / 504 | Job alive, rc-copilot not listening. Check the job log |
| "Access token rejected" | Job restarted and minted a new token. Re-read it |
| "can't reach vLLM" | Backend is up, model isn't. Check `logs/vllm-<jobid>.out` |
| Button never appears | Not on a ticket form, or the UI isn't recognised |
| Panel says no fields found | It names the UI *and* the URL path — send both; selectors need widening |
| "Extension was reloaded" | Refresh the ServiceNow tab |

Isolate the layer by skipping the browser entirely, from a login node:

```bash
curl --noproxy '*' http://<node>:<port>/api/health
```

`--noproxy` matters — these nodes set `http_proxy`, which 403s internal traffic.

---

## What was verified, and what wasn't

Tested on the cluster filesystem against a stub model:

- Auth: 401 without a session; wrong token rejected; correct token issues a
  session; draft succeeds with it. A **tampered signature and a forged
  far-future expiry are both rejected** — the expiry is signed, not trusted.
  Expiry confirmed with a sub-second session.
- The startup banner does not leak the token (grepped the log: 0 hits).
- **The whole contract through a simulated OOD proxy** that strips
  `/rnode/<node>/<port>` exactly as OOD does: health, 401 enforcement, login,
  draft, and the static frontend all work under the prefix.
- Field matching against 11 realistic ServiceNow signatures from both UIs,
  including the case that matters most — "description" must not swallow
  "short description".
- All JS parses; popup ids resolve; every message type the content script and
  popup send is handled by the service worker; manifest references all exist.
- `bash -n` on the sbatch scripts; `capture_server.py` compiles and serves JSON
  on any path, with CORS and a 204 preflight, bound on `0.0.0.0`.

**Not verified — no Chrome and no live OOD here:**

- The extension has never been loaded into a browser.
- The real OOD route. `/rnode/` is standard and strips the prefix, which is what
  the simulation reproduced, but if your portal only enables `/node/` the banner
  prints that URL too. If neither works, OOD's node proxy may be disabled at
  your site — that's a portal config question, not a code one.
- Selectors against a real ServiceNow instance. The panel names the UI it
  detected, which is the thing to report if fields aren't found.
- `serve_stack.sbatch` end to end, which needs a GPU allocation and the cu129
  vLLM wheel working.
