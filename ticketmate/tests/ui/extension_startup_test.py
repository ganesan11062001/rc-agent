"""The extension panel's startup progress, in Chromium, on a mock ticket page.

  PW_PYTHON tests/ui/extension_startup_test.py <timeline.json> [shots_dir]

<timeline.json> is /api/health as recorded by tests/startup/run_slow_start.sh
(RC_SLOW_START_TIMELINE=...). Each snapshot is passed through the real ping()
from extension/background.js, so the panel sees exactly what the service
worker would hand it; content.js and content.css are the shipped files.
"""
import json
import os
import re
import sys

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
EXT = os.path.join(HERE, "..", "..", "extension")
timeline = json.load(open(sys.argv[1]))
shots = sys.argv[2] if len(sys.argv) > 2 else "/tmp"
bg = open(os.path.join(EXT, "background.js")).read()
ping_src = re.search(r"async function ping\(\) \{.*?\n\}\n", bg, re.S).group(0)
request_src = re.search(r"async function request\(path, options\) \{.*?\n\}\n", bg, re.S).group(0)

def health(snap):
    return {"status": snap["status"], "configured_model": "stub-model", "detail": None, "startup": snap["startup"]}

# Synthetic states the recorded run did not pass through.
last = timeline[-1]["startup"]
kg_loading = json.loads(json.dumps(last))
kg_loading.update(ready=False)
kg_loading["knowledge_graph"] = next(s["startup"]["knowledge_graph"] for s in timeline
                                     if s["startup"]["knowledge_graph"]["stage"] == "warmup")
oom = json.loads(json.dumps(timeline[len(timeline) // 3]["startup"]))
oom["vllm"].update(stage="failed", label="Failed", failed=True,
                   detail="torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB")

PAGE = """<!doctype html><html><body style="font-family:sans-serif;margin:0">
<div style="padding:20px;max-width:700px"><h2>INC0012345</h2>
<label>Number <input name="number" value="INC0012345"></label><br>
<label>Short description <input name="short_description" value="Jupyter on OOD won't start"></label><br>
<label>Description <textarea name="description">My Jupyter session on Open OnDemand stays queued.</textarea></label>
</div></body></html>"""

INIT = """
window.__health = null;
// {__raw: ...} stands for what request() itself returns, e.g. OOD's error page.
const request = async () => (window.__health && window.__health.__raw) || { ok: true, data: window.__health };
%s
window.chrome = { runtime: {
  sendMessage: async (m) => {
    if (m.type === "PING") return ping();
    if (m.type === "SIGN_IN") return { ok: true, expiresAt: Date.now() / 1000 + 3 * 3600 };
    return { ok: false, error: "not mocked" };
  },
  getURL: (p) => p, onMessage: { addListener() {} } },
  storage: { local: { get: async () => ({}), set: async () => {} }, onChanged: { addListener() {} } } };
""" % ping_src

fails = []
def check(c, what):
    print(("  ok    " if c else "  FAIL  ") + what)
    if not c: fails.append(what)

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1100, "height": 760})
    pg.add_init_script(INIT)
    # A real origin (about:blank has no sessionStorage, which holds the panel's open state).
    pg.route("https://servicenow.test/**", lambda r: r.fulfill(status=200, content_type="text/html", body=PAGE))
    pg.goto("https://servicenow.test/incident.do")
    open_key = re.search(r'OPEN_KEY\s*=\s*"([^"]+)"', open(os.path.join(EXT, "content.js")).read()).group(1)
    pg.evaluate("k => sessionStorage.setItem(k, '1')", open_key)
    pg.evaluate("h => { window.__health = h; }", health(timeline[0]))
    pg.add_style_tag(path=os.path.join(EXT, "content.css"))
    pg.add_script_tag(path=os.path.join(EXT, "content.js"))
    panel = "aside#rc-copilot-panel"
    pg.wait_for_selector(panel + " .rc-boot:not([hidden])", timeout=10000)

    def show(h, wait_for=None):
        pg.evaluate("h => { window.__health = h; }", h)
        pg.click(panel + " .rc-status")     # same as the 3 s poll, without waiting for it
        pg.wait_for_timeout(400)

    picks = {"early": timeline[1], "weights": next(s for s in timeline if s["startup"]["vllm"]["stage"] == "weights"),
             "graphs": next(s for s in timeline if s["startup"]["vllm"]["stage"] == "graphs")}
    for name, snap in picks.items():
        show(health(snap))
        st = pg.get_attribute(panel + " .rc-status", "data-state")
        txt = pg.inner_text(panel + " .rc-boot")
        check(st == "starting", "%s: the strip says starting (%s)" % (name, st))
        check("Model" in txt and snap["startup"]["vllm"]["label"] in txt, "%s: model stage shown" % name)
        if snap["startup"]["knowledge_graph"]["stage"] != "ready":
            check(pg.locator(panel + " .rc-step").count() == 8, "%s: the graph checklist has 8 steps" % name)
        pg.screenshot(path=os.path.join(shots, "ext-startup-%s.png" % name))

    show({"status": "ok", "configured_model": "stub-model", "startup": kg_loading})
    txt = pg.inner_text(panel + " .rc-boot")
    check(pg.get_attribute(panel + " .rc-status", "data-state") == "ok", "model ready, graph loading: connected")
    check("won't cite RC pages" in txt and pg.is_visible(panel + " .rc-boot"), "model ready, graph loading: note shown")
    pg.screenshot(path=os.path.join(shots, "ext-startup-kg-loading.png"))

    show({"status": "ok", "configured_model": "stub-model", "startup": last})
    check(pg.is_hidden(panel + " .rc-boot"), "all ready: the progress block goes away")

    # The real request() on what OOD sends back: its "Failed to connect" page
    # while nothing listens yet is not the SSO login page.
    def classify(status, html):
        return pg.evaluate("""async ([status, html]) => {
            const TIMEOUT_MS = 5000;
            const getConfig = async () => ({ baseUrl: "https://ood.test/rnode/d4054/24980" });
            const fetch = async () => new Response(html, { status, headers: { "content-type": "text/html" } });
            %s
            return request("/api/health");
        }""" % request_src, [status, html])
    r = classify(404, "<html><body><h1>Failed to connect to d4054:24980</h1></body></html>")
    check(r.get("backendDown") and "d4054:24980" in r.get("error", ""), "OOD 'Failed to connect' page: backend not up, address named")
    check(not r.get("needsOodLogin"), "OOD 'Failed to connect' page: not called a login page")
    r = classify(502, "<html><body>Bad Gateway</body></html>")
    check(r.get("backendDown"), "HTML 5xx: backend not up")
    r = classify(200, "<html><body><form action='/idp/profile/SAML2'>Sign in</form></body></html>")
    check(r.get("needsOodLogin") and not r.get("backendDown"), "SSO login page: still asks to sign in")

    # The panel keeps waiting while nothing answers, and gives up after 5 min.
    down = {"__raw": {"ok": False, "backendDown": True,
                      "error": "Nothing is answering at d4054:24980 yet."}}
    show(down)
    check(pg.get_attribute(panel + " .rc-status", "data-state") == "starting", "backend not up: the strip says starting")
    check("Waiting for the session" in pg.inner_text(panel + " .rc-status"), "backend not up: waiting, not 'sign in'")
    pg.evaluate("() => { const real = Date.now; Date.now = () => real() + 6 * 60 * 1000; }")
    show(down)
    check(pg.get_attribute(panel + " .rc-status", "data-state") == "bad", "backend still not up after 5 min: not connected")
    check("Nothing is answering" in pg.inner_text(panel + " .rc-status"), "backend still not up after 5 min: says why")
    pg.screenshot(path=os.path.join(shots, "ext-backend-down.png"))

    show({"status": "unreachable", "configured_model": "stub-model", "detail": "connection refused", "startup": oom})
    check(pg.get_attribute(panel + " .rc-status", "data-state") == "bad", "OOM: the strip says not connected")
    check("out of memory" in pg.inner_text(panel + " .rc-status").lower(), "OOM: the error is shown")
    check(pg.locator(panel + " .rc-boot-row[data-status=failed]").count() == 1, "OOM: the model row is marked failed")
    check("Startup failed after" in pg.inner_text(panel + " .rc-boot"), "OOM: the header says it failed")
    pg.screenshot(path=os.path.join(shots, "ext-startup-failed.png"))
    b.close()

print("\n%s: %d failure(s)" % ("PASS" if not fails else "FAIL", len(fails)))
sys.exit(1 if fails else 0)
