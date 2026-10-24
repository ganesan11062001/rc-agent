/* Runs on Open OnDemand pages. Its whole job is to remove the copy-paste step.
 *
 * The RC Copilot interactive app renders a hidden element carrying this
 * session's proxy path and access token (see ood-app/view.html.erb). OOD shows
 * that card only to the user who owns the job, so reading it here is the same
 * trust boundary OOD already uses for Jupyter and RStudio passwords.
 *
 * Because OOD renders each app's view inline on "My Interactive Sessions",
 * simply visiting that page configures the extension. Nothing to type.
 */

(function () {
  "use strict";

  const MARKER_ID = "rc-copilot-connection";
  const BANNER_ID = "rc-copilot-autoconfig-banner";

  function banner(text, ok) {
    let box = document.getElementById(BANNER_ID);
    if (!box) {
      box = document.createElement("div");
      box.id = BANNER_ID;
      document.body.appendChild(box);
    }
    box.textContent = text;
    box.style.cssText = [
      "position:fixed", "right:16px", "bottom:16px", "z-index:2147483647",
      "max-width:360px", "padding:11px 14px", "border-radius:8px",
      "font:400 13px/1.45 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif",
      "box-shadow:0 2px 12px rgba(0,0,0,.18)",
      ok ? "background:#e3f5e9;color:#1c6b39;border:1px solid #2a9d5c"
         : "background:#fce8e8;color:#9b1c1c;border:1px solid #d64545"
    ].join(";");
    setTimeout(function () { box.remove(); }, 7000);
  }

  let lastSaved = "";

  async function harvest() {
    const marker = document.getElementById(MARKER_ID);
    if (!marker) return;

    const path = marker.getAttribute("data-rc-path");
    const token = marker.getAttribute("data-rc-token");
    const node = marker.getAttribute("data-rc-node") || "?";
    // Two apps (the model one and the capture test) share this marker.
    // Name whichever was picked up, so it is never ambiguous.
    const appName = marker.getAttribute("data-rc-app") || "RC Copilot";
    if (!path || !token) return;

    // Relative path in the markup, resolved here: nothing hard-codes a host.
    const baseUrl = window.location.origin + path.replace(/\/+$/, "");

    // Don't re-save (and so don't re-clear the session) on every DOM mutation.
    const fingerprint = baseUrl + "|" + token + "|" + appName;
    if (fingerprint === lastSaved) return;
    lastSaved = fingerprint;

    try {
      const reply = await chrome.runtime.sendMessage({
        type: "SAVE_CONFIG",
        baseUrl: baseUrl,
        accessToken: token
      });
      if (reply && reply.ok) {
        banner(appName + " configured automatically — session on " + node + ".", true);
      } else {
        banner("RC Copilot: couldn't save config. " + ((reply && reply.error) || ""), false);
      }
    } catch (err) {
      // Extension reloaded while the page stayed open.
      lastSaved = "";
    }
  }

  harvest();

  /* The sessions list renders cards asynchronously and polls for status. */
  let pending = null;
  const observer = new MutationObserver(function () {
    clearTimeout(pending);
    pending = setTimeout(harvest, 400);
  });
  observer.observe(document.documentElement, { childList: true, subtree: true });
})();
