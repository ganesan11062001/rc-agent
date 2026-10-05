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

  /* Walk the DOM including shadow roots. Next Experience needs this; Classic
     is unaffected because it simply has no shadow roots to descend into. */
  function deepCollect(root, out, depth) {
    if (depth > 12) return out; // guard against pathological nesting
    let nodes;
    try {
      nodes = root.querySelectorAll("input, textarea, [contenteditable='true']");
    } catch (err) {
      return out;
    }
    for (const node of nodes) out.push(node);

    let all;
    try {
      all = root.querySelectorAll("*");
    } catch (err) {
      return out;
    }
    for (const el of all) {
      if (el.shadowRoot) deepCollect(el.shadowRoot, out, depth + 1);
    }
    return out;
  }

  function allFields() {
    return deepCollect(document, [], 0).filter(function (node) {
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
      const node = document.querySelector(selector);
      if (node && node.innerText && node.innerText.trim()) {
        return node.innerText.trim().slice(0, MAX_ACTIVITY_CHARS);
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

    const proto =
      node instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
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

  async function testConnection() {
    setStatus("checking", "Checking…", "contacting the cluster", "…");

    const ping = await chrome.runtime.sendMessage({ type: "PING" }).catch(() => null);
    if (!ping || !ping.ok) {
      setStatus("bad", "Not connected", (ping && ping.error) || "no reply", "Retry");
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
    if (meta.length) html += '<p class="rc-meta">' + meta.join(" · ") + "</p>";

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

  /* ---------- lifecycle ---------- */

  function refresh() {
    renderTicket();
    if ($(".rc-status") && $(".rc-status").dataset.state === "idle") testConnection();
  }

  function hasFields() {
    return !!(findField("short_description") || findField("description"));
  }

  /* We run in every frame (all_frames:true), so exactly one must own the UI.
     The frame holding the ticket fields wins. If no frame has fields -- which
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

  /* ServiceNow swaps forms in without a page load. */
  let pending = null;
  const observer = new MutationObserver(function () {
    clearTimeout(pending);
    pending = setTimeout(function () {
      mount();
      if (isOpen()) renderTicket();
    }, 500);
  });
  observer.observe(document.documentElement, { childList: true, subtree: true });
})();
