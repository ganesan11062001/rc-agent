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

async function getConfig() {
  const stored = await chrome.storage.local.get(CFG_KEY);
  const cfg = stored[CFG_KEY] || {};
  return {
    baseUrl: normaliseBase(cfg.baseUrl),
    accessToken: (cfg.accessToken || "").trim(),
    targetField: cfg.targetField || "work_notes"
  };
}

async function getSession() {
  const stored = await chrome.storage.session.get(SESSION_KEY);
  const s = stored[SESSION_KEY];
  if (!s || !s.token) return null;
  if (s.expiresAt && s.expiresAt * 1000 <= Date.now()) return null;
  return s;
}

async function setSession(value) {
  if (value) await chrome.storage.session.set({ [SESSION_KEY]: value });
  else await chrome.storage.session.remove(SESSION_KEY);
}

/* Written by the OOD content script when it finds a live RC Copilot session.
   Only loopback-equivalent hosts we hold permission for are accepted, so a
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

function describeHttpFailure(status, bodyText) {
  if (status === 404) {
    return (
      "OOD returned 404. The Slurm job has almost certainly ended, or the " +
      "node/port in your URL is stale — resubmit serve_stack.sbatch and paste " +
      "the new URL and token."
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

  /* A 200 carrying HTML means SSO gave us a login page, not the backend.
     This is the most confusing failure mode there is, so name it exactly. */
  if (contentType.includes("text/html")) {
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

async function signIn() {
  const cfg = await getConfig();
  if (!cfg.accessToken) {
    return { ok: false, error: "No access token saved. Get it with: cat ~/.rc_copilot/token" };
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
        error: "The backend rejected that access token. It changes every time the job restarts — re-read ~/.rc_copilot/token."
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

async function draft(ticketText, ticketNumber, extraInstructions) {
  if (!ticketText || !ticketText.trim()) {
    return { ok: false, error: "No ticket text to send." };
  }

  const payload = { ticket_text: ticketText };
  // Optional: the backend files captures under this, and falls back to a regex
  // over the text if the page didn't expose a number field.
  if (ticketNumber) payload.ticket_number = ticketNumber;
  // Free-text guidance the RC member typed before drafting.
  if (extraInstructions) payload.extra_instructions = extraInstructions;

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
    DRAFT: () => draft(message.ticketText, message.ticketNumber, message.extraInstructions),
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
