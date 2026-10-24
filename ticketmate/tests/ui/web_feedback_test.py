"""Browser test of the backend's web UI: sign in, draft, read the automatic
checks, give feedback, and confirm what the backend stored. Run through
tests/ui/run.sh, which starts rc_kg, a stub model and the backend."""
import glob, json, sys, time
from playwright.sync_api import sync_playwright
APP, RUNS, SHOTS = sys.argv[1], sys.argv[2], sys.argv[3]
fails = []
def check(c, what):
    print(("  ok    " if c else "  FAIL  ") + what)
    if not c: fails.append(what)
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1100, "height": 1500})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto("http://127.0.0.1:%s/" % APP)
    pg.fill("#access-token", "uitest"); pg.click("#login-btn")
    pg.wait_for_selector("#app-main:not([hidden])", timeout=15000)
    pg.fill("#ticket-text", "Hi, my job 4123456 was killed, sacct says OUT_OF_MEMORY. I asked for --mem=4G. What should I do?")
    pg.click("#draft-btn")
    pg.wait_for_selector("#results:not([hidden])", timeout=60000)
    ev = pg.inner_text("#eval-summary")
    print("  eval summary:", ev)
    check("Automatic checks:" in ev and "passed" in ev, "automatic checks shown")
    check("no URLs outside the sources" in ev, "the failed assertion is named")
    check("has-fail" in pg.get_attribute("#eval-summary", "class"), "summary styled as a warning when a check fails")
    rid = pg.inner_text("#fb-id").replace("draft ", "").strip()
    check(len(rid) == 8, "request id shown: %s" % rid)
    check(pg.is_disabled("#fb-submit"), "Send disabled before any choice")
    pg.click('#feedback-card .fb-row[data-key="thumbs"] .fb-chip[data-score="1"]')
    pg.click('#feedback-card .fb-row[data-key="correctness"] .fb-chip[data-score="0.5"]')
    pg.click('#feedback-card .fb-row[data-key="usage"] .fb-chip[data-value="edited"]')
    pg.click('#feedback-card .fb-row[data-key="rating"] .fb-chip[data-value="5"]')
    pg.click('#feedback-card .fb-row[data-key="rating"] .fb-chip[data-value="4"]')    # change of mind
    check(pg.locator('#feedback-card .fb-row[data-key="rating"] .fb-chip.on').count() == 1, "one choice per row")
    pg.fill("#fb-comment", "Right fix; drop the second paragraph.")
    pg.click(".fb-more summary")
    pg.fill("#fb-correction", pg.input_value("#draft-response").replace("Hi,", "Hello Priya,"))
    pg.screenshot(path=SHOTS + "/feedback_before_send.png", full_page=True)
    check(not pg.is_disabled("#fb-submit"), "Send enabled after choosing")
    pg.click("#fb-submit")
    pg.wait_for_function("document.getElementById('fb-status').textContent.indexOf('Thanks') === 0", timeout=15000)
    print("  status:", pg.inner_text("#fb-status"))
    check(pg.inner_text("#fb-submit") == "Sent ✓", "button shows Sent ✓")
    pg.screenshot(path=SHOTS + "/feedback_after_send.png", full_page=True)
    check(not errors, "no JavaScript errors (%s)" % errors[:2])
    b.close()
time.sleep(1)
fb = glob.glob("%s/*/*-%s.feedback.jsonl" % (RUNS, rid))
recs = [json.loads(l) for l in open(fb[0]).read().splitlines()] if fb else []
by = {r["key"]: r for r in recs if r.get("source") == "user"}
print("  stored user feedback:", {k: (v.get("score"), v.get("value")) for k, v in by.items()})
check(by.get("thumbs", {}).get("score") == 1 and by.get("correctness", {}).get("score") == 0.5, "thumbs and correctness stored")
check(by.get("usage", {}).get("value") == "edited" and by.get("rating", {}).get("value") == "4", "usage value and the final rating stored")
check(by.get("comment", {}).get("comment", "").startswith("Right fix") and by.get("comment", {}).get("correction"), "comment and sent reply stored")
check(any(r["key"] == "edit_similarity" for r in recs), "edit_similarity derived from the sent reply")
print("\n%s: %d failure(s)" % ("PASS" if not fails else "FAIL", len(fails)))
sys.exit(1 if fails else 0)
