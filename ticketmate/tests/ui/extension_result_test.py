"""The extension panel's result layout, in Chromium, on a mock ticket page.

    PW_PYTHON tests/ui/extension_result_test.py [shots_dir]      exit 0 = pass

The service worker is mocked (connected, signed in, one canned draft); the
panel is the shipped content.js and content.css. Checks the layout: half the
page wide; what to act on (where it went, confidence, what to check, the
draft) shown at once; everything else in collapsible sections whose titles
say what is inside, remembered once opened; cited RC pages as links.
"""
import json
import os
import re
import sys

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
EXT = os.path.join(HERE, "..", "..", "extension")
shots = sys.argv[1] if len(sys.argv) > 1 else "/tmp"

DRAFT = {
    "problem_summary": "srun hangs after allocation; the job then hit its 1-hour limit.",
    "suggested_steps": ["Check the job with sacct -j 10945531", "Suggest --time=02:00:00 and srun --pty bash",
                        "Point to the interactive jobs page [1]"],
    "confidence": 0.8, "confidence_label": "high",
    "caveats": ["Confirm the partition the researcher has access to."],
    "draft_response": "Hi Ada,\n\nYour job 10945531 reached its 1-hour time limit, which is why it stopped.\n\n"
                      "Best regards,\nResearch Computing",
    "model": "qwen2.5-7b-instruct-awq", "latency_ms": 2400, "request_id": "abcd1234",
    "usage": {"prompt_tokens": 5100, "completion_tokens": 300, "total_tokens": 5400},
    "kg_status": "ok",
    "references": [{"n": 1, "title": "Interactive Jobs: srun Command",
                    "url": "https://rc.northeastern.edu/example/#interactive"}],
    "lookups": [{"kind": "job", "target": "10945531", "command": "sacct -j 10945531", "ok": True,
                 "summary": "TIMEOUT, exit 0:0, ran 01:00:06 of 01:00:00", "output": "", "ms": 140}],
    "context": {"raw_chars": 3690, "chars": 1666, "notifications_removed": 4, "addresses_removed": 44},
    "evaluations": {"assertions": {"assert_signoff": True, "assert_has_steps": True,
                                   "assert_cites_when_grounded": False},
                    "metrics": {"reply_words": 23, "conciseness": 1.0, "answer_relevance_embedding": 0.85}},
    "judge": {"mode": "gate", "verdict": "ok", "rewritten": True,
              "first": {"verdict": "low", "reasons": ["may not resolve the request: no time limit given"]},
              "scores": {"hallucination": {"score": 0.0, "comment": ""},
                         "correctness": {"score": 0.9, "comment": "raises the limit as the page says"},
                         "conciseness": {"score": 1.0, "comment": ""},
                         "answer_relevance": {"score": 1.0, "comment": ""}},
              "rubric": [{"item": "It says access was granted only if an RC staff entry shows it.", "met": True,
                          "reason": "no grant claimed"},
                         {"item": "A stage the ticket is not at.", "met": None, "reason": ""}]},
}

PAGE = """<!doctype html><html><body style="font-family:sans-serif;margin:0">
<div style="padding:20px;max-width:700px"><h2>INC0012345</h2>
<label>Number <input name="number" value="INC0012345"></label><br>
<label>Short description <input name="short_description" value="srun hangs"></label><br>
<label>Description <textarea name="description">srun hangs after allocation.</textarea></label><br>
<label>Work notes <textarea name="work_notes"></textarea></label><br>
<label>Additional comments <textarea name="comments"></textarea></label>
</div></body></html>"""

INIT = """
window.chrome = { runtime: {
  sendMessage: async (m) => {
    if (m.type === "PING") return { ok: true, data: { status: "ok", configured_model: "stub" }, startup: null };
    if (m.type === "SIGN_IN") return { ok: true, expiresAt: Date.now() / 1000 + 3 * 3600 };
    if (m.type === "GET_CONFIG") {
      const all = JSON.parse(sessionStorage.getItem("__store") || "{}");
      return { ok: true, data: { targetField: (all.oodConfig || {}).targetField || "work_notes" } };
    }
    if (m.type === "DRAFT") { window.__draftMsg = m; return { ok: true, data: %s }; }
    return { ok: false, error: "not mocked" };
  },
  getURL: (p) => p, onMessage: { addListener() {} } },
  // chrome.storage.local, kept in the page's sessionStorage so it survives a reload.
  storage: { local: {
    get: async (k) => { const all = JSON.parse(sessionStorage.getItem("__store") || "{}");
                        return typeof k === "string" ? (k in all ? { [k]: all[k] } : {}) : all; },
    set: async (o) => { const all = JSON.parse(sessionStorage.getItem("__store") || "{}");
                        Object.assign(all, o); sessionStorage.setItem("__store", JSON.stringify(all)); } },
    onChanged: { addListener() {} } } };
""" % json.dumps(DRAFT)

fails = []


def check(c, what):
    """Print ok/FAIL for one condition and remember failures."""
    print(("  ok    " if c else "  FAIL  ") + what)
    if not c:
        fails.append(what)


with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1400, "height": 900})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.add_init_script(INIT)
    pg.route("https://servicenow.test/**", lambda r: r.fulfill(status=200, content_type="text/html", body=PAGE))
    pg.goto("https://servicenow.test/incident.do")
    open_key = re.search(r'OPEN_KEY\s*=\s*"([^"]+)"', open(os.path.join(EXT, "content.js")).read()).group(1)
    pg.evaluate("k => sessionStorage.setItem(k, '1')", open_key)
    panel = "aside#rc-copilot-panel"

    def load_panel():
        """Inject the extension's content script and CSS and wait for the panel."""
        pg.add_style_tag(path=os.path.join(EXT, "content.css"))
        pg.add_script_tag(path=os.path.join(EXT, "content.js"))
        pg.wait_for_selector(panel + " .rc-go", timeout=10000)
        pg.wait_for_timeout(700)

    def width():
        """The panel's rendered width in pixels."""
        return pg.evaluate("s => document.querySelector(s).getBoundingClientRect().width", panel)

    load_panel()
    check(abs(width() - 700) <= 2, "panel starts at half the page (%dpx of 1400)" % width())
    pg.screenshot(path=os.path.join(shots, "ext-compose.png"))

    print("width")
    pg.click(panel + ' .rc-size[data-size="third"]'); pg.wait_for_timeout(200)
    check(abs(width() - 1400 / 3) <= 2, "⅓ preset (%dpx)" % width())
    pg.click(panel + ' .rc-size[data-size="wide"]'); pg.wait_for_timeout(200)
    check(abs(width() - 1400 * 2 / 3) <= 2, "⅔ preset (%dpx)" % width())
    box = pg.eval_on_selector(panel + " .rc-resize", "e => { const r = e.getBoundingClientRect(); return [r.x + r.width / 2, r.y + 300]; }")
    pg.mouse.move(*box); pg.mouse.down(); pg.mouse.move(1400 - 820, 300, steps=8); pg.mouse.up(); pg.wait_for_timeout(200)
    check(abs(width() - 820) <= 4, "dragging the left edge resizes it (%dpx)" % width())
    check(pg.eval_on_selector_all(panel + " .rc-size.rc-size-on", "els => els.length") == 0,
          "no preset is marked after a custom width")
    pg.reload(); pg.evaluate("k => sessionStorage.setItem(k, '1')", open_key); load_panel()
    check(abs(width() - 820) <= 4, "the width is remembered after a reload (%dpx)" % width())
    pg.dblclick(panel + " .rc-resize"); pg.wait_for_timeout(200)
    check(abs(width() - 700) <= 2, "double-click on the edge: back to half (%dpx)" % width())

    print("compose")
    pg.click(panel + ' .rc-chip:has-text("Follow up / close")')
    check("we will go ahead and close the ticket" in pg.input_value(panel + " .rc-notes"),
          "a quick instruction chip fills the instructions box")
    pg.click(panel + ' .rc-chip:has-text("Follow up / close")')
    check(pg.input_value(panel + " .rc-notes").count("close the ticket") == 1, "clicking it twice does not repeat it")
    pg.select_option(panel + " .rc-target", "comments"); pg.wait_for_timeout(200)
    check("researcher sees" in pg.inner_text(panel + " .rc-target-note") and
          "rc-danger" in pg.get_attribute(panel + " .rc-target-note", "class"),
          "choosing comments warns that the researcher sees it")
    stored = pg.evaluate("() => JSON.parse(sessionStorage.getItem('__store') || '{}').oodConfig")
    check((stored or {}).get("targetField") == "comments", "the choice is saved (same setting as the popup)")
    pg.click(panel + " .rc-go"); pg.wait_for_selector(panel + " .rc-draft", timeout=10000); pg.wait_for_timeout(300)
    check("1-hour time limit" in pg.input_value("textarea[name=comments]") and not pg.input_value("textarea[name=work_notes]"),
          "the draft goes where you chose")
    pg.select_option(panel + " .rc-target", "work_notes"); pg.wait_for_timeout(200)
    pg.fill("textarea[name=comments]", "")

    pg.evaluate("""() => { const all = JSON.parse(sessionStorage.getItem("__store") || "{}");
                           all.rcSignoff = "Thanks,\\nSam Rivera\\nResearch Computing";
                           sessionStorage.setItem("__store", JSON.stringify(all)); }""")
    pg.click(panel + " .rc-go")
    pg.wait_for_selector(panel + " .rc-draft", timeout=10000)
    pg.wait_for_timeout(300)
    check(pg.evaluate("() => window.__draftMsg && window.__draftMsg.signoff") == "Thanks,\nSam Rivera\nResearch Computing",
          "the popup's sign-off is sent with the draft")

    check(pg.is_visible(panel + " .rc-caveat-box") and "Confirm the partition" in pg.inner_text(panel + " .rc-caveat-box"),
          "'Check before sending' is shown at once")
    check(pg.is_visible(panel + " .rc-draft") and "1-hour time limit" in pg.input_value(panel + " .rc-draft"),
          "the draft is shown at once")
    check("Written into" in pg.inner_text(panel + " .rc-msg"), "says where the draft was written")
    check("1-hour time limit" in pg.input_value("textarea[name=work_notes]"), "draft written into work notes")
    titles = pg.eval_on_selector_all(panel + " details.rc-more > summary", "els => els.map(e => e.textContent)")
    print("  sections:", titles)
    want = ["steps for you (3)", "Cluster lookups (1 ran)", "RC pages cited (1)", "Quality checks: 2 of 3 passed",
            "LLM judge: looks right (rewritten once)", "Ticket & model details", "Rate this draft"]
    check(all(any(w in t for t in titles) for w in want), "every section's title says what is inside")
    check(pg.eval_on_selector_all(panel + " details.rc-more[open]", "els => els.length") == 0,
          "sections start closed")
    check(pg.is_hidden(panel + " .rc-lookups"), "details stay out of the way until asked for")
    check("rc-more-warn" in pg.get_attribute(panel + ' details[data-sec="checks"]', "class"),
          "a failed check marks its section")

    pg.click(panel + ' details[data-sec="lookups"] > summary')
    pg.click(panel + ' details[data-sec="refs"] > summary')
    check(pg.is_visible(panel + " .rc-lookups") and "TIMEOUT" in pg.inner_text(panel + " .rc-lookups"),
          "lookups open on click")
    href = pg.get_attribute(panel + " .rc-refs a", "href")
    check(href == DRAFT["references"][0]["url"] and pg.get_attribute(panel + " .rc-refs a", "target") == "_blank",
          "cited pages are links that open in a new tab")
    pg.screenshot(path=os.path.join(shots, "ext-result.png"))

    pg.click(panel + " .rc-go")           # a second draft
    pg.wait_for_timeout(800)
    opened = pg.eval_on_selector_all(panel + " details.rc-more[open]", "els => els.map(e => e.dataset.sec)")
    check(sorted(opened) == ["lookups", "refs"], "sections you opened stay open on the next draft (%s)" % opened)
    pg.click(panel + ' details[data-sec="feedback"] > summary')
    check(pg.is_visible(panel + " .rc-fb-send"), "feedback form works inside its section")
    judge = pg.eval_on_selector(panel + ' details[data-sec="judge"]', "e => e.textContent")
    check("resolves the request 90%" in judge and "granted only if" in judge and "stage the ticket is not at" not in judge
          and "rewritten once" in judge, "judge section: scores, the rubric items that apply, the rewrite")
    check(not errors, "no JavaScript errors (%s)" % errors)
    b.close()

print("\n%s: %d failure(s)" % ("PASS" if not fails else "FAIL", len(fails)))
sys.exit(1 if fails else 0)
