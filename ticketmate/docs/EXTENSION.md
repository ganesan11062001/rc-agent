# The extension and the OOD apps

How the browser half of RC Copilot works, how it finds and talks to a session,
and how to change, test and ship it. For using it, see
[ood-apps/MEMBERS.md](../ood-apps/MEMBERS.md); for the whole system,
[ARCHITECTURE.md](../ARCHITECTURE.md).

```
ServiceNow tab                  Chrome                    Northeastern
┌────────────────┐  message   ┌──────────────┐   https  ┌──────────┐  proxy  ┌──────────────────────────┐
│ content.js     │───────────>│ background.js│─────────>│ OOD      │────────>│ Slurm job                │
│ panel, fields  │<───────────│ session,fetch│<─────────│ /rnode/  │<────────│ rc-copilot :PORT         │
└────────────────┘   draft    └──────────────┘          └──────────┘         │ vLLM, rc-kg on 127.0.0.1 │
        ▲                                                                    └──────────────────────────┘
 ood-connect.js on OOD's "My Interactive Sessions" saves the session's URL and token
```

Nothing is ever saved to the ticket automatically. The draft lands in the
field unsaved; a person reviews it and clicks Update.

---

## Layout

```
extension/              Chrome MV3 extension
  manifest.json           service worker, content scripts, popup; version
  content.js / .css       the panel on ServiceNow: read the ticket, compose, show, write
  background.js           the only place that fetches; owns the session and error wording
  ood-connect.js          on OOD: saves the session's URL + token from its card
  popup.html/js/css       connection status, target field, manual configuration
  check.py                static checks (run by package.sh)
  package.sh              check + build dist/rc-copilot-extension.zip
  icons/

ood-apps/               Open OnDemand interactive apps
  rc-copilot/             the real thing: rc-kg + backend + vLLM in one GPU job
  capture-test/           no model; stores what is sent and echoes it back
  publish.sh              release both the app and the extension zip to RC members
  install.sh              (copied into the release) links a member's sandbox to it
  MEMBERS.md              the getting-started guide (published as the release README)

cluster/
  serve_stack.sbatch      the same stack as a plain batch job (no OOD)
  capture.sbatch, capture_server.py   the capture test as a batch job
```

---

## How the extension finds a session

**Through OOD (the normal way).** The OOD app's session card
(`ood-apps/rc-copilot/view.html.erb`) carries the backend URL through OOD's
node proxy (`/rnode/<host>/<port>/`) and the session's access token in `data-`
attributes. When the member opens *My Interactive Sessions*, `ood-connect.js`
reads the first RC Copilot card on the page and saves both (with two sessions
running, delete the one you are not using). Only the job's owner sees that
card. Nothing to copy.

**By hand (`serve_stack.sbatch`).** The batch job prints the URL and writes the
token to `~/.rc_copilot/token` (mode 600, never in the job log). Paste both
into the toolbar popup and click **Save**, then **Test connection**.

Either way the extension then signs in (`POST /api/login`) and keeps the
signed session in `chrome.storage.session` (memory only; cleared when Chrome
closes); it signs in again on a 401.

---

## The panel

- **Opening:** the red **Draft reply** button bottom-right on any page with
  ticket fields. Width: ⅓ / ½ / ⅔ in the header, or drag the left edge
  (double-click: half); remembered in `chrome.storage.local`.
- **Status strip:** connected / starting (with the model's loading stage and
  the knowledge graph's checklist from `/api/health`) / waiting for the session /
  not connected, with the reason.
- **Compose:** what was read from the page; instructions with quick chips;
  **Write the draft into** (work notes / additional comments / both — the same
  setting as the popup); **Draft reply** or Ctrl+Enter.
- **Result:** always shown — where the draft went, confidence, *Check before
  sending*, the editable draft with **Copy** / **Insert**. Collapsible
  (remembered per tab): steps, cluster lookups, RC pages cited, quality checks,
  ticket & model details, *Rate this draft*.

---

## Failure modes, and what the extension says

| Situation | What the member sees | Why it is told apart |
| --- | --- | --- |
| New session, backend not listening yet | *Waiting for the session…*, retrying every 3 s for 5 min | OOD's "Failed to connect to <node>:<port>" page is HTML, like a login page; telling people to sign in sent them the wrong way |
| Still nothing after 5 min | *Nothing is answering at <node>:<port> …* | The job ended or failed; check `output.log` |
| OOD sign-in expired | *Got the OOD login page instead of JSON …* | OOD answers 200 + HTML, not 401 |
| Job ended / old address | OOD 404 → *launch a new session …* | |
| Token from an old session | *rejected the access token …* | Each session has its own token |
| Model still loading | progress display, not an error | `/api/health` is always 200; the body says |
| No ticket fields | *No ticket fields on this page* + the UI it detected | Report both if fields are really there |
| Extension reloaded | *refresh this page* | The old content script lost its worker |

From a shell, the backend can be checked without the browser:
`curl --noproxy '*' http://<node>:<port>/api/health` (`--noproxy` matters:
these nodes set `http_proxy`, which 403s internal traffic).

---

## ServiceNow details this depends on

- Northeastern's instance is **`service.northeastern.edu`**, a custom domain;
  the content script matches it and `*.service-now.com`.
- **Classic** renders the form in an iframe (`#gsft_main`): the content script
  runs in all frames and draws its button only where ticket fields exist.
- **Next Experience** hides fields in shadow DOM: fields are found by a
  shadow-piercing walk, matched on id, name, `aria-label`, placeholder and label
  text, not on UI-specific selectors.
- **Assigning `.value` is not enough:** writes go through the native value
  setter and dispatch `input` and `change`, or ServiceNow's scripts and
  unsaved-changes tracker never notice.
- `description` must never match `short description`; `check.py` and the
  matcher both guard it.
- **Catalog requests** (multi-GPU, long partition, access, classroom — `TASK`,
  `SCTASK`, `RITM`, `REQ`) carry their substance in the request form's answers,
  not the description. `readRequestForm()` reads the variable editor (Classic
  `#variable_map` / `.sc_variable_editor`, Service Portal `sp-variable-editor`,
  Next Experience catalog-variable editors, or a read-only "Variables" section
  as text): one `question: answer` per field — a drop-down's selected text, a
  checkbox as yes/no, a reference field's display value (never its hidden
  sys_id), a radio group's chosen option. The answers are sent under
  `Request form:`; the backend searches them as the researcher's own words and
  playbooks match on them. The panel's "read from page" list says
  `request form (N answers)`, or `no request form found` on a catalog ticket.
  `tests/ui/extension_form_test.py` covers the four page shapes.

---

## Changing, testing and shipping

1. Edit the files in `extension/`. Load the folder itself with *Load unpacked*
   while developing; click reload on `chrome://extensions` after each change.
2. Test:

   ```bash
   backend/.venv/bin/python extension/check.py      # static checks: JS parses, ids, messages, CSS classes
   tests/ui/run.sh                                  # headless Chromium: web page + the panel's layout and controls
   ```

   `tests/ui/extension_startup_test.py` replays a recorded startup through the
   panel (record one with `RC_SLOW_START_TIMELINE=... tests/startup/run_slow_start.sh`).
3. Bump `"version"` in `manifest.json`.
4. Package: `extension/package.sh` → `dist/rc-copilot-extension.zip`.
5. Commit, then `ood-apps/publish.sh` to release it (and the OOD app) to RC
   members. Tell them to re-download the zip and reload — the extension does not
   update itself.

### The OOD apps

`ood-apps/rc-copilot/` is the interactive app: `form.yml` (model, GPUs,
partition, hours), `submit.yml.erb` (turns the form into Slurm options and
enforces partition limits), `template/before.sh.erb` (ports, password),
`template/script.sh.erb` (starts rc-kg, the backend, vLLM — see
[ARCHITECTURE.md §4.3](../ARCHITECTURE.md#43-the-ood-app-and-the-session)) and
`view.html.erb` (the session card the extension reads). Template changes reach
members only through `ood-apps/publish.sh`; `publish.sh --list` shows releases,
`--rollback` steps back one. `ood-apps/capture-test/` is the same app with no
model, to test the data path on a CPU in minutes.
