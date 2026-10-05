/* RC Copilot frontend — no build step, no dependencies. */
(function () {
  "use strict";

  var el = function (id) { return document.getElementById(id); };

  /* ---------- session ----------
     Stored per-origin. Note that inside the browser-extension side panel this
     is partitioned storage, so signing in there is separate from signing in
     in a normal tab. Expiry here is only for the UI -- the backend signs the
     session token and enforces the deadline itself. */

  var SESSION_KEY = "rc_copilot_session";
  var session = null;      // {token, expires_at}
  var authRequired = true;
  var countdownTimer = null;

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

  function remainingText() {
    if (!session) return "";
    var mins = Math.max(0, Math.round((session.expires_at * 1000 - Date.now()) / 60000));
    if (mins >= 60) {
      var h = Math.floor(mins / 60);
      return "signed in · " + h + "h " + (mins % 60) + "m left";
    }
    return "signed in · " + mins + "m left";
  }

  function renderSessionChip() {
    var box = el("session");
    if (!session) {
      box.hidden = true;
      return;
    }
    el("session-text").textContent = remainingText();
    box.hidden = false;
  }

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

  function setBusy(busy) {
    draftBtn.disabled = busy;
    draftBtn.textContent = busy ? "Drafting…" : "Draft Response";
    document.body.classList.toggle("busy", busy);
  }

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
    el("result-meta").textContent = meta.length
      ? "Generated by " + meta.join(" · ")
      : "";

    fillList(el("suggested-steps"), data.suggested_steps, "No steps returned.");
    fillList(el("caveats"), data.caveats, "The model flagged nothing — check it yourself anyway.");

    el("draft-response").value = data.draft_response || "";
    results.hidden = false;
    results.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  /* ---------- actions ---------- */

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

  async function checkHealth() {
    var dot = el("health-dot");
    var text = el("health-text");
    try {
      var res = await fetch("/api/health");
      var data = await res.json();

      // A backend started with AUTH_DISABLED shouldn't show a sign-in screen.
      if (typeof data.auth_required === "boolean") authRequired = data.auth_required;

      if (res.ok && data.status === "ok" && data.model_available !== false) {
        dot.className = "dot ok";
        text.textContent = "model server up · " + (data.configured_model || "");
        el("health").title = data.vllm_base_url;
      } else {
        dot.className = "dot bad";
        text.textContent = data.status === "ok" ? "model not served" : "model server unreachable";
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
