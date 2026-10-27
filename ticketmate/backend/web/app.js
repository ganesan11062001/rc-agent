/* RC Copilot frontend — no build step, no dependencies. */
(function () {
  "use strict";

  var el = function (id) { return document.getElementById(id); };

  /* ---------- session ----------
     Stored per-origin in localStorage, so a sign-in survives a reload. Expiry here
     is only for the UI -- the backend signs the session token and enforces the
     deadline itself. */

  var SESSION_KEY = "rc_copilot_session";
  var session = null;      // {token, expires_at}
  var authRequired = true;
  var countdownTimer = null;

  /* The stored session, or null if missing, malformed or expired. */
  function loadSession() {
    try {
      var raw = window.localStorage.getItem(SESSION_KEY);
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      if (!parsed.token || !parsed.expires_at) return null;
      if (parsed.expires_at * 1000 <= Date.now()) return null;
      return parsed;
    } catch (err) {
      return null;
    }
  }

  /* Keep the session in memory and localStorage; null signs out. */
  function saveSession(value) {
    session = value;
    try {
      if (value) window.localStorage.setItem(SESSION_KEY, JSON.stringify(value));
      else window.localStorage.removeItem(SESSION_KEY);
    } catch (err) { /* private mode — session just won't survive reload */ }
  }

  function authHeaders() {
    return session ? { Authorization: "Bearer " + session.token } : {};
  }

  /* "signed in · 1h 5m left" for the session chip. */
  function remainingText() {
    if (!session) return "";
    var mins = Math.max(0, Math.round((session.expires_at * 1000 - Date.now()) / 60000));
    if (mins >= 60) {
      var h = Math.floor(mins / 60);
      return "signed in · " + h + "h " + (mins % 60) + "m left";
    }
    return "signed in · " + mins + "m left";
  }

  /* The header chip showing time left; hidden when signed out. */
  function renderSessionChip() {
    var box = el("session");
    if (!session) {
      box.hidden = true;
      return;
    }
    el("session-text").textContent = remainingText();
    box.hidden = false;
  }

  /* Show the sign-in screen, with an error message if given. */
  function showLogin(message) {
    el("login-main").hidden = false;
    el("app-main").hidden = true;
    renderSessionChip();

    var err = el("login-error");
    if (message) {
      err.textContent = message;
      err.hidden = false;
    } else {
      err.hidden = true;
    }
    el("access-token").focus();
  }

  function showApp() {
    el("login-main").hidden = true;
    el("app-main").hidden = false;
    renderSessionChip();
  }

  /* Expire the UI at the same moment the server will start rejecting us. */
  function startCountdown() {
    if (countdownTimer) clearInterval(countdownTimer);
    countdownTimer = setInterval(function () {
      if (!session) return;
      if (session.expires_at * 1000 <= Date.now()) {
        saveSession(null);
        showLogin("Your session expired. Sign in again.");
        return;
      }
      renderSessionChip();
    }, 30000);
  }

  function signOut(message) {
    saveSession(null);
    el("access-token").value = "";
    showLogin(message);
  }

  /* Exchange the pasted access token for a session token (POST /api/login). */
  async function signIn() {
    var input = el("access-token");
    var value = input.value.trim();
    if (!value) {
      showLogin("Paste the access token from the server console.");
      return;
    }

    var btn = el("login-btn");
    btn.disabled = true;
    el("login-hint").textContent = "Checking…";

    try {
      var res = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ access_token: value })
      });
      var payload = await res.json().catch(function () { return null; });

      if (!res.ok) {
        showLogin((payload && payload.error) || "Sign-in failed.");
        return;
      }

      saveSession({ token: payload.session_token, expires_at: payload.expires_at });
      input.value = "";
      el("login-error").hidden = true;
      showApp();
      startCountdown();
    } catch (err) {
      showLogin("Couldn't reach the backend. Is uvicorn still running?");
    } finally {
      btn.disabled = false;
      el("login-hint").textContent = "";
    }
  }

  var ticketText = el("ticket-text");
  var extraInstructions = el("extra-instructions");
  var draftBtn = el("draft-btn");
  var clearBtn = el("clear-btn");
  var copyBtn = el("copy-btn");
  var charCount = el("char-count");
  var statusBox = el("status");
  var results = el("results");

  /* ---------- helpers ---------- */

  /* The status box above the results: a bold message and optional detail. */
  function showStatus(kind, message, detail) {
    statusBox.className = "status " + kind;
    statusBox.innerHTML = "";

    var strong = document.createElement("strong");
    strong.textContent = message;
    statusBox.appendChild(strong);

    if (detail) {
      var p = document.createElement("p");
      p.textContent = detail;
      statusBox.appendChild(p);
    }
    statusBox.hidden = false;
  }

  function hideStatus() {
    statusBox.hidden = true;
  }

  /* Disable the draft button and mark the page busy while a draft runs. */
  function setBusy(busy) {
    draftBtn.disabled = busy;
    draftBtn.textContent = busy ? "Drafting…" : "Draft Response";
    document.body.classList.toggle("busy", busy);
  }

  /* Replace a list's items with text (never HTML), or one muted placeholder. */
  function fillList(node, items, emptyText) {
    node.innerHTML = "";
    if (!items || items.length === 0) {
      var li = document.createElement("li");
      li.className = "muted";
      li.textContent = emptyText;
      node.appendChild(li);
      return;
    }
    items.forEach(function (item) {
      var li = document.createElement("li");
      li.textContent = String(item);
      node.appendChild(li);
    });
  }

  /* Trust the server's label, but fall back to the same thresholds. */
  function labelFor(score, given) {
    if (given) return given;
    if (score >= 0.8) return "high";
    if (score >= 0.6) return "medium";
    return "low";
  }

  /* ---------- rendering ---------- */

  /* Show a /api/draft response: summary, confidence, steps, caveats, lookups,
     the draft itself and the feedback card. */
  function render(data) {
    el("problem-summary").textContent = data.problem_summary || "(none returned)";

    var label = labelFor(data.confidence, data.confidence_label);
    var badge = el("confidence-badge");
    badge.className = "badge badge-" + label;
    badge.textContent =
      label + " confidence · " + Math.round((data.confidence || 0) * 100) + "%";

    var meta = [];
    if (data.model) meta.push(data.model);
    if (typeof data.latency_ms === "number") {
      meta.push((data.latency_ms / 1000).toFixed(1) + "s");
    }
    var c = data.context || {};
    if (c.raw_chars) {
      var removed = [];
      if (c.notifications_removed) removed.push(c.notifications_removed + " notification emails");
      if (c.addresses_removed) removed.push(c.addresses_removed + " addresses");
      meta.push("ticket " + c.raw_chars.toLocaleString() + " → " + (c.chars || 0).toLocaleString() + " chars" +
                (removed.length ? " (removed " + removed.join(", ") + ")" : "") +
                (c.entries_omitted ? ", " + c.entries_omitted + " middle entries left out to fit" : ""));
    }
    if (Array.isArray(c.playbooks) && c.playbooks.length) meta.push("workflow: " + c.playbooks.join(", "));
    el("result-meta").textContent = meta.length
      ? "Generated by " + meta.join(" · ")
      : "";

    fillList(el("suggested-steps"), data.suggested_steps, "No steps returned.");
    fillList(el("caveats"), data.caveats, "The model flagged nothing — check it yourself anyway.");
    renderLookups(data.lookups);

    el("draft-response").value = data.draft_response || "";
    renderFeedback(data);
    results.hidden = false;
    results.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // Read-only cluster lookups the backend ran for this ticket (none: no card).
  function renderLookups(items) {
    var list = el("lookups");
    list.textContent = "";
    items = Array.isArray(items) ? items : [];
    el("lookups-card").hidden = items.length === 0;
    items.forEach(function (l) {
      var li = document.createElement("li");
      if (!l.ok) li.className = "lookup-miss";
      var code = document.createElement("code");
      code.textContent = l.command;
      li.appendChild(code);
      li.appendChild(document.createTextNode(" " + l.summary));
      list.appendChild(li);
    });
  }

  /* ---------- evaluation + feedback ---------- */

  var currentRequestId = "";

  var ASSERT_NAMES = {
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

  /* Reset the feedback card for a new draft and summarise its automatic
     checks and metrics. */
  function renderFeedback(data) {
    currentRequestId = data.request_id || "";
    el("fb-id").textContent = currentRequestId ? "draft " + currentRequestId : "";
    var ev = data.evaluations || {};
    var checks = ev.assertions || {};
    var keys = Object.keys(checks);
    var failed = keys.filter(function (k) { return !checks[k]; });
    var m = ev.metrics || {};
    var box = el("eval-summary");
    box.className = "eval" + (failed.length ? " has-fail" : "");
    var parts = [];
    if (keys.length) {
      parts.push("Automatic checks: " + (keys.length - failed.length) + " of " + keys.length + " passed" +
        (failed.length ? " — failed: " + failed.map(function (k) { return ASSERT_NAMES[k] || k; }).join(", ") : "") + ".");
    }
    var ms = [];
    if (typeof m.conciseness === "number") ms.push("conciseness " + m.conciseness.toFixed(2));
    if (typeof m.answer_relevance_embedding === "number") ms.push("relevance " + m.answer_relevance_embedding.toFixed(2));
    if (typeof m.reply_words === "number") ms.push(m.reply_words + " words");
    if (typeof m.hallucinated_urls === "number" && m.hallucinated_urls) ms.push(m.hallucinated_urls + " invented URL(s) removed");
    if (data.usage && data.usage.total_tokens) ms.push(data.usage.total_tokens + " tokens");
    if (ms.length) parts.push(ms.join(" · "));
    box.textContent = parts.join("  ");
    box.hidden = !parts.length;

    Array.prototype.forEach.call(document.querySelectorAll("#feedback-card .fb-chip"), function (c) {
      c.classList.remove("on");
    });
    el("fb-comment").value = "";
    el("fb-correction").value = "";
    el("fb-status").textContent = "";
    el("fb-submit").disabled = true;
    el("fb-submit").textContent = "Send feedback";
  }

  /* The selected chip in each feedback row, as {key, score, value}. */
  function feedbackChoices() {
    var out = [];
    Array.prototype.forEach.call(document.querySelectorAll("#feedback-card .fb-row"), function (row) {
      var on = row.querySelector(".fb-chip.on");
      if (on) {
        out.push({ key: row.getAttribute("data-key"), score: parseFloat(on.getAttribute("data-score")),
                   value: on.getAttribute("data-value") || "" });
      }
    });
    return out;
  }

  function refreshFeedbackButton() {
    var any = feedbackChoices().length || el("fb-comment").value.trim() || el("fb-correction").value.trim();
    el("fb-submit").disabled = !any || !currentRequestId;
  }

  document.querySelector("#feedback-card").addEventListener("click", function (e) {
    var chip = e.target.closest(".fb-chip");
    if (!chip) return;
    var row = chip.closest(".fb-row");
    var wasOn = chip.classList.contains("on");
    Array.prototype.forEach.call(row.querySelectorAll(".fb-chip"), function (c) { c.classList.remove("on"); });
    if (!wasOn) chip.classList.add("on");
    refreshFeedbackButton();
  });
  el("fb-comment").addEventListener("input", refreshFeedbackButton);
  el("fb-correction").addEventListener("input", refreshFeedbackButton);

  /* POST one /api/feedback record per chosen item, plus one for the comment
     and correction, all under the current draft's request_id. */
  async function sendFeedback() {
    var items = feedbackChoices();
    var comment = el("fb-comment").value.trim();
    var correction = el("fb-correction").value.trim();
    if (comment || correction) items.push({ key: "comment", score: null, value: "", comment: comment, correction: correction });
    if (!items.length || !currentRequestId) return;

    var btn = el("fb-submit");
    btn.disabled = true;
    el("fb-status").textContent = "Sending…";
    var headers = { "Content-Type": "application/json" };
    var extra = authHeaders();
    for (var k in extra) headers[k] = extra[k];
    var saved = 0;
    try {
      for (var i = 0; i < items.length; i++) {
        var body = Object.assign({ request_id: currentRequestId, comment: "", correction: "" }, items[i]);
        var res = await fetch("/api/feedback", { method: "POST", headers: headers, body: JSON.stringify(body) });
        if (res.status === 401) { signOut("Your session expired. Sign in again."); return; }
        if (!res.ok) {
          var p = await res.json().catch(function () { return null; });
          throw new Error((p && p.error) || "HTTP " + res.status);
        }
        saved++;
      }
      el("fb-status").textContent = "Thanks — " + saved + " item" + (saved === 1 ? "" : "s") + " saved with draft " + currentRequestId + ".";
      btn.textContent = "Sent ✓";
    } catch (err) {
      el("fb-status").textContent = "Could not save feedback: " + err.message;
      btn.disabled = false;
    }
  }
  el("fb-submit").addEventListener("click", sendFeedback);

  /* ---------- actions ---------- */

  /* Send the pasted ticket for a draft and render the result. */
  async function requestDraft() {
    var text = ticketText.value.trim();
    if (!text) {
      showStatus("warn", "Paste some ticket text first.");
      ticketText.focus();
      return;
    }

    setBusy(true);
    showStatus("info", "Asking the model…", "This can take up to a minute on a busy cluster.");
    results.hidden = true;

    try {
      var headers = { "Content-Type": "application/json" };
      var extra = authHeaders();
      for (var k in extra) headers[k] = extra[k];

      var res = await fetch("/api/draft", {
        method: "POST",
        headers: headers,
        /* Mirrors what the extension sends (extension/background.js draft()),
           so the prompt can be tuned here against the real code path. */
        body: JSON.stringify({
          ticket_text: text,
          extra_instructions: (extraInstructions.value || "").trim()
        })
      });

      var payload = null;
      try {
        payload = await res.json();
      } catch (e) {
        payload = null;
      }

      /* The server is the authority on expiry, not our countdown. */
      if (res.status === 401) {
        hideStatus();
        signOut((payload && payload.error) || "Your session expired. Sign in again.");
        return;
      }

      if (!res.ok) {
        var msg = (payload && payload.error) || "The server returned an error (HTTP " + res.status + ").";
        showStatus("error", msg, payload && payload.detail ? payload.detail : null);
        return;
      }

      hideStatus();
      render(payload);
    } catch (err) {
      showStatus(
        "error",
        "Couldn't reach the RC Copilot server.",
        "Is uvicorn still running? (" + err.message + ")"
      );
    } finally {
      setBusy(false);
    }
  }

  /* Copy the draft, falling back to execCommand where the Clipboard API is
     unavailable. */
  async function copyDraft() {
    var box = el("draft-response");
    if (!box.value) return;

    var done = function () {
      var original = copyBtn.textContent;
      copyBtn.textContent = "Copied ✓";
      copyBtn.classList.add("copied");
      setTimeout(function () {
        copyBtn.textContent = original;
        copyBtn.classList.remove("copied");
      }, 1600);
    };

    try {
      // navigator.clipboard needs a secure context (https or localhost).
      await navigator.clipboard.writeText(box.value);
      done();
    } catch (err) {
      box.select();
      box.setSelectionRange(0, box.value.length);
      try {
        document.execCommand("copy");
        done();
      } catch (e) {
        showStatus("warn", "Couldn't copy automatically.", "The draft is selected — press Ctrl+C.");
      }
    }
  }

  var healthTimer = null;

  /* The header health indicator from /api/health. While the session starts
     (or the graph loads) it polls every 3 s; otherwise the 30 s interval in
     boot refreshes it. Also learns whether sign-in is required. */
  async function checkHealth() {
    var dot = el("health-dot");
    var text = el("health-text");
    try {
      var res = await fetch("/api/health");
      var data = await res.json();

      // A backend started with AUTH_DISABLED shouldn't show a sign-in screen.
      if (typeof data.auth_required === "boolean") authRequired = data.auth_required;

      var st = data.startup || null;
      var kg = (st && st.knowledge_graph) || {};
      var kgLoading = st && !st.ready && kg.stage && kg.stage !== "disabled" && kg.stage !== "ready";
      var vl = (st && st.vllm) || {};
      clearTimeout(healthTimer);
      if (data.status !== "ok" && st && !vl.failed) {
        // Fresh session: show what is loading, and check again soon.
        dot.className = "dot busy";
        text.textContent = "starting · model " + Math.round(vl.pct || 0) + "% (" + (vl.label || "loading") + ")" +
          (kgLoading ? " · graph " + Math.round(kg.pct || 0) + "% (" + kg.label + ")" : "") +
          " · " + Math.round(st.elapsed_s || 0) + "s";
        el("health").title = vl.detail || "";
        healthTimer = setTimeout(checkHealth, 3000);
      } else if (res.ok && data.status === "ok" && data.model_available !== false) {
        dot.className = kgLoading ? "dot busy" : "dot ok";
        text.textContent = "model server up · " + (data.configured_model || "") +
          (kgLoading ? " · graph loading " + Math.round(kg.pct || 0) + "% (drafts won't cite RC pages yet)" : "");
        el("health").title = data.vllm_base_url;
        if (kgLoading) healthTimer = setTimeout(checkHealth, 3000);
      } else {
        dot.className = "dot bad";
        text.textContent = data.status === "ok" ? "model not served" :
          vl.failed ? "model failed to start · " + (vl.detail || "") : "model server unreachable";
        el("health").title = (data.detail || "") + " (" + (data.vllm_base_url || "") + ")";
      }
    } catch (err) {
      dot.className = "dot bad";
      text.textContent = "backend unreachable";
    }
  }

  /* ---------- wiring ---------- */

  draftBtn.addEventListener("click", requestDraft);
  copyBtn.addEventListener("click", copyDraft);

  clearBtn.addEventListener("click", function () {
    ticketText.value = "";
    extraInstructions.value = "";
    charCount.textContent = "0 characters";
    results.hidden = true;
    hideStatus();
    ticketText.focus();
  });

  ticketText.addEventListener("input", function () {
    charCount.textContent = ticketText.value.length.toLocaleString() + " characters";
  });

  // Ctrl/Cmd+Enter submits from the textarea.
  ticketText.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      requestDraft();
    }
  });

  el("login-btn").addEventListener("click", signIn);
  el("signout-btn").addEventListener("click", function () { signOut(null); });

  el("access-token").addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      e.preventDefault();
      signIn();
    }
  });

  /* ---------- boot ---------- */

  /* Show the app or the sign-in screen, depending on auth and a stored session. */
  async function start() {
    // Learn whether auth is on before deciding what to show.
    await checkHealth();

    if (!authRequired) {
      showApp();
      return;
    }

    session = loadSession();
    if (session) {
      showApp();
      startCountdown();
    } else {
      showLogin(null);
    }
  }

  start();
  setInterval(checkHealth, 30000);
})();
