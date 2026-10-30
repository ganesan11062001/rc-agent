/* Service worker: the only place that talks to the backend.
 *
 * Why here and not in content.js: an MV3 content script runs in the page's
 * origin and IS subject to CORS, so a fetch from service-now.com to
 * northeastern.edu would be blocked whatever headers the backend sends.
 * Service-worker fetches are covered by host_permissions and skip CORS.
 *
 * Also owns the session. rc-copilot issues a signed session token in exchange
 * for the access token from the Slurm job; it expires server-side after a few
 * hours. We hold it in chrome.storage.session, which lives in memory only and
 * is cleared when the browser closes -- deliberately not storage.local.
 */

const CFG_KEY = "oodConfig"; // {baseUrl, accessToken, targetField}
const SESSION_KEY = "session"; // {token, expiresAt}
const TIMEOUT_MS = 180000; // drafting on a busy cluster is slow

/* ---------- config ---------- */

function normaliseBase(url) {
  return String(url || "").trim().replace(/\/+$/, "");
}

/* Saved config with defaults filled in. */
async function getConfig() {
  const stored = await chrome.storage.local.get(CFG_KEY);
  const cfg = stored[CFG_KEY] || {};
  return {
    baseUrl: normaliseBase(cfg.baseUrl),
    accessToken: (cfg.accessToken || "").trim(),
    targetField: cfg.targetField || "work_notes"
  };
}

/* The current session token, or null if there is none or it has expired. */
async function getSession() {
  const stored = await chrome.storage.session.get(SESSION_KEY);
  const s = stored[SESSION_KEY];
  if (!s || !s.token) return null;
  if (s.expiresAt && s.expiresAt * 1000 <= Date.now()) return null;
  return s;
}

/* Store a session ({token, expiresAt}), or clear it with null. */
async function setSession(value) {
  if (value) await chrome.storage.session.set({ [SESSION_KEY]: value });
  else await chrome.storage.session.remove(SESSION_KEY);
}

/* Written by the OOD content script when it finds a live RC Copilot session.
   Only Northeastern hosts (which we hold permission for) are accepted, so a
   page on some other site can't point us at an endpoint of its choosing. */
async function saveConfig(baseUrl, accessToken) {
  const url = normaliseBase(baseUrl);
  let parsed;
  try {
    parsed = new URL(url);
  } catch (err) {
    return { ok: false, error: "Not a valid URL." };
  }
  if (!/(^|\.)(northeastern\.edu|neu\.edu)$/i.test(parsed.hostname)) {
    return { ok: false, error: "Refusing to save a non-Northeastern endpoint." };
  }
  if (!accessToken) return { ok: false, error: "No access token in the session card." };

  const current = await getConfig();
  await chrome.storage.local.set({
    [CFG_KEY]: {
      baseUrl: url,
      accessToken: accessToken,
      // Never silently change where the draft gets written.
      targetField: current.targetField || "work_notes"
    }
  });
  return { ok: true };
}

/* ---------- error wording ---------- */

/* Turn a non-2xx answer into a message that says what to do. Most of these
   statuses come from OOD's proxy, not the backend, so they usually mean the
   job ended, is still starting, or the OOD login expired. */
function describeHttpFailure(status, bodyText) {
  if (status === 404) {
    return (
      "OOD returned 404: the session has most likely ended, or the extension " +
      "still has an old session's address. Launch a new RC Copilot session and " +
      "open My Interactive Sessions. (With serve_stack.sbatch: paste the new " +
      "URL and token into the popup.)"
    );
  }
  if (status === 502 || status === 503 || status === 504) {
    return (
      "OOD reached the node but got no answer from the app (" + status + "). " +
      "Either the job has ended, or it is still starting up -- a model can " +
      "take a few minutes to load. Check the session in Open OnDemand."
    );
  }
  if (status === 401 || status === 403) {
    return (
      "OOD rejected the request (" + status + "). Your OOD session has " +
      "probably expired — open OOD in a tab, sign in, then try again."
    );
  }
  let extra = "";
  try {
    const parsed = JSON.parse(bodyText);
    extra = parsed.error || parsed.detail || "";
  } catch (err) {
    extra = (bodyText || "").slice(0, 200);
  }
  return "Backend returned HTTP " + status + ". " + extra;
}

/* ---------- raw request ---------- */

/* One fetch to the backend, always resolving to {ok, status, data | error}
   and never throwing, so callers only branch on ok. */
async function request(path, options) {
  const cfg = await getConfig();
  if (!cfg.baseUrl) {
    return { ok: false, error: "No backend URL saved. Open the extension popup first." };
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);

  let response;
  try {
    response = await fetch(cfg.baseUrl + path, {
      method: (options && options.method) || "GET",
      // OOD is behind university SSO; without this we arrive unauthenticated
      // and get handed the login page.
      credentials: "include",
      cache: "no-store",
      headers: Object.assign({ Accept: "application/json" }, (options && options.headers) || {}),
      body: (options && options.body) || undefined,
      signal: controller.signal
    });
  } catch (err) {
    clearTimeout(timer);
    if (err.name === "AbortError") {
      return { ok: false, error: "Timed out after " + TIMEOUT_MS / 1000 + "s. The model may still be loading." };
    }
    return {
      ok: false,
      error:
        "Could not reach " + cfg.baseUrl + ". Check the URL, and that you're " +
        "on the campus network or VPN. (" + err.message + ")"
    };
  }
  clearTimeout(timer);

  const contentType = response.headers.get("content-type") || "";

  /* HTML is never the backend. Two very different things send it: OOD's own
     error page when nothing listens on the session's port yet ("Failed to
     connect to d4054:24980" -- a new session starts its backend a minute or
     two after the card says Running), and SSO's login page. Telling the
     first one to sign in sends people the wrong way, so tell them apart. */
  if (contentType.includes("text/html")) {
    const html = await response.text().catch(() => "");
    const down = html.match(/Failed to connect to\s+([\w.-]+:\d+)/i);
    if (down || response.status >= 500) {
      return {
        ok: false,
        status: response.status,
        backendDown: true,
        error:
          "Nothing is answering at " + (down ? down[1] : "the session's address") +
          " yet. A new session starts its backend a minute or two after the card " +
          "says Running. If this lasts, open output.log from the session card."
      };
    }
    return {
      ok: false,
      status: response.status,
      needsOodLogin: true,
      error:
        "Got the OOD login page instead of JSON. Open " +
        "https://ood.explorer.northeastern.edu/pun/sys/dashboard in a tab, " +
        "sign in, then retry."
    };
  }

  const text = await response.text().catch(() => "");

  if (!response.ok) {
    /* An older backend answered /api/health with 503 when the model was not
       loaded yet. That is indistinguishable from OOD's own 503 ("nothing is
       listening"), and reporting a dead job while the backend is answering is
       badly misleading. If the body is our JSON, trust it over the status. */
    try {
      const body = JSON.parse(text);
      if (body && typeof body.status === "string" && "vllm_base_url" in body) {
        return { ok: true, status: response.status, data: body };
      }
    } catch (err) {
      /* not our JSON -- fall through to the generic message */
    }
    return {
      ok: false,
      status: response.status,
      error: describeHttpFailure(response.status, text)
    };
  }

  try {
    return { ok: true, status: response.status, data: JSON.parse(text) };
  } catch (err) {
    return { ok: false, error: "Backend sent malformed JSON." };
  }
}

/* ---------- sign-in ---------- */

/* Exchange the saved access token for a session token (POST /api/login). */
async function signIn() {
  const cfg = await getConfig();
  if (!cfg.accessToken) {
    return { ok: false, error: "No access token saved. With your RC Copilot session running, open My Interactive Sessions in OOD. (With serve_stack.sbatch: paste the output of cat ~/.rc_copilot/token into the popup.)" };
  }

  const result = await request("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ access_token: cfg.accessToken })
  });

  if (!result.ok) {
    if (result.status === 401) {
      return {
        ok: false,
        error: "The backend rejected the access token; it changes with every session. Open My Interactive Sessions in OOD again so the extension picks up the new one. (With serve_stack.sbatch: re-read ~/.rc_copilot/token.)"
      };
    }
    return result;
  }

  await setSession({ token: result.data.session_token, expiresAt: result.data.expires_at });
  return { ok: true, expiresAt: result.data.expires_at };
}

/* Run `fn` with a valid session, signing in first and retrying once on 401. */
async function withSession(fn) {
  let session = await getSession();

  if (!session) {
    const login = await signIn();
    if (!login.ok) return login;
    session = await getSession();
  }

  let result = await fn(session.token);

  if (result.status === 401) {
    await setSession(null);
    const login = await signIn();
    if (!login.ok) return login;
    session = await getSession();
    result = await fn(session.token);
  }

  return result;
}

/* ---------- the operations ---------- */

/* GET /api/health. ok only when the model is serving; while a fresh session
   is still loading it returns starting:true with the startup progress. */
async function ping() {
  const result = await request("/api/health");
  if (!result.ok) return result;

  const data = result.data || {};
  const startup = data.startup || null;
  if (data.status !== "ok") {
    // A fresh session: the backend is up, vLLM is still loading. Report the
    // progress rather than an error, unless the load has actually failed.
    if (startup && !(startup.vllm && startup.vllm.failed)) {
      return { ok: false, starting: true, startup: startup, data: data };
    }
    return {
      ok: false,
      startup: startup,
      error:
        "rc-copilot is up, but it can't reach vLLM (" +
        ((startup && startup.vllm && startup.vllm.detail) || data.detail || "no detail") + ")."
    };
  }
  return { ok: true, data: data, startup: startup };
}

/* POST /api/draft with the ticket as read from the page. */
async function draft(ticketText, ticketNumber, extraInstructions, signoff) {
  if (!ticketText || !ticketText.trim()) {
    return { ok: false, error: "No ticket text to send." };
  }

  const payload = { ticket_text: ticketText };
  // Optional: the backend files captures under this, and falls back to a regex
  // over the text if the page didn't expose a number field.
  if (ticketNumber) payload.ticket_number = ticketNumber;
  // Free-text guidance the RC member typed before drafting.
  if (extraInstructions) payload.extra_instructions = extraInstructions;
  // The engineer's own sign-off (popup), used exactly as written.
  if (signoff && signoff.trim()) payload.signoff = signoff.slice(0, 300);

  return withSession((sessionToken) =>
    request("/api/draft", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + sessionToken
      },
      body: JSON.stringify(payload)
    })
  );
}

/* ---------- feedback ---------- */

/* One feedback record for a draft (see backend /api/feedback): a score under
   a key, optionally a categorical value, a comment, and the reply as sent. */
async function feedback(item) {
  if (!item || !item.request_id || !item.key) {
    return { ok: false, error: "Nothing to send." };
  }
  const body = {
    request_id: item.request_id,
    key: item.key,
    score: typeof item.score === "number" ? item.score : null,
    value: item.value || "",
    comment: item.comment || "",
    correction: item.correction || ""
  };
  return withSession((sessionToken) =>
    request("/api/feedback", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + sessionToken
      },
      body: JSON.stringify(body)
    })
  );
}

/* ---------- message plumbing ---------- */

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || typeof message.type !== "string") return false;

  const handlers = {
    PING: () => ping(),
    SIGN_IN: () => signIn(),
    DRAFT: () => draft(message.ticketText, message.ticketNumber, message.extraInstructions, message.signoff),
    FEEDBACK: () => feedback(message.item),
    GET_CONFIG: () => getConfig().then((cfg) => ({ ok: true, data: cfg })),
    SAVE_CONFIG: () => saveConfig(message.baseUrl, message.accessToken)
  };

  const handler = handlers[message.type];
  if (!handler) return false;

  handler()
    .then(sendResponse)
    .catch((err) => sendResponse({ ok: false, error: "Unexpected: " + err.message }));
  return true; // keep the channel open for the async reply
});

/* A new job means a new token; drop any session signed by the old one. */
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes[CFG_KEY]) setSession(null);
});
