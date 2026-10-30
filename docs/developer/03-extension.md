---
name: 3. The Chrome extension
route: /extension
menu: The pipeline
---

# 3. The Chrome extension

The extension is the only integration with ServiceNow. It reads the open
ticket from the page, sends it to the backend in the engineer's Slurm job,
and writes the draft back into a field of the form, unsaved. It never
clicks Update. `ticketmate/docs/EXTENSION.md` is the operations summary;
this chapter goes through the code.

## 3.1 Structure

Chrome Manifest V3, plain JavaScript, no build step. Everything is in
`ticketmate/extension/`.

| file | runs in | job |
|---|---|---|
| `manifest.json` | -- | declares everything below; `"version"` is the release number (0.9.8 at the time of writing) |
| `content.js`, `content.css` | every frame of a ServiceNow page | finds fields, reads the ticket, draws the button and panel, writes the draft |
| `background.js` | the service worker | **every** network call; owns the sign-in session and the error wording |
| `ood-connect.js` | Open OnDemand pages (top frame) | reads the session card and saves the backend URL and token |
| `popup.html`, `popup.js`, `popup.css` | the toolbar popup | connection status, target field, manual URL and token |
| `check.py` | Python, before packaging | static checks (3.9) |
| `package.sh` | bash | checks, then builds `dist/rc-copilot-extension.zip` |
| `icons/` | -- | 16, 32, 48, 128 px |

The manifest:

| key | value | why |
|---|---|---|
| `permissions` | `storage` only | config in `chrome.storage.local`, the session in `chrome.storage.session` |
| `host_permissions` | `https://*.northeastern.edu/*`, `https://northeastern.edu/*`, `https://*.neu.edu/*` | lets the service worker fetch OOD without CORS |
| content script 1 | `https://service.northeastern.edu/*`, `*://*.service-now.com/*`; `content.js` + `content.css`; `run_at: document_idle`; `all_frames: true` | Northeastern's instance is a custom domain; Classic puts the form in an iframe |
| content script 2 | `https://ood.explorer.northeastern.edu/*`; `ood-connect.js`; `all_frames: false` | the session card is in the top frame |
| `background.service_worker` | `background.js` | |
| `action.default_popup` | `popup.html` | |

The parts talk only through `chrome.runtime.sendMessage` and storage:

```
content.js ──PING / SIGN_IN / DRAFT / FEEDBACK / GET_CONFIG──► background.js ──fetch──► OOD /rnode/<node>/<port>/api/...
popup.js   ──PING / SIGN_IN──────────────────────────────────►      │
ood-connect.js ──SAVE_CONFIG─────────────────────────────────►      │
                                    chrome.storage.local  oodConfig = {baseUrl, accessToken, targetField}
                                    chrome.storage.session session  = {token, expiresAt}
```

## 3.2 Which UI, and which frame

ServiceNow has three UIs, and Northeastern's domain does not say which one is
showing. `detectUi()` decides from the DOM, only for the error message shown
when nothing is found:

| UI | how it is recognised | where the form is |
|---|---|---|
| Next Experience | `now-record-form`, `now-app-root` or an id starting `sn-` | web components with shadow roots; classic forms open in a `#gsft_main` iframe inside the shell's shadow DOM |
| Classic | `window.name === "gsft_main"`, or ids starting `element.` / `sys_display.` | plain DOM inside the `#gsft_main` iframe |
| Service Portal | ids starting `sp_formfield_`, `.sp-form`, `#sp-page-root`, `sp-page`, `.sp-widget` | AngularJS |

Because the script runs in every frame, exactly one frame must own the UI
(`mount()`):

1. A child frame whose top frame is same-origin never builds anything
   (`topFrameOwns()`): the top frame reaches into it (3.3).
2. Otherwise the frame that has ticket fields builds the button and panel
   (`claimUi()` checks `hasFields()`: a short description or description
   field) and sets `window.top.__rcCopilotOwned`.
3. After 2.5 s, if no frame claimed the UI, the top frame builds the button
   anyway. The code comment gives the reason: *"a missing button with no
   error is impossible to diagnose"*; the panel then says *No ticket fields
   on this page* with the detected UI and the path.

ServiceNow swaps forms without page loads. A `MutationObserver` on the whole
document, debounced 500 ms, calls `onPageChanged()` (mount again, re-read the
ticket if the panel is open). A child frame cannot be seen by the top frame's
observer, so it calls `window.top.__rcCopilotPageChanged()` instead; content
scripts of one extension share a world across same-origin frames, so that
function is reachable.

## 3.3 Finding fields

No selector is specific to one UI. Every field is found the same way:

1. `searchRoots(root)` collects every document and shadow root reachable
   from `document`: it walks all elements, descends into each
   `el.shadowRoot`, and into each `IFRAME`/`FRAME` whose `contentDocument` it
   can read (cross-origin frames throw or return null and are skipped). Depth
   is capped at 12.
2. `deepQueryAll(selector)` runs `querySelectorAll` in every one of those roots.
3. `allFields()` keeps the visible, enabled `input`, `textarea` and
   `[contenteditable='true']` elements (not `type="hidden"`, not `disabled`,
   and with an `offsetParent` or a client rect).
4. `signature(node)` joins the node's `id`, `name`, `aria-label`,
   `placeholder`, `data-field`, `data-type` and the text of its `<label>`s,
   lowercased.
5. `findField(key)` tests each signature against `PATTERNS[key]` and prefers a
   writable match (`!readOnly`), falling back to a read-only one for reading.

| key | pattern | note |
|---|---|---|
| `ticket_number` | `/(^\|[\s\|._-])number([\s\|._-]\|$)/` | anchored on both sides, or it matches `phone_number`, `account_number`, sys_id fields |
| `short_description` | `/short[\s._-]*description/` | |
| `description` | `/(^\|[\s\|._-])description/` | a field that also matches `short_description` is rejected |
| `work_notes` | `/work[\s._-]*notes/` | |
| `comments` | `/additional\s*comments\|(^\|[\s\|._-])comments([\s\|._-]\|$)/` | |

`readValue(node)` returns the trimmed `innerText` of a contenteditable, else
the trimmed `value`.

The activity stream is read by `readActivity()`: the first non-empty
`innerText` of `.sn-widget-list-table_v2`, `#activity-stream`,
`[data-stream-entries]` or `.h-card-wrapper`, cut to `MAX_ACTIVITY_CHARS`
(100,000). Its markup varies between versions, so empty is normal. The limit
is high on purpose: the comment records that at 4,000 characters the
notification noise filled the cut and older messages never arrived; the
backend now does the cleaning and fitting (chapter 5).

## 3.4 The request form of a catalog item

Catalog tasks and requested items (multi-GPU, long partition, Explorer
access, classroom use) carry their substance in the request form's answers,
not in the description, which is often empty. On a task those answers are
read-only. `readRequestForm()` reads them.

**Finding the form** (`formContainers()`):

- every element matching one of `FORM_CONTAINERS`: `#variable_map`,
  `.variable_editor`, `table.variable_editor`, `.sc_variable_editor`,
  `[id^='variable_editor']`, `.catalog_variables`,
  `sn-catalog-variable-editor`, `now-record-catalog-variable-editor`,
  `sp-variable-editor`, `.sp-variable-editor`, `[data-type='variable_editor']`;
- plus every section headed by text matching `FORM_HEADINGS`
  (`variables`, `request details`, `catalog variables`, `item variables`,
  `request form`, `options`, whole text, case-insensitive). Headings are
  `h1`-`h4`, `legend`, `.section-header`, `.sn-widget-header`,
  `.tab_caption_text`, `[role='heading']`, `[role='tab']`, `caption`; the
  section is the heading's closest `section`, `fieldset`, `.section`,
  `.tabs2_section`, `[role='region']`, `[role='tabpanel']`, `.panel`,
  `.form-section` or `table`, else its parent;
- de-duplicated, keeping only the outermost of nested matches.

**Reading each field** in a container (`input`, `select`, `textarea`,
`[contenteditable='true']`). The ticket's own five fields (from
`findField`) are skipped, as are `hidden`, `button` and `submit` inputs.
Unlike `allFields()`, read-only and disabled fields are read.

| field | question | answer |
|---|---|---|
| `select` | label (below) | the selected option's text; nothing if its value is `""` (`-- None --`) |
| checkbox | label | `yes` / `no` |
| radio | the group's `legend`, `.question_text`, `label.control-label` or `th` (group = closest `fieldset`, `.question_spacer`, `.form-group`, `tr`, `[role='radiogroup']`), else the input's `name` | the checked option's label; unchecked radios are skipped |
| contenteditable | label | its `innerText` |
| anything else | label | its `value` |

`questionLabel(node)` tries, in order: the node's first `<label>`; its
`aria-label`; the element named by `aria-labelledby` (looked up in the node's
own root, so it works inside a shadow root); the `label`, `legend`,
`.question_text`, `.control-label`, `.sn-tooltip-basic` or `th` of the row it
sits in (closest `.question_spacer`, `.form-group`, `.sc-row`, `tr`, `li` or
`[class*='question']`). Trailing `:`, `*` and spaces are removed (the
required-field star). `squash()` collapses runs of spaces and tabs and trims
around newlines, so multi-line answers stay multi-line.

A reference field shows its display value in a visible `sys_display.*`
input and keeps the sys_id in a hidden input. Hidden inputs are skipped, so
only the display value is read.

A question already seen is not repeated. A container with **no** fields but
with text (a read-only form rendered as text) contributes its `innerText` as
one answer with no question. At most 40 pairs are kept.

**What is sent.** `formText()` writes one line per answer,
`- <question>: <answer>`, continuation lines of a multi-line answer indented
two spaces, and question-less text as is. The whole block is cut to
`MAX_FORM_CHARS` (6,000) with `[...]` appended.

`tests/ui/extension_form_test.py` covers four page shapes: a Classic-style
variable editor (disabled select, read-only textarea, a reference field with
its hidden sys_id, a disabled checkbox, a read-only input), a Service
Portal-style form (a `*` label, `aria-labelledby`, a radio group), a
read-only *Variables* section rendered as text, and an incident with no form.

## 3.5 What `gatherTicket()` sends

`gatherTicket()` reads the number, short description, description, activity
and request form, and returns `null` when there is no short description, no
description and no form answer. Otherwise it returns `{number, has, text}`
where `text` is these blocks, empty ones left out, joined by a blank line:

```
Ticket: <number>

Short description: <short description>

Description:
<description>

Request form:
- <question>: <answer>
...

Work notes / activity:
<activity stream as the page renders it, newest first>
```

The labels are a contract with the backend: `ticket_context.prepare` splits on
`Work notes / activity:` (`ACTIVITY_LABEL`) and `build_query` reads the
`Short description:`, `Description:` and `Request form:` fields (chapter 5).
Change one side, change both.

`has` records what was found (`number`, `short`, `description`, `activity`
as booleans; `form` as the answer count) for the panel.

## 3.6 The panel

`build()` adds two elements to `document.body`, once:

- `#rc-copilot-fab`, the red **Draft reply** pill, fixed bottom-right. The
  comment: a labelled pill, *not a subtle edge tab*, because the entry point
  has to be obvious on a busy ServiceNow form.
- `#rc-copilot-panel`, an `<aside>` docked on the right.

Everything put into `innerHTML` from the ticket or the backend goes through
`escapeHtml()`.

| part | what it does |
|---|---|
| header | ⅓ / ½ / ⅔ width presets (`33.333vw`, `50vw`, `66.667vw`); the left edge drags to any width (minimum 340 px, maximum 95% of the window); double-click resets to half. The choice is saved in `chrome.storage.local` under `rcPanelWidth` |
| status strip | `idle`, `checking`, `starting`, `ok`, `warn`, `bad`; clicking it runs `testConnection()` (3.8) |
| startup box | while a session loads: a *Model* bar and a *Knowledge graph* bar with its step checklist, from `/api/health`'s `startup` block |
| *This ticket* | `renderTicket()`: the number and one row per part read (below) |
| *Instructions* | a free-text box, five quick-instruction chips that append their text once (*Follow up / close*, *Ask for missing details*, *Shorter*, *Step-by-step*, *Mark resolved*), and **Write the draft into** |
| result | after a draft (3.7) |

The rows of *This ticket*: *short description*, *description*, then, when
the page had form answers **or** the number starts `SCTASK`, `TASK`, `RITM`
or `REQ`, either *request form (N answers)* or *no request form found*;
then *comments & work notes* or *no comments yet*. Incidents with no form get
no form row. With nothing read, **Draft reply** is disabled.

**Write the draft into** has three values. It is the same setting as the
popup's (`oodConfig.targetField`), so they always agree:

| value | where the draft goes | note shown |
|---|---|---|
| `work_notes` (default) | work notes | internal only: a mis-click can't reach the researcher |
| `comments` | additional comments | the researcher sees it; one wrong Update sends it (red) |
| `both` | the analysis to work notes, the reply to additional comments | red |

Open or closed is remembered per tab (`sessionStorage` key
`rc-copilot-open`); Escape closes the panel; Ctrl+Enter (or Cmd+Enter) in the
instructions box drafts.

## 3.7 Drafting and writing the draft back

`draft()` sends `{type: "DRAFT", ticketText, ticketNumber, extraInstructions}`
to the service worker and shows a placeholder (*Asking the model. Up to a
minute on a busy cluster.*). An error is shown as returned. A thrown
`sendMessage` means the extension was reloaded under the page: *refresh this
page*.

`renderResult(data)` then:

1. Reads `targetField` (`GET_CONFIG`).
2. Builds the writes. For `both`, the reply (`draft_response`) goes to
   comments and `analysisText(data)` to work notes: *Summary*, numbered
   *Suggested steps*, *Check before sending* caveats and a last line
   `-- drafted by RC Copilot (confidence <label> <n>%), not reviewed --`.
   It is plain text because journal fields do not render Markdown, and
   writing the reply into both fields would duplicate it and lose the
   analysis.
3. Writes each with `setFieldValue(node, value)`, if `findField` returns a
   writable field.

**`setFieldValue`.** Assigning `.value` is not enough: ServiceNow's client
scripts and its unsaved-changes tracker only react to events, and frameworks
patch the `value` property. So:

- contenteditable: set `textContent`, dispatch a bubbling `input` event;
- otherwise: take the `value` property descriptor from the prototype of the
  node's **own** window (`node.ownerDocument.defaultView`), because the node
  may be in a child frame where `instanceof` against this frame's
  constructors is false and would pick the input setter for a textarea and
  throw; then `focus()`, call the native setter, dispatch bubbling `input`
  and `change`, `blur()`.

Nothing else happens to the form. **The draft is never saved**: the engineer
reviews it and clicks Update.

The result section always shows what to act on: where the draft was written
(*Written into work notes, unsaved. Review it, then click Update.*, plus
*Could not find: ...* for a target that failed; or *No writable ... field
here -- copy the draft from below*), *How sure the model is* (label and
percent), *Check before sending* (the caveats), and the draft in an editable
textarea. Below it, collapsible `<details>` sections whose titles say what is
inside. Which are open is remembered per tab (`rc-copilot-open-sections`):

| section | from the response |
|---|---|
| *What's going on & steps for you (N)* | `problem_summary`, `suggested_steps` |
| *Cluster lookups (N ran)* | `lookups`: each `command` and `summary` (chapter 6) |
| *RC pages cited (N)* | `references` as links; or *none -- knowledge graph `<kg_status>`* |
| *Quality checks: X of Y passed* | `evaluations.assertions`, named by `ASSERT_NAMES`; marked when one failed; `reply_words`, `conciseness`, `answer_relevance_embedding` |
| *Ticket & model details* | model, latency, tokens, draft id, and `contextHtml(data.context)`: raw → cleaned characters, notifications, addresses, signatures and quotes removed, middle entries left out, playbooks followed |
| *Rate this draft* | feedback (below) |

Footer buttons: **Copy** (clipboard, with an `execCommand("copy")` fallback)
and **Insert** (writes the textarea, as edited, into the reply field: comments
for `both`, else the chosen field; for `both` it also rewrites work notes with
the analysis).

**Feedback.** Chips for *Helpful?* (`thumbs` 1/0), *Correct?*
(`correctness` 1/0.5/0) and *Used it?* (`usage` 1/0.5/0 with value
`used_as_is`/`edited`/`not_used`), a comment, and **Capture what's in the
field**, which reads the reply field as the engineer edited it. **Send** posts
one `FEEDBACK` message per chosen chip, plus one with key `comment` carrying
the comment and the captured text as `correction`. The backend compares the
correction with the draft (`edit_similarity`, chapter 4).

> **rc-agent:** `contextHtml` also shows *agent: N lookups/searches* when
> `context.agent.tool_calls` is non-empty. It is the only difference in the
> extension.

## 3.8 The service worker and every network call

`background.js` is the only code that fetches. An MV3 content script runs in
the page's origin and is subject to CORS, so a fetch from ServiceNow to
`northeastern.edu` would be blocked whatever the backend sent; service-worker
fetches are covered by `host_permissions` and skip CORS.

**Where requests go.** `oodConfig.baseUrl` is the session's address through
OOD's node proxy, `https://ood.explorer.northeastern.edu/rnode/<node>/<port>`.
OOD authenticates the request (university SSO) and forwards it to the
backend on the GPU node. `request(path, options)` adds:

| | value | why |
|---|---|---|
| `credentials` | `include` | sends the OOD SSO cookie; without it OOD returns its login page |
| `cache` | `no-store` | |
| `Accept` | `application/json` | |
| timeout | `TIMEOUT_MS` = 180,000 ms, by `AbortController` | drafting on a busy cluster is slow |

It never throws; it always resolves to `{ok, status, data | error}`. It tells
failures apart, because most come from OOD, not the backend:

| answer | what the extension concludes |
|---|---|
| network error | *Could not reach ...* (URL, campus network or VPN) |
| abort | *Timed out after 180s* |
| HTML containing `Failed to connect to <host:port>`, or HTML with status >= 500 | `backendDown`: nothing listens yet. A new session starts its backend a minute or two after the card says Running; telling these people to sign in sent them the wrong way |
| other HTML | `needsOodLogin`: the OOD sign-in expired (OOD answers 200 + HTML, not 401) |
| non-2xx whose JSON body has `status` and `vllm_base_url` | treated as ok: an older backend answered `/api/health` with 503 while the model loaded |
| 404 | the session ended or the address is old: launch a new one |
| 502 / 503 / 504 | OOD reached the node but the app did not answer |
| 401 / 403 | OOD rejected the request: sign in to OOD |
| other | `Backend returned HTTP <n>.` plus the body's `error` or `detail` |

**The operations** (the message handlers):

| message | call | notes |
|---|---|---|
| `PING` | `GET /api/health` | `ok` only when `status == "ok"`; while vLLM loads (a `startup` block, not failed) returns `starting: true` with the progress |
| `SIGN_IN` | `POST /api/login` `{access_token}` | stores `{token: session_token, expiresAt: expires_at}` |
| `DRAFT` | `POST /api/draft` `{ticket_text, ticket_number?, extra_instructions?}` | `Authorization: Bearer <session token>` |
| `FEEDBACK` | `POST /api/feedback` `{request_id, key, score, value, comment, correction}` | bearer token |
| `GET_CONFIG` | -- | `{baseUrl, accessToken, targetField}` with defaults |
| `SAVE_CONFIG` | -- | from `ood-connect.js` (3.9) |

**The session.** The OOD session card's access token is exchanged for a
session token at `/api/login`. The session token is kept in
`chrome.storage.session`, which lives in memory and is cleared when the
browser closes, deliberately not in `storage.local`. `withSession(fn)` signs
in when there is no unexpired session, runs `fn`, and on a 401 clears the
session, signs in again and retries once. A 401 at sign-in means the token
belongs to another session (*it changes with every session*). Any change to
`oodConfig` in `storage.local` clears the session, because a new job means a
new token.

**`testConnection()`** in the panel: `PING`; if the session is starting, show
the progress and poll every 3 s (`BOOT_POLL_MS`) for up to 40 min
(`BOOT_GIVE_UP_MS`; a first model download can take most of the job's 30-min
wait); if the backend is down, keep polling for 5 min (`BACKEND_WAIT_MS`),
then report it; otherwise `SIGN_IN` and show *Connected*, the model and the
time left on the sign-in, updated every minute without contacting the
cluster. When the model is ready but the graph is still loading, it keeps
polling `PING` alone (`pollGraph`) with the note *You can draft now; until the
graph is ready, drafts won't cite RC pages.*

## 3.9 Connecting to the OOD session

The OOD app's session card (`ood-apps/rc-copilot/view.html.erb`) contains a
hidden element:

```html
<div id="rc-copilot-connection"
     data-rc-path="/rnode/<host>/<port>"  data-rc-token="<password>"
     data-rc-node="<host>"  data-rc-app="RC Copilot" hidden></div>
```

`host`, `port` and `password` come from `connection.yml`, which OOD writes from
what `template/before.sh.erb` exports: `port=$(find_port)` and
`password=$(create_passwd)`; the job script passes the password to the
backend as `AUTH_TOKEN`. OOD shows a session card only to the job's owner,
the same trust boundary OOD uses for Jupyter and RStudio passwords. The path
is relative so nothing hard-codes a host.

`ood-connect.js` runs on OOD pages. `harvest()` finds the marker by id,
builds `baseUrl = location.origin + data-rc-path`, and sends `SAVE_CONFIG`
unless the same URL, token and app were already saved from this page (each
save clears the session, so it must not repeat on every DOM change). A
`MutationObserver` (400 ms debounce) re-runs it, because *My Interactive
Sessions* renders cards asynchronously. A corner toast says *<app>
configured automatically -- session on <node>* or why it failed.

`saveConfig` in the service worker accepts only a URL whose host ends in
`northeastern.edu` or `neu.edu`, so another site cannot point the extension
at an endpoint of its choosing, and requires a token. It keeps the existing
`targetField` (*never silently change where the draft gets written*).

**The popup** shows the node (from the URL), the model and the time left
after **Test connection** (`PING` then `SIGN_IN`), and the access token
masked (first and last four characters). **Connection details** saves a URL
and token by hand, for `serve_stack.sbatch` sessions; it applies the same
host rule and also requires `https:`.

## 3.10 Packaging, versioning and publishing

1. **Check.** `backend/.venv/bin/python extension/check.py` (it needs
   `esprima`, which is in the backend venv). Each check exists because what
   it catches once shipped broken:

   | check | catches |
   |---|---|
   | every `*.js` parses | syntax errors |
   | no duplicate `const`/`let` in one scope | a duplicate `PANEL_ID` made the whole content script throw at load; esprima's parse does no scope analysis |
   | the CSS token block (`--rc-accent`) is declared on every root id the JS creates (`FAB_ID`, `PANEL_ID`); no `var(--rc-*)` under an undeclared id | the collapsed button once had no background |
   | every class queried with `$(".x")` / `querySelector(".x")` is emitted; every emitted class is styled (except the JS hooks) | selectors that fail silently |
   | every message `type` sent by content, popup and OOD scripts has a handler in `background.js` | |
   | every file the manifest names exists; every `el("id")` in `popup.js` exists in `popup.html` | |

2. **Version.** Bump `"version"` in `manifest.json`. Chrome shows it on
   `chrome://extensions`; it is how a member can tell they loaded the new
   build.
3. **Package.** `extension/package.sh` runs `check.py` (and stops if it
   fails), then zips `manifest.json`, `background.js`, `content.js`,
   `content.css`, `ood-connect.js`, the popup files and the four icons at the
   zip's top level into `dist/rc-copilot-extension.zip`, through a `.tmp` file
   renamed at the end so a failed run never leaves half a zip.
4. **Publish.** Commit, then `ood-apps/publish.sh`. It copies
   `ood-apps/rc-copilot/` and the zip into
   `<repo>/rc-copilot/releases/<YYYYmmdd-HHMMSS>-<commit>[-dirty]/`, writes
   `template/ticketmate.env` (`TICKETMATE_HOME=<repo>`, which the job script
   reads) and a `VERSION` file, refreshes `install.sh` and `README.md` (from
   `MEMBERS.md`) beside the releases, links `rc-copilot-extension.zip` to the
   live release's zip, and switches the `current` symlink atomically (a new
   link renamed over the old). It keeps the newest 3 releases.
   `--list` shows them; `--rollback` steps back to the newest release older
   than the live one. Running sessions are unaffected: OOD copies the
   template into each session's staging directory at launch.

The extension does not update itself. Members re-download the zip, replace
the unzipped folder and click reload on `chrome://extensions`. While
developing, *Load unpacked* the `extension/` folder itself.

> **rc-agent:** releases go to `/projects/rc/projects/rc-agent/rc-copilot`,
> and `install.sh` links `~/ondemand/dev/rc-agent` (TicketMate's link is
> `~/ondemand/dev/rc-copilot`), so the two apps can be installed side by side.
> The app is named *RC Agent* in `manifest.yml`. The extension zip has the
> same name and version as TicketMate's.

## 3.11 Tests

| test | how to run | what it proves |
|---|---|---|
| `extension/check.py` | `backend/.venv/bin/python extension/check.py` | the static checks of 3.10 |
| `tests/ui/extension_form_test.py` | `<Playwright python> tests/ui/extension_form_test.py` | the request-form reader on four mock pages (3.4): the `Request form:` block, each field type, the sys_id never sent, the ticket's own fields not repeated, `request form (5 answers)` in the panel, nothing about a form on an incident |
| `tests/ui/extension_result_test.py` | run by `tests/ui/run.sh` | the result layout: half-page width, presets, drag and its persistence, double-click reset, quick chips, the target choice saved, the draft written into the chosen field, what is always shown and what is collapsed, sections remembered, citations as links, feedback, no JavaScript errors |
| `tests/ui/extension_startup_test.py` | `<Playwright python> tests/ui/extension_startup_test.py <timeline.json>` | replays a recorded `/api/health` timeline (`RC_SLOW_START_TIMELINE=... tests/startup/run_slow_start.sh`) through the real `ping()` from `background.js` into the panel |
| `tests/ui/web_feedback_test.py` | run by `tests/ui/run.sh` | the backend's own web page (chapter 4) |

All use headless Chromium through Playwright, kept out of the backend venv
(setup in the header of `tests/ui/run.sh`). The extension tests inject the
shipped `content.js` and `content.css` into a mock page and replace
`chrome.runtime` with a stub that answers `PING`, `SIGN_IN`, `GET_CONFIG`
and captures `DRAFT`; no backend is needed. `run.sh` starts rc-kg, the stub
model and a backend for `web_feedback_test.py`, then runs
`extension_result_test.py`. It does not run the form and startup tests; run
them by hand.
