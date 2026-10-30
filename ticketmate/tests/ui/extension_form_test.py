"""The extension reads a catalog item's request form (multi-GPU, long partition,
access, classroom requests), in Chromium, on mock ticket pages.

    PW_PYTHON tests/ui/extension_form_test.py      exit 0 = pass

Catalog tasks carry their substance in the request form's answers, which are
read-only on a task. Four mock pages: a Classic-style variable editor (read-only
inputs, a drop-down, a checkbox, a reference field with its hidden sys_id), a
Service Portal-style form, a read-only "Variables" section rendered as text,
and an incident with no form. The service worker is mocked; the text the panel
sends is captured and checked.
"""
import json
import os
import re
import sys

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
EXT = os.path.join(HERE, "..", "..", "extension")

HEAD = """<!doctype html><html><body style="font-family:sans-serif"><div style="max-width:700px;padding:20px">
<label>Number <input name="number" value="%(num)s"></label>
<label>Short description <input name="short_description" value="%(short)s"></label>
<label>Work notes <textarea name="work_notes"></textarea></label>
<label>Additional comments <textarea name="comments"></textarea></label>"""
PAGES = {
    "classic": HEAD % {"num": "TASK0100001", "short": "Multi-GPU Partition Access"} + """
<div class="sc_variable_editor">
  <div class="question_spacer"><label for="v1">Requested partition</label>
    <select id="v1" disabled><option value="">-- None --</option><option value="mg" selected>multigpu</option></select></div>
  <div class="question_spacer"><label for="v2">Describe your benchmarking calculation in detail</label>
    <textarea id="v2" readonly>I train with PyTorch DDP and want to scale from 1 to 4 GPUs.
The model fits on one GPU.</textarea></div>
  <div class="question_spacer"><label for="sys_display.v3">PI / Sponsor</label>
    <input id="sys_display.v3" readonly value="Jordan Lee"><input type="hidden" id="v3" value="6816f79cc0a8016401c5a33be04be441"></div>
  <div class="question_spacer"><label for="v4">I have read the multi-GPU policy</label>
    <input id="v4" type="checkbox" checked disabled></div>
  <div class="question_spacer"><label for="v5">Username</label><input id="v5" readonly value="doe.l"></div>
</div></div></body></html>""",
    "portal": HEAD % {"num": "RITM0200002", "short": "Long partition access"} + """
<sp-variable-editor><form>
  <div class="form-group"><label class="control-label">Expected run time *</label>
    <input class="form-control" value="5 days" readonly></div>
  <div class="form-group"><span class="control-label" id="q2">Can your job be checkpointed?</span>
    <input class="form-control" aria-labelledby="q2" value="No - proprietary solver" readonly></div>
  <fieldset><legend>Account type</legend>
    <label><input type="radio" name="acct" value="s"> Student</label>
    <label><input type="radio" name="acct" value="f" checked> Faculty</label></fieldset>
</form></sp-variable-editor></div></body></html>""",
    "textonly": HEAD % {"num": "TASK0300003", "short": "RC HPC Cluster Classroom Use for: Doe, Jane"} + """
<section><h3>Variables</h3><div>Course: CHEM0000.12345.202710<br>Number of students: 40<br>
Software needed: Gaussian</div></section></div></body></html>""",
    "incident": HEAD % {"num": "INC0400004", "short": "Job killed"} + """
<label>Description <textarea name="description">My job 4812345 was killed, OUT_OF_MEMORY.</textarea></label>
</div></body></html>""",
}

INIT = """
window.__sent = null;
window.chrome = { runtime: {
  sendMessage: async (m) => {
    if (m.type === "PING") return { ok: true, data: { status: "ok", configured_model: "stub" }, startup: null };
    if (m.type === "SIGN_IN") return { ok: true, expiresAt: Date.now() / 1000 + 3 * 3600 };
    if (m.type === "GET_CONFIG") return { ok: true, data: { targetField: "work_notes" } };
    if (m.type === "DRAFT") { window.__sent = m; return { ok: false, error: "captured" }; }
    return { ok: false, error: "not mocked" };
  },
  getURL: (p) => p, onMessage: { addListener() {} } },
  storage: { local: { get: async () => ({}), set: async () => {} }, onChanged: { addListener() {} } } };
"""

fails = []


def check(c, what):
    """Print ok/FAIL for one condition and remember failures."""
    print(("  ok    " if c else "  FAIL  ") + what)
    if not c:
        fails.append(what)


def sent_for(pg, name):
    """Load a mock page, open the panel, click Draft reply; (text sent, panel's read-from-page list)."""
    pg.route("https://servicenow.test/**", lambda r: r.fulfill(status=200, content_type="text/html", body=PAGES[name]))
    pg.goto("https://servicenow.test/%s.do" % name)
    key = re.search(r'OPEN_KEY\s*=\s*"([^"]+)"', open(os.path.join(EXT, "content.js")).read()).group(1)
    pg.evaluate("k => sessionStorage.setItem(k, '1')", key)
    pg.add_style_tag(path=os.path.join(EXT, "content.css"))
    pg.add_script_tag(path=os.path.join(EXT, "content.js"))
    pg.wait_for_selector("aside#rc-copilot-panel .rc-go", timeout=10000)
    pg.wait_for_timeout(500)
    pg.click("aside#rc-copilot-panel .rc-go")
    pg.wait_for_function("() => window.__sent !== null", timeout=10000)
    return pg.evaluate("() => window.__sent.ticketText"), pg.inner_text("aside#rc-copilot-panel .rc-checks")


with sync_playwright() as p:
    b = p.chromium.launch()
    errors = []
    for name in PAGES:
        pg = b.new_page()
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.add_init_script(INIT)
        text, checks = sent_for(pg, name)
        print(name)
        if name == "classic":
            check("Request form:" in text and text.index("Request form:") < len(text), "the form is sent under 'Request form:'")
            check("- Requested partition: multigpu" in text, "a drop-down: its selected text")
            check("Describe your benchmarking calculation in detail: I train with PyTorch DDP" in text
                  and "The model fits on one GPU." in text, "a read-only textarea, multi-line answer kept")
            check("- PI / Sponsor: Jordan Lee" in text and "6816f79c" not in text,
                  "a reference field: its display value, not the hidden sys_id")
            check("- I have read the multi-GPU policy: yes" in text, "a checkbox: yes / no")
            check("- Username: doe.l" in text, "a read-only input")
            check("Work notes" not in text.split("Request form:")[1].split("\n\n")[0],
                  "the ticket's own fields are not repeated as form answers")
            check("request form (5 answers)" in checks, "the panel says what it read: %r" % checks.replace("\n", " | "))
        elif name == "portal":
            check("- Expected run time: 5 days" in text, "Service Portal: label in the form group, '*' dropped")
            check("- Can your job be checkpointed?: No - proprietary solver" in text, "a label by aria-labelledby")
            check("- Account type: Faculty" in text and "Student" not in text, "a radio group: the chosen answer")
        elif name == "textonly":
            check("Request form:" in text and "Course: CHEM0000.12345.202710" in text and "Gaussian" in text,
                  "a read-only 'Variables' section rendered as text is kept as text")
        elif name == "incident":
            check("Request form:" not in text and "OUT_OF_MEMORY" in text, "an incident with no form: unchanged")
            check("request form" not in checks, "and the panel does not mention a form")
        pg.close()
    check(not errors, "no page errors: %s" % errors[:2])
    b.close()

print("\n%s: %d failure(s)" % ("PASS" if not fails else "FAIL", len(fails)))
sys.exit(1 if fails else 0)
