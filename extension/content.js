/* Runs on ServiceNow pages. Reads the open ticket, asks the backend for a
 * draft, writes it into the work notes.
 *
 * Handles all three ServiceNow UIs:
 *
 *   Classic          form lives inside an iframe (#gsft_main), plain DOM.
 *                    The manifest sets all_frames:true so we run in there.
 *   Next Experience  form is web components with closed-ish shadow roots, so
 *                    querySelector alone never reaches the inputs.
 *   Service Portal   AngularJS, sp_formfield_* ids.
 *
 * All three are handled by collecting fields with a shadow-piercing walk
 * instead of selectors that only work in one of them.
 */

(function () {
  "use strict";

  const PANEL_ID = "rc-copilot-panel";
  const MAX_ACTIVITY_CHARS = 4000;

  /* ---------- field discovery ---------- */

  /* Every document and shadow root reachable from here. Next Experience needs
     the shadow roots; Classic is unaffected because it has none.

     Same-origin iframes are followed too. Next Experience opens classic forms
     (/now/nav/ui/classic/...) in a #gsft_main iframe that sits inside the
     shell's shadow DOM, so without this the top frame sees no fields at all.
     Cross-origin frames throw or return null, and are skipped. */
  function searchRoots(root, out, depth) {
    out.push(root);
    if (depth > 12) return out; // guard against pathological nesting

    let all;
    try {
      all = root.querySelectorAll("*");
    } catch (err) {
      return out;
    }
    for (const el of all) {
      if (el.shadowRoot) searchRoots(el.shadowRoot, out, depth + 1);
      if (el.tagName === "IFRAME" || el.tagName === "FRAME") {
        let doc = null;
        try { doc = el.contentDocument; } catch (err) { /* cross-origin */ }
        if (doc) searchRoots(doc, out, depth + 1);
      }
    }
    return out;
  }

  function deepQueryAll(selector) {
    const out = [];
    for (const root of searchRoots(document, [], 0)) {
      try {
        for (const node of root.querySelectorAll(selector)) out.push(node);
      } catch (err) { /* detached or cross-origin mid-navigation */ }
    }
    return out;
  }

  function allFields() {
    return deepQueryAll("input, textarea, [contenteditable='true']").filter(function (node) {
      if (node.type === "hidden" || node.disabled) return false;
      if (node.offsetParent === null && node.getClientRects().length === 0) return false;
      return true;
    });
  }

  /* Everything we can use to recognise a field, lowercased into one string. */
  function signature(node) {
    const parts = [
      node.id,
      node.name,
      node.getAttribute("aria-label"),
      node.getAttribute("placeholder"),
      node.getAttribute("data-field"),
      node.getAttribute("data-type")
    ];
    if (node.labels && node.labels.length) {
      for (const label of node.labels) parts.push(label.textContent);
    }
    return parts.filter(Boolean).join(" | ").toLowerCase();
  }

  const PATTERNS = {
    // Anchored on both sides: a loose /number/ would match phone_number,
    // account_number, sys_id fields and so on.
    ticket_number: /(^|[\s|._-])number([\s|._-]|$)/,
    short_description: /short[\s._-]*description/,
    description: /(^|[\s|._-])description/,
    work_notes: /work[\s._-]*notes/,
    comments: /additional\s*comments|(^|[\s|._-])comments([\s|._-]|$)/
  };

  function findField(key) {
    const wanted = PATTERNS[key];
    if (!wanted) return null;

    const candidates = allFields().filter(function (node) {
      const sig = signature(node);
      if (!wanted.test(sig)) return false;
      // "description" must not swallow "short description".
      if (key === "description" && PATTERNS.short_description.test(sig)) return false;
      return true;
    });

    // Prefer a writable one; fall back to read-only for reading.
    return candidates.find((n) => !n.readOnly) || candidates[0] || null;
  }

  function readValue(node) {
    if (!node) return "";
    if (node.isContentEditable) return (node.innerText || "").trim();
    return (node.value || "").trim();
  }

  /* Best effort: the activity stream gives the model prior context. Its markup
     differs a lot between versions, so treat an empty result as normal. */
  function readActivity() {
    const selectors = [
      ".sn-widget-list-table_v2",
      "#activity-stream",
      "[data-stream-entries]",
      ".h-card-wrapper"
    ];
    for (const selector of selectors) {
      for (const node of deepQueryAll(selector)) {
        if (node.innerText && node.innerText.trim()) {
          return node.innerText.trim().slice(0, MAX_ACTIVITY_CHARS);
        }
      }
    }
    return "";
  }

  /* Northeastern runs ServiceNow on a custom domain (service.northeastern.edu),
     so the hostname tells us nothing about which UI we're in. Detect from the
     DOM instead. Three are possible:
       Classic         agent form inside the #gsft_main iframe
       Next Experience agent workspace, web components + shadow DOM
       Service Portal  the "Service Hub" requester portal, AngularJS */
  function detectUi() {
    if (document.querySelector("now-record-form, now-app-root, [id^='sn-']")) {
      return "Next Experience";
    }
    if (window.name === "gsft_main" || document.querySelector("[id^='element.'], [id^='sys_display.']")) {
      return "Classic";
    }
    if (document.querySelector("[id^='sp_formfield_'], .sp-form, #sp-page-root, sp-page, .sp-widget")) {
      return "Service Portal";
    }
    return "Unknown";
  }

  /* ---------- writing ---------- */

  /* Assigning .value silently is not enough: ServiceNow's client scripts and
     its unsaved-changes tracker only react to events. Go through the native
     setter (frameworks patch the property) and then dispatch. */
  function setFieldValue(node, value) {
    if (node.isContentEditable) {
      node.textContent = value;
      node.dispatchEvent(new Event("input", { bubbles: true }));
      return true;
    }

    // The node may live in a child frame, and instanceof against this frame's
    // constructors is false for it -- which would pick the input setter for a
    // textarea and throw. Use the constructors of the node's own window.
    const win = (node.ownerDocument && node.ownerDocument.defaultView) || window;
    const proto =
      node instanceof win.HTMLTextAreaElement
        ? win.HTMLTextAreaElement.prototype
        : win.HTMLInputElement.prototype;
    const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
    if (!descriptor || !descriptor.set) return false;

    node.focus();
    descriptor.set.call(node, value);
    node.dispatchEvent(new Event("input", { bubbles: true }));
    node.dispatchEvent(new Event("change", { bubbles: true }));
    node.blur();
    return true;
  }

  /* ==================================================================
     UI — a "Draft reply" button that opens a docked right-hand panel
     holding the whole drafting workflow: connection, ticket, instructions,
     draft. The toolbar popup is still needed for one thing the panel does not
     own: choosing which field the draft is written into (work notes /
     additional comments / both). We read that here via GET_CONFIG.
     ================================================================== */

  const FAB_ID = "rc-copilot-fab";
  const OPEN_KEY = "rc-copilot-open";

  let extraInstructions = "";   // survives a redraft
  let lastTicket = null;
  let statusTimer = null;
  let bootTimer = null;
  let bootSince = 0;

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = String(text == null ? "" : text);
    return div.innerHTML;
  }

  const $ = (sel) => {
    const p = document.getElementById(PANEL_ID);
    return p ? p.querySelector(sel) : null;
  };

  /* ---------- open / closed ---------- */

  function isOpen() {
    try { return sessionStorage.getItem(OPEN_KEY) === "1"; } catch (e) { return false; }
  }
  function setOpen(open) {
    try { sessionStorage.setItem(OPEN_KEY, open ? "1" : "0"); } catch (e) { /* private mode */ }
    const panel = document.getElementById(PANEL_ID);
    const fab = document.getElementById(FAB_ID);
    if (panel) panel.setAttribute("data-open", open ? "1" : "0");
    if (fab) fab.setAttribute("data-hidden", open ? "1" : "0");
    if (open) refresh();
  }

  /* ---------- structure, built once ---------- */

  function build() {
    if (document.getElementById(PANEL_ID)) return;

    // A labelled pill, not a subtle edge tab: the entry point has to be
    // obvious on a page as busy as a ServiceNow form.
    const fab = document.createElement("button");
    fab.id = FAB_ID;
    fab.type = "button";
    fab.className = "rc-fab";
    fab.title = "Open RC Copilot and draft a reply to this ticket";
    fab.innerHTML =
      '<span class="rc-fab-ico" aria-hidden="true"></span>' +
      '<span class="rc-fab-label">Draft reply</span>';
    fab.addEventListener("click", () => setOpen(true));
    document.body.appendChild(fab);

    const panel = document.createElement("aside");
    panel.id = PANEL_ID;
    panel.className = "rc-panel";
    panel.setAttribute("role", "complementary");
    panel.setAttribute("aria-label", "RC Copilot");
    panel.innerHTML = [
      '<header class="rc-head">',
        '<span class="rc-mark" aria-hidden="true"></span>',
        '<span class="rc-title">RC Copilot</span>',
        '<button type="button" class="rc-icon-btn rc-collapse" aria-label="Collapse">›</button>',
      "</header>",

      // status strip — the thing the toolbar popup used to own
      '<button type="button" class="rc-status" data-state="idle">',
        '<span class="rc-pulse" aria-hidden="true"><i></i></span>',
        '<span class="rc-status-text">',
          '<strong class="rc-status-title">Not checked</strong>',
          '<span class="rc-status-sub">click to test the connection</span>',
        "</span>",
        '<span class="rc-status-go">Test</span>',
      "</button>",
      // startup progress while a fresh session loads vLLM and the graph
      '<div class="rc-boot" hidden></div>',

      '<div class="rc-scroll">',
        '<section class="rc-sec rc-sec-ticket">',
          '<h3 class="rc-h">Ticket</h3>',
          '<div class="rc-ticket"></div>',
        "</section>",

        '<section class="rc-sec rc-sec-compose">',
          '<h3 class="rc-h">Instructions <span class="rc-opt">optional</span></h3>',
          '<textarea class="rc-notes" rows="3" placeholder="Anything the model should know that the ticket doesn\'t say."></textarea>',
          '<button type="button" class="rc-btn rc-btn-primary rc-go">Draft reply</button>',
          '<p class="rc-hint">Ctrl+Enter to draft</p>',
        "</section>",

        '<section class="rc-sec rc-sec-result" hidden></section>',
      "</div>",

      '<footer class="rc-foot" hidden></footer>'
    ].join("");

    document.body.appendChild(panel);

    panel.querySelector(".rc-collapse").addEventListener("click", () => setOpen(false));
    panel.querySelector(".rc-status").addEventListener("click", testConnection);
    panel.querySelector(".rc-go").addEventListener("click", draft);
    panel.querySelector(".rc-notes").addEventListener("keydown", function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); draft(); }
    });

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && isOpen()) setOpen(false);
    });

    panel.setAttribute("data-open", isOpen() ? "1" : "0");
    fab.setAttribute("data-hidden", isOpen() ? "1" : "0");
  }

  /* ---------- status strip ---------- */

  function setStatus(state, title, sub, action) {
    const el = $(".rc-status");
    if (!el) return;
    el.dataset.state = state;
    $(".rc-status-title").textContent = title;
    $(".rc-status-sub").textContent = sub || "";
    $(".rc-status-go").textContent = action || "Test";
  }

  /* ---------- startup progress ---------- */

  // How long to keep polling a session that is still starting: a first-time
  // model download can take most of the 30 minutes the job script allows.
  const BOOT_POLL_MS = 3000;
  const BOOT_GIVE_UP_MS = 40 * 60 * 1000;

  function fmtElapsed(s) {
    s = Math.max(0, Math.round(s || 0));
    return s >= 60 ? Math.floor(s / 60) + "m " + (s % 60) + "s" : s + "s";
  }

  function bootRow(name, p) {
    const state = p.failed ? "failed" : p.stage === "ready" ? "done" : "running";
    const pct = Math.max(0, Math.min(100, Number(p.pct) || 0));
    return (
      '<div class="rc-boot-row" data-status="' + state + '">' +
        '<div class="rc-boot-head">' +
          '<span class="rc-boot-name">' + escapeHtml(name) + "</span>" +
          '<span class="rc-boot-stage">' + escapeHtml(p.label || p.stage || "") + "</span>" +
          '<span class="rc-boot-pct">' + Math.round(pct) + "%</span>" +
        "</div>" +
        '<div class="rc-bar"><span class="rc-bar-fill" style="width:' + pct + '%"></span></div>' +
        (p.detail && state !== "done" ? '<div class="rc-boot-detail">' + escapeHtml(p.detail) + "</div>" : "") +
      "</div>"
    );
  }

  function bootSteps(steps) {
    if (!steps || !steps.length) return "";
    return (
      '<ol class="rc-steps">' +
      steps.map(function (st) {
        return '<li class="rc-step" data-status="' + escapeHtml(st.status) + '">' + escapeHtml(st.label) + "</li>";
      }).join("") +
      "</ol>"
    );
  }

  // Shows what the session is doing; returns false once there is nothing left to show.
  function renderStartup(st, note) {
    const box = $(".rc-boot");
    if (!box) return false;
    if (!st || st.ready) { box.hidden = true; box.innerHTML = ""; return false; }
    const kg = st.knowledge_graph || {};
    const showKg = kg.stage && kg.stage !== "disabled";
    box.innerHTML =
      '<div class="rc-boot-time">' + (((st.vllm || {}).failed || kg.failed) ? "Startup failed after " : "Starting up · ") +
        fmtElapsed(st.elapsed_s) + (((st.vllm || {}).failed || kg.failed) ? "" : " elapsed") + "</div>" +
      bootRow("Model", st.vllm || {}) +
      (showKg ? bootRow("Knowledge graph", kg) + (kg.stage === "ready" ? "" : bootSteps(kg.steps)) : "") +
      (note ? '<p class="rc-boot-note">' + escapeHtml(note) + "</p>" : "");
    box.hidden = false;
    return true;
  }

  function pollStartup(fn) {
    clearTimeout(bootTimer);
    if (!bootSince) bootSince = Date.now();
    if (Date.now() - bootSince > BOOT_GIVE_UP_MS) return;   // the strip still offers Retry
    bootTimer = setTimeout(fn, BOOT_POLL_MS);
  }

  // After sign-in, only the graph may still be loading: keep its progress up
  // without signing in again on every poll.
  async function pollGraph() {
    const ping = await chrome.runtime.sendMessage({ type: "PING" }).catch(() => null);
    if (!ping || !ping.ok) { testConnection(); return; }
    if (renderStartup(ping.startup, "You can draft now; until the graph is ready, drafts won't cite RC pages.")) {
      pollStartup(pollGraph);
    } else {
      bootSince = 0;
    }
  }

  async function testConnection() {
    clearTimeout(bootTimer);
    if ($(".rc-status").dataset.state !== "starting") {
      setStatus("checking", "Checking…", "contacting the cluster", "…");
    }

    const ping = await chrome.runtime.sendMessage({ type: "PING" }).catch(() => null);
    if (ping && ping.starting) {
      const st = ping.startup || {};
      const v = st.vllm || {};
      setStatus("starting", "Starting up…",
        "loading the model · " + Math.round(Number(v.pct) || 0) + "% · " + fmtElapsed(st.elapsed_s), "Check");
      renderStartup(st);
      pollStartup(testConnection);
      return false;
    }
    if (!ping || !ping.ok) {
      setStatus("bad", "Not connected", (ping && ping.error) || "no reply", "Retry");
      // A failed load keeps its progress up: it shows which stage broke.
      if (!(ping && ping.startup && renderStartup(ping.startup))) renderStartup(null);
      bootSince = 0;
      return false;
    }

    const login = await chrome.runtime.sendMessage({ type: "SIGN_IN" }).catch(() => null);
    if (!login || !login.ok) {
      setStatus("warn", "Reachable, not signed in", (login && login.error) || "", "Retry");
      return false;
    }

    const model = (ping.data && ping.data.configured_model) || "?";
    const mins = Math.max(0, Math.round((login.expiresAt * 1000 - Date.now()) / 60000));
    const left = mins >= 60 ? Math.floor(mins / 60) + "h " + (mins % 60) + "m" : mins + "m";
    setStatus("ok", "Connected", model + " · " + left + " left", "Recheck");
    if (renderStartup(ping.startup, "You can draft now; until the graph is ready, drafts won't cite RC pages.")) {
      pollStartup(pollGraph);
    } else {
      bootSince = 0;
    }

    // Keep the countdown honest without re-pinging the cluster.
    clearInterval(statusTimer);
    statusTimer = setInterval(function () {
      const m = Math.max(0, Math.round((login.expiresAt * 1000 - Date.now()) / 60000));
      if (m <= 0) {
        clearInterval(statusTimer);
        setStatus("warn", "Session expired", "sign in again", "Retry");
      } else if ($(".rc-status") && $(".rc-status").dataset.state === "ok") {
        const l = m >= 60 ? Math.floor(m / 60) + "h " + (m % 60) + "m" : m + "m";
        $(".rc-status-sub").textContent = model + " · " + l + " left";
      }
    }, 60000);
    return true;
  }

  /* ---------- ticket section ---------- */

  function gatherTicket() {
    const number = readValue(findField("ticket_number"));
    const short = readValue(findField("short_description"));
    const description = readValue(findField("description"));
    const activity = readActivity();

    if (!short && !description) return null;

    return {
      number: number,
      has: { number: !!number, short: !!short, description: !!description, activity: !!activity },
      text: [
        number ? "Ticket: " + number : "",
        short ? "Short description: " + short : "",
        description ? "Description:\n" + description : "",
        activity ? "Work notes / activity:\n" + activity : ""
      ].filter(Boolean).join("\n\n")
    };
  }

  function renderTicket() {
    const box = $(".rc-ticket");
    if (!box) return;
    lastTicket = gatherTicket();

    if (!lastTicket) {
      box.innerHTML =
        '<p class="rc-msg rc-warn"><span>No ticket fields on this page.<br>' +
        "UI: " + escapeHtml(detectUi()) + "<br>Path: " + escapeHtml(location.pathname) +
        "</span></p>";
      const go = $(".rc-go");
      if (go) go.disabled = true;
      return;
    }

    const go = $(".rc-go");
    if (go) go.disabled = false;

    const row = (ok, label) =>
      '<li class="' + (ok ? "rc-yes" : "rc-no") + '">' + escapeHtml(label) + "</li>";

    box.innerHTML =
      '<div class="rc-ticket-id">' +
        (lastTicket.number ? escapeHtml(lastTicket.number) : "<em>no number</em>") +
        '<span class="rc-chars">' + lastTicket.text.length.toLocaleString() + " chars</span>" +
      "</div>" +
      '<ul class="rc-checks">' +
        row(lastTicket.has.short, "short description") +
        row(lastTicket.has.description, "description") +
        row(lastTicket.has.activity, "activity stream") +
      "</ul>";
  }

  /* ---------- drafting ---------- */

  function resultSection() { return $(".rc-sec-result"); }

  function showResultHtml(html, footHtml) {
    const sec = resultSection();
    const foot = $(".rc-foot");
    sec.hidden = false;
    sec.innerHTML = html;
    if (footHtml) { foot.hidden = false; foot.innerHTML = footHtml; }
    else { foot.hidden = true; foot.innerHTML = ""; }
    sec.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  async function draft() {
    if (!lastTicket) renderTicket();
    if (!lastTicket) return;

    extraInstructions = ($(".rc-notes").value || "").trim();

    const go = $(".rc-go");
    go.disabled = true;
    go.textContent = "Drafting…";

    showResultHtml(
      '<h3 class="rc-h">Draft</h3>' +
      '<div class="rc-working"><span class="rc-spin"></span>' +
      '<span class="rc-working-text">Asking the model. Up to a minute on a busy cluster.</span></div>' +
      '<div class="rc-skel" style="width:90%"></div>' +
      '<div class="rc-skel" style="width:72%"></div>' +
      '<div class="rc-skel" style="width:84%"></div>'
    );

    let reply;
    try {
      reply = await chrome.runtime.sendMessage({
        type: "DRAFT",
        ticketText: lastTicket.text,
        ticketNumber: lastTicket.number,
        extraInstructions: extraInstructions
      });
    } catch (err) {
      reply = { ok: false, error: "Extension was reloaded — refresh this page." };
    }

    go.disabled = false;
    go.textContent = "Draft reply";

    if (!reply || !reply.ok) {
      showResultHtml('<h3 class="rc-h">Draft</h3><p class="rc-msg rc-error"><span>' +
        escapeHtml((reply && reply.error) || "Unknown error.") + "</span></p>");
      return;
    }

    renderResult(reply.data || {});
  }

  /* The analysis half of the response, formatted for work notes. Deliberately
     plain text: ServiceNow journal fields do not render markdown. */
  function analysisText(data) {
    const out = [];
    if (data.problem_summary) out.push("Summary: " + data.problem_summary);
    if (Array.isArray(data.suggested_steps) && data.suggested_steps.length) {
      out.push("");
      out.push("Suggested steps:");
      data.suggested_steps.forEach((s, i) => out.push("  " + (i + 1) + ". " + s));
    }
    if (Array.isArray(data.caveats) && data.caveats.length) {
      out.push("");
      out.push("Check before sending:");
      data.caveats.forEach((c) => out.push("  - " + c));
    }
    const conf = typeof data.confidence === "number"
      ? " (confidence " + (data.confidence_label || "?") + " " +
        Math.round(data.confidence * 100) + "%)" : "";
    out.push("");
    out.push("-- drafted by RC Copilot" + conf + ", not reviewed --");
    return out.join("\n");
  }

  async function renderResult(data) {
    const cfg = await chrome.runtime.sendMessage({ type: "GET_CONFIG" }).catch(() => null);
    const targetKey = (cfg && cfg.data && cfg.data.targetField) || "work_notes";
    const targetName = targetKey.replace("_", " ");

    /* "both" splits the response: the analysis goes to work notes for the RC
       engineer, the reply goes to additional comments for the researcher.
       Writing draft_response into both would duplicate it and lose the
       analysis. */
    const writes = targetKey === "both"
      ? [["work_notes", analysisText(data)], ["comments", data.draft_response]]
      : [[targetKey, data.draft_response]];

    const done = [];
    const failed = [];
    for (const [key, value] of writes) {
      if (!value) continue;
      const node = findField(key);
      if (node && !node.readOnly && setFieldValue(node, value)) done.push(key);
      else failed.push(key);
    }
    const wrote = done.length > 0;

    const level = data.confidence_label || "unknown";
    const pct = typeof data.confidence === "number" ? Math.round(data.confidence * 100) : null;
    const caveats = Array.isArray(data.caveats) ? data.caveats : [];

    let html = '<h3 class="rc-h">Result</h3>';

    html +=
      '<div class="rc-conf rc-' + escapeHtml(level) + '">' +
        '<div class="rc-conf-top">' +
          '<span class="rc-conf-label">Confidence</span>' +
          '<span class="rc-conf-val">' + escapeHtml(level) +
          (pct === null ? "" : " · " + pct + "%") + "</span>" +
        "</div>" +
        '<div class="rc-meter"><i style="width:' + (pct === null ? 0 : pct) + '%"></i></div>' +
      "</div>";

    const pretty = (k) => escapeHtml(k.replace("_", " "));
    if (wrote) {
      html += '<p class="rc-msg rc-ok"><span>Written into <b>' +
              done.map(pretty).join("</b> and <b>") +
              "</b>, unsaved. Review before Update.";
      if (failed.length) {
        html += " Could not find: " + failed.map(pretty).join(", ") + ".";
      }
      html += "</span></p>";
    } else {
      html += '<p class="rc-msg rc-warn"><span>No writable ' +
              writes.map(([k]) => pretty(k)).join(" or ") +
              " field here — copy from below.</span></p>";
    }

    if (data.problem_summary) {
      html += '<h3 class="rc-h">Summary</h3><p class="rc-summary">' +
              escapeHtml(data.problem_summary) + "</p>";
    }

    if (Array.isArray(data.suggested_steps) && data.suggested_steps.length) {
      html += '<h3 class="rc-h">Steps</h3><ol class="rc-list rc-steps">';
      for (const s of data.suggested_steps) html += "<li>" + escapeHtml(s) + "</li>";
      html += "</ol>";
    }

    if (caveats.length) {
      html += '<h3 class="rc-h">Check before sending</h3><ul class="rc-list rc-caveats">';
      for (const c of caveats) html += "<li>" + escapeHtml(c) + "</li>";
      html += "</ul>";
    }

    html += '<h3 class="rc-h">Draft</h3>' +
            '<textarea class="rc-draft" spellcheck="true">' +
            escapeHtml(data.draft_response || "") + "</textarea>";

    const meta = [];
    if (data.model) meta.push(escapeHtml(data.model));
    if (typeof data.latency_ms === "number") meta.push((data.latency_ms / 1000).toFixed(1) + "s");
    if (data.usage && data.usage.total_tokens) meta.push(data.usage.total_tokens + " tokens");
    if (data.request_id) meta.push("draft " + escapeHtml(data.request_id));
    if (meta.length) html += '<p class="rc-meta">' + meta.join(" · ") + "</p>";

    html += evaluationHtml(data.evaluations || {});
    if (data.request_id) html += feedbackHtml();

    const foot =
      '<button type="button" class="rc-btn rc-btn-primary rc-copy">Copy</button>' +
      '<button type="button" class="rc-btn rc-btn-ghost rc-insert">Insert</button>';

    showResultHtml(html, foot);

    const area = $(".rc-draft");
    const flash = (btn, text) => {
      const old = btn.textContent;
      btn.textContent = text;
      btn.classList.add("rc-btn-done");
      setTimeout(() => { btn.textContent = old; btn.classList.remove("rc-btn-done"); }, 1500);
    };

    $(".rc-copy").addEventListener("click", function () {
      const btn = this;
      navigator.clipboard.writeText(area.value).then(
        () => flash(btn, "Copied ✓"),
        () => { area.select(); try { document.execCommand("copy"); flash(btn, "Copied ✓"); } catch (e) {} }
      );
    });

    if (data.request_id) wireFeedback(data.request_id, targetKey);

    $(".rc-insert").addEventListener("click", function () {
      // The textarea holds the reply, so it goes to the customer-visible field
      // in "both" mode and to the chosen field otherwise.
      const replyKey = targetKey === "both" ? "comments" : targetKey;
      const node = findField(replyKey);
      let ok = !!(node && !node.readOnly && setFieldValue(node, area.value));
      if (targetKey === "both") {
        const wn = findField("work_notes");
        if (wn && !wn.readOnly) setFieldValue(wn, analysisText(data));
      }
      flash(this, ok ? "Inserted ✓" : "No field");
    });
  }

  /* ---------- evaluation + feedback ---------- */

  const ASSERT_NAMES = {
    assert_signoff: "sign-off",
    assert_no_placeholder: "no placeholder name",
    assert_no_invented_ticket_number: "no invented ticket number",
    assert_no_promises: "no promised timeline or exception",
    assert_confidence_consistent: "confidence matches caveats",
    assert_summary_short: "summary is 1-2 sentences",
    assert_has_steps: "has steps",
    assert_urls_from_sources: "no URLs outside the sources",
    assert_no_unknown_citations: "no citations to missing sources",
    assert_cites_when_grounded: "cites the RC sources it was given"
  };

  /* The backend's automatic checks for this draft (app/evaluators.py). A
     failed assertion is shown, not hidden: it is something to fix before Update. */
  function evaluationHtml(ev) {
    const checks = ev.assertions || {};
    const keys = Object.keys(checks);
    if (!keys.length) return "";
    const failed = keys.filter((k) => !checks[k]);
    const m = ev.metrics || {};
    const bits = [];
    if (typeof m.conciseness === "number") bits.push("conciseness " + m.conciseness.toFixed(2));
    if (typeof m.answer_relevance_embedding === "number") bits.push("relevance " + m.answer_relevance_embedding.toFixed(2));
    if (typeof m.reply_words === "number") bits.push(m.reply_words + " words");
    return '<p class="rc-eval' + (failed.length ? " rc-eval-fail" : "") + '">' +
      "Automatic checks: " + (keys.length - failed.length) + "/" + keys.length + " passed" +
      (failed.length ? " — failed: " + failed.map((k) => escapeHtml(ASSERT_NAMES[k] || k)).join(", ") : "") +
      (bits.length ? "<br>" + bits.join(" · ") : "") + "</p>";
  }

  function feedbackHtml() {
    const row = (key, label, chips) =>
      '<div class="rc-fb-row" data-key="' + key + '"><span class="rc-fb-label">' + label + "</span>" +
      chips.map(([text, score, value]) =>
        '<button type="button" class="rc-fb-chip" data-score="' + score + '" data-value="' + (value || "") + '">' +
        text + "</button>").join("") + "</div>";
    return '<h3 class="rc-h">Feedback</h3><div class="rc-fb">' +
      row("thumbs", "Helpful?", [["👍", 1], ["👎", 0]]) +
      row("correctness", "Correct?", [["Correct", 1], ["Partly", 0.5], ["Wrong", 0]]) +
      row("usage", "Used it?", [["As is", 1, "used_as_is"], ["Edited", 0.5, "edited"], ["Not used", 0, "not_used"]]) +
      '<textarea class="rc-fb-comment" rows="2" placeholder="Comment (optional)"></textarea>' +
      '<div class="rc-fb-actions">' +
        '<button type="button" class="rc-btn rc-btn-ghost rc-fb-capture" title="Read the reply from the ServiceNow field, as you edited it">Capture what\u2019s in the field</button>' +
        '<button type="button" class="rc-btn rc-btn-primary rc-fb-send" disabled>Send</button>' +
      "</div>" +
      '<p class="rc-fb-status"></p></div>';
  }

  function wireFeedback(requestId, targetKey) {
    const box = $(".rc-fb");
    if (!box) return;
    let correction = "";
    const status = $(".rc-fb-status");
    const send = $(".rc-fb-send");
    const choices = () => Array.from(box.querySelectorAll(".rc-fb-row")).map((row) => {
      const on = row.querySelector(".rc-fb-chip.rc-on");
      return on ? { key: row.dataset.key, score: parseFloat(on.dataset.score), value: on.dataset.value || "" } : null;
    }).filter(Boolean);
    const refresh = () => {
      send.disabled = !(choices().length || $(".rc-fb-comment").value.trim() || correction);
    };

    box.addEventListener("click", (e) => {
      const chip = e.target.closest(".rc-fb-chip");
      if (!chip) return;
      const wasOn = chip.classList.contains("rc-on");
      chip.parentElement.querySelectorAll(".rc-fb-chip").forEach((c) => c.classList.remove("rc-on"));
      if (!wasOn) chip.classList.add("rc-on");
      refresh();
    });
    $(".rc-fb-comment").addEventListener("input", refresh);

    /* The reply as the engineer left it in ServiceNow: comparing it with the
       draft tells the backend how much editing the draft needed. */
    $(".rc-fb-capture").addEventListener("click", () => {
      const node = findField(targetKey === "both" ? "comments" : targetKey);
      correction = node && typeof node.value === "string" ? node.value.trim() : "";
      status.textContent = correction ? "Captured " + correction.length + " characters from the field."
                                      : "No reply found in the field.";
      refresh();
    });

    send.addEventListener("click", async () => {
      const items = choices();
      const comment = $(".rc-fb-comment").value.trim();
      if (comment || correction) items.push({ key: "comment", score: null, value: "", comment, correction });
      send.disabled = true;
      status.textContent = "Sending…";
      let saved = 0;
      for (const item of items) {
        let reply;
        try {
          reply = await chrome.runtime.sendMessage({ type: "FEEDBACK", item: Object.assign({ request_id: requestId }, item) });
        } catch (err) {
          reply = { ok: false, error: "Extension was reloaded — refresh this page." };
        }
        if (!reply || !reply.ok) {
          status.textContent = "Could not save feedback: " + ((reply && reply.error) || "unknown error");
          send.disabled = false;
          return;
        }
        saved++;
      }
      status.textContent = "Thanks — " + saved + " item" + (saved === 1 ? "" : "s") + " saved.";
      send.textContent = "Sent ✓";
    });
  }

  /* ---------- lifecycle ---------- */

  function refresh() {
    renderTicket();
    if ($(".rc-status") && $(".rc-status").dataset.state === "idle") testConnection();
  }

  function hasFields() {
    return !!(findField("short_description") || findField("description"));
  }

  /* True in a child frame whose top frame is same-origin. The top frame reads
     this frame's fields through searchRoots(), so it owns the UI and this
     frame must not build a second panel. */
  function topFrameOwns() {
    if (window === window.top) return false;
    try { return !!window.top.document; } catch (e) { return false; }
  }

  /* We run in every frame (all_frames:true), so exactly one must own the UI.
     A same-origin top frame always does (see topFrameOwns). Otherwise the
     frame holding the ticket fields wins. If no frame has fields -- which
     is what happens when the selectors do not match this ServiceNow UI -- the
     top frame still shows the button, and the panel explains why nothing was
     found. Failing visibly matters more than failing tidily: a missing button
     with no error is impossible to diagnose. */
  function claimUi() {
    let top = null;
    try { top = window.top; } catch (e) { /* cross-origin */ }

    if (hasFields()) {
      try { if (top) top.__rcCopilotOwned = true; } catch (e) {}
      return true;
    }
    return false;
  }

  function mount() {
    if (topFrameOwns()) return;
    if (document.getElementById(PANEL_ID)) {
      if (isOpen()) refresh();
      return;
    }
    if (claimUi()) {
      build();
      if (isOpen()) refresh();
    }
  }

  mount();

  /* Fallback: if after a grace period no frame claimed the UI, the top frame
     shows the button anyway so the failure is visible and reportable. */
  if (window === window.top) {
    setTimeout(function () {
      let owned = false;
      try { owned = !!window.__rcCopilotOwned; } catch (e) {}
      if (!owned && !document.getElementById(PANEL_ID)) build();
    }, 2500);
  }

  function onPageChanged() {
    mount();
    if (isOpen()) renderTicket();
  }

  /* The top frame's observer can't see inside child frames, so a form that
     loads into an iframe after the panel was built would never be re-read.
     Child frames call this instead. Content scripts of one extension share a
     world across same-origin frames, so window.top's copy is reachable. */
  if (window === window.top) window.__rcCopilotPageChanged = onPageChanged;

  /* ServiceNow swaps forms in without a page load. */
  let pending = null;
  const observer = new MutationObserver(function () {
    clearTimeout(pending);
    pending = setTimeout(function () {
      if (topFrameOwns()) {
        try {
          if (typeof window.top.__rcCopilotPageChanged === "function") {
            window.top.__rcCopilotPageChanged();
          }
        } catch (e) { /* top navigated away */ }
        return;
      }
      onPageChanged();
    }, 500);
  });
  observer.observe(document.documentElement, { childList: true, subtree: true });
})();
