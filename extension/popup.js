(function () {
  "use strict";

  const CFG_KEY = "oodConfig";
  const el = (id) => document.getElementById(id);
  const status = el("status");

  function say(kind, message) {
    status.className = "toast " + kind;
    status.textContent = message;
    status.hidden = false;
  }
  const clearSay = () => { status.hidden = true; };

  function normalise(url) {
    return String(url || "").trim().replace(/\/+$/, "");
  }

  /* Never show a whole token; enough to tell two sessions apart. */
  function maskToken(token) {
    if (!token) return "—";
    return token.length <= 8 ? "••••" : token.slice(0, 4) + "…" + token.slice(-4);
  }

  /* The node name is the interesting part of the URL, so surface just that. */
  function nodeFromUrl(url) {
    const m = String(url || "").match(/\/r?node\/([^/]+)\//);
    return m ? m[1] : "—";
  }

  function setConn(state, title, sub) {
    el("conn").dataset.state = state;
    el("conn-title").textContent = title;
    el("conn-sub").textContent = sub || "";
  }

  function renderTargetWarning() {
    const warn = el("target-warning");
    const v = el("target-field").value;
    if (v === "comments") {
      warn.textContent = "Visible to whoever filed the ticket. The draft lands unsaved, but one wrong Update sends it.";
      warn.className = "note danger";
    } else if (v === "both") {
      warn.textContent = "Summary, steps and caveats go to work notes; the reply goes to additional comments — which is customer visible.";
      warn.className = "note danger";
    } else {
      warn.textContent = "Internal only, so a mis-click can't reach the researcher.";
      warn.className = "note";
    }
  }

  async function render() {
    const stored = await chrome.storage.local.get(CFG_KEY);
    const cfg = stored[CFG_KEY] || {};
    const configured = Boolean(cfg.baseUrl && cfg.accessToken);

    el("unconfigured").hidden = configured;
    el("test").disabled = !configured;
    el("facts").hidden = !configured;

    if (configured) {
      setConn("warn", "Configured", "not checked yet — hit Test connection");
      el("fact-node").textContent = nodeFromUrl(cfg.baseUrl);
      el("fact-node").title = cfg.baseUrl;
      el("fact-model").textContent = "—";
      el("fact-session").textContent = maskToken(cfg.accessToken);
      el("fact-session").title = "access token (masked)";
      el("ood-url").value = cfg.baseUrl;
      el("access-token").value = cfg.accessToken;
    } else {
      setConn("bad", "No session found", "launch RC Copilot in Open OnDemand");
      el("manual").open = true;
    }

    if (cfg.targetField) el("target-field").value = cfg.targetField;
    renderTargetWarning();
  }

  /* Target field is a local preference; save it without disturbing the rest. */
  el("target-field").addEventListener("change", async () => {
    renderTargetWarning();
    const stored = await chrome.storage.local.get(CFG_KEY);
    const cfg = stored[CFG_KEY] || {};
    cfg.targetField = el("target-field").value;
    await chrome.storage.local.set({ [CFG_KEY]: cfg });
  });

  el("save").addEventListener("click", async () => {
    const url = normalise(el("ood-url").value);
    const token = el("access-token").value.trim();

    let parsed;
    try {
      parsed = new URL(url);
    } catch (err) {
      return say("error", "That isn't a valid URL.");
    }
    if (parsed.protocol !== "https:") return say("error", "The URL must start with https://");
    if (!/(^|\.)(northeastern\.edu|neu\.edu)$/i.test(parsed.hostname)) {
      return say("error", "Host '" + parsed.hostname + "' isn't in this extension's permissions.");
    }
    if (!token) return say("error", "Paste the access token from the OOD session card.");

    const stored = await chrome.storage.local.get(CFG_KEY);
    const cfg = stored[CFG_KEY] || {};
    await chrome.storage.local.set({
      [CFG_KEY]: { baseUrl: url, accessToken: token, targetField: cfg.targetField || "work_notes" }
    });
    say("ok", "Saved.");
    render();
  });

  el("test").addEventListener("click", async () => {
    const btn = el("test");
    btn.classList.add("busy");
    clearSay();
    setConn("loading", "Checking…", "contacting the cluster");

    try {
      const ping = await chrome.runtime.sendMessage({ type: "PING" });
      if (ping && ping.starting) {
        // A fresh session still loading; the panel on the ticket page shows the details.
        const st = ping.startup || {};
        const v = st.vllm || {};
        const kg = st.knowledge_graph || {};
        setConn("loading", "Starting up…",
          "model " + Math.round(Number(v.pct) || 0) + "% · " + (v.label || "loading"));
        return say("info", "The session is still starting (" + Math.round(st.elapsed_s || 0) + "s so far). " +
          "Model: " + (v.label || "loading") + (v.detail ? " (" + v.detail + ")" : "") + ". " +
          (kg.stage && kg.stage !== "disabled" ? "Knowledge graph: " + (kg.label || kg.stage) + ". " : "") +
          "Test again in a minute.");
      }
      if (!ping || !ping.ok) {
        setConn("bad", "Can't reach it", "see the message below");
        return say("error", (ping && ping.error) || "No reply from the service worker.");
      }

      // Reaching the backend isn't enough — prove the token works too.
      const login = await chrome.runtime.sendMessage({ type: "SIGN_IN" });
      if (!login || !login.ok) {
        setConn("warn", "Reachable, not signed in", "the token was rejected");
        return say("error", (login && login.error) || "Sign-in failed.");
      }

      const model = (ping.data && ping.data.configured_model) || "?";
      const mins = Math.max(0, Math.round((login.expiresAt * 1000 - Date.now()) / 60000));
      const pretty = mins >= 60
        ? Math.floor(mins / 60) + "h " + (mins % 60) + "m"
        : mins + " min";

      setConn("ok", "Connected", "signed in and ready");
      el("fact-model").textContent = model;
      el("fact-model").title = model;
      el("fact-session").textContent = pretty;
      el("fact-session").title = "time left on this sign-in";
      say("ok", "Ready. Open a ticket and click Draft reply.");
    } finally {
      btn.classList.remove("busy");
    }
  });

  // Reflect auto-configuration that happens while the popup is open.
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes[CFG_KEY]) render();
  });

  chrome.runtime.getManifest && (el("version").textContent = "v" + chrome.runtime.getManifest().version);
  render();
})();
