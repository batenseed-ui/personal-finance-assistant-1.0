"""End-to-end browser test (Playwright + headless Chromium) against a real Flask server.

Run from the project folder:  python3 tests/browser_test.py
Checks the two main buttons, field-error display, input persistence across reloads,
and the failed-fetch path (server down, then recovery via the Retry button).
"""

import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Throw-away database and secret, set BEFORE app.py is imported: this test never touches your real data.
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production"
os.environ["WEALTH_DB"] = os.path.join(tempfile.mkdtemp(prefix="pfa-browser-test-"), "browser.db")

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

import app as wealth_app

# No AI keys, so the app uses its rule-based fallback instead of calling a real AI service.
for _name in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "AI_PROVIDER"):
    os.environ.pop(_name, None)
PORT = 5071
BASE = f"http://127.0.0.1:{PORT}"
TEST_USER = "browsertester"
TEST_PASSWORD = "test-password-123"

FIELDS = {
    "age": "34", "retirement_age": "62", "monthly_income": "5500", "monthly_expenses": "3200",
    "monthly_debt_payments": "600", "monthly_investment": "400", "liquid_savings": "9000",
    "investments": "42000", "total_debt": "38000",
}

results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition)))
    print(("PASS  " if condition else "FAIL  ") + name + (f"  [{detail}]" if detail and not condition else ""))


class Server:
    def start(self):
        self.srv = make_server("127.0.0.1", PORT, wealth_app.app, threaded=True)
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.t.join(timeout=5)


def fill(page):
    for name, value in FIELDS.items():
        page.fill(f"#profile-form [name={name}]", value)
    page.check("#profile-form [name=has_health_insurance]")


def main():
    server = Server()
    server.start()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        console_errors = []
        page.on("pageerror", lambda e: console_errors.append(str(e)))

        # --- opening the template straight from disk must explain itself, not show a dead form
        page.goto("file://" + os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "index.html"))
        page.wait_for_selector("text=Open this app through the Flask server")
        check("file:// shows 'use the Flask server' instructions", page.locator("#btn-analyze").count() == 0)

        # --- login: a logged-out visitor is sent to the login page; creating an account signs you in
        page.goto(BASE)
        page.wait_for_selector("#form-login")
        check("logged-out visitor is sent to the login page", page.url.endswith("/login"), page.url)
        page.click("#tab-register")
        page.fill("#register-username", TEST_USER)
        page.fill("#register-password", TEST_PASSWORD)
        page.fill("#register-confirm", TEST_PASSWORD)
        page.click("#form-register .submit")
        page.wait_for_selector("#btn-analyze")
        check("creating an account signs you in and opens the app", page.url.rstrip("/") == BASE, page.url)
        page.wait_for_selector("#wa-auth-bar")
        check("signed-in name is shown", TEST_USER in page.inner_text("#wa-auth-bar"), page.inner_text("#wa-auth-bar"))

        page.goto(BASE)
        check("served page does not show the file:// warning", page.locator("text=Open this app through the Flask server").count() == 0)
        page.wait_for_selector("#method-body ul")  # methodology loaded from /api/methodology
        check("methodology rendered from server", page.locator("#method-body li").count() == 6)

        # --- validation: empty form -> field errors, no crash
        page.click("#btn-analyze")
        page.wait_for_selector("[data-error-for=age]:not(:empty)")
        check("empty form shows field errors", "required" in page.inner_text("[data-error-for=monthly_income]"))
        check("score card still hidden after validation error", page.locator("#score-card").is_hidden())
        check("buttons re-enabled after error", page.locator("#btn-analyze").is_enabled())

        # --- Analyze My Finances
        fill(page)
        page.click("#btn-analyze")
        page.wait_for_selector("#score-card:not([hidden])")
        check("analyze shows score 60.9", page.inner_text("#score-num") == "60.9", page.inner_text("#score-num"))
        check("analyze shows grade Fair", page.inner_text("#score-grade") == "Fair")
        check("six breakdown rows", page.locator("#breakdown-body tr").count() == 6)
        check("field errors cleared on success", page.inner_text("[data-error-for=age]") == "")
        check("advice card not shown by Analyze", page.locator("#advice-card").is_hidden())

        # --- Get AI Wealth Advice (no API key on this server -> labelled fallback)
        page.click("#btn-advice")
        page.wait_for_selector("#advice-card:not([hidden])")
        check("advice card shows fallback badge", page.inner_text("#advice-badge") == "Rule-based fallback")
        check("fallback notice explains why", "ANTHROPIC_API_KEY" in page.inner_text("#advice-notice"))
        check("advice text present", len(page.inner_text("#advice-text")) > 50)

        # --- what-if simulator
        def slide(name, value):
            page.evaluate("""([sel, val]) => { const e = document.querySelector(sel); e.value = val;
                e.dispatchEvent(new Event('input', {bubbles: true})); }""", [f"#wi-{name}", str(value)])

        page.wait_for_selector("#whatif-card:not([hidden])")
        page.wait_for_function("document.querySelector('#wi-base').textContent === '60.9'")
        check("what-if card shown after analysis with base score 60.9", page.inner_text("#wi-base") == "60.9")
        check("expense slider sized to the user's expenses", page.get_attribute("#wi-expense_cut", "max") == "3200")
        check("no change before moving sliders", page.inner_text("#wi-delta") == "0")
        slide("expense_cut", 500)
        page.wait_for_function("document.querySelector('#wi-new').textContent !== '60.9'")
        new_score = float(page.inner_text("#wi-new"))
        check("cutting expenses 500 raises the score to about 63.5", 63.0 <= new_score <= 64.0, str(new_score))
        check("delta shown with a plus sign", page.inner_text("#wi-delta").startswith("+"), page.inner_text("#wi-delta"))
        check("what-if table has six rows", page.locator("#wi-body tr").count() == 6)
        check("slider label shows the amount", "500" in page.inner_text("#wi-out-expense_cut"))
        page.click("#wi-reset")
        page.wait_for_function("document.querySelector('#wi-new').textContent === '60.9'")
        check("reset returns to no change", page.inner_text("#wi-delta") == "0" and page.input_value("#wi-expense_cut") == "0")
        slide("extra_investment", 2000)
        page.wait_for_selector("#wi-warnings:not([hidden])")
        check("unaffordable investing shows a warning", "surplus" in page.inner_text("#wi-warnings"))
        page.click("#wi-reset")
        page.wait_for_selector("#wi-warnings", state="hidden")

        # --- currency selector: Chinese yuan (CN¥) is offered, used in the UI, and remembered
        check("currency list offers CN¥", page.locator("#currency option[value='CN¥']").count() == 1)
        page.select_option("#currency", "CN¥")
        slide("expense_cut", 500)
        check("what-if label uses CN¥", "CN¥500" in page.inner_text("#wi-out-expense_cut"), page.inner_text("#wi-out-expense_cut"))
        page.reload()
        page.wait_for_selector("#method-body ul")
        check("CN¥ is remembered after reload", page.input_value("#currency") == "CN¥")
        page.select_option("#currency", "$")

        # --- persistence across reload
        page.reload()
        page.wait_for_selector("#method-body ul")
        check("inputs restored after reload", page.input_value("#profile-form [name=monthly_income]") == "5500")
        check("checkbox restored after reload", page.is_checked("#profile-form [name=has_health_insurance]"))

        # --- failed fetch: stop the server, click each button
        server.stop()
        page.click("#btn-analyze")
        page.wait_for_selector("#banner:not([hidden])")
        check("analyze: banner explains the connection failure", "Could not reach the server" in page.inner_text("#banner-text"))
        check("analyze: retry button offered", page.locator("#banner-retry").is_visible())
        check("analyze: inputs preserved after failure", page.input_value("#profile-form [name=age]") == "34"
              and page.input_value("#profile-form [name=total_debt]") == "38000")
        check("analyze: button usable again", page.locator("#btn-analyze").is_enabled()
              and page.inner_text("#btn-analyze") == "Analyze My Finances")

        page.click("#banner-close")
        check("banner dismissable", page.locator("#banner").is_hidden())

        slide("expense_cut", 300)
        page.wait_for_selector("#banner:not([hidden])")
        check("what-if: banner on connection failure", "Could not reach the server" in page.inner_text("#banner-text"))
        check("what-if: slider keeps its position after failure", page.input_value("#wi-expense_cut") == "300")
        page.click("#banner-close")

        page.click("#btn-advice")
        page.wait_for_selector("#banner:not([hidden])")
        check("advice: banner on connection failure", "Could not reach the server" in page.inner_text("#banner-text"))
        check("advice: button label restored", page.inner_text("#btn-advice") == "Get AI Wealth Advice")

        # --- recovery: restart server, Retry re-runs the advice request
        server.start()
        page.click("#banner-retry")
        page.wait_for_function("document.querySelector('#banner').hidden === true")
        page.wait_for_function("document.querySelector('#advice-text').textContent.length > 50")
        check("retry succeeds once server is back", page.inner_text("#score-num") == "60.9")

        # --- calculators
        page.fill("form[data-calc=loan] [name=principal]", "100000")
        page.fill("form[data-calc=loan] [name=rate]", "6")
        page.fill("form[data-calc=loan] [name=years]", "30")
        page.click("form[data-calc=loan] button[type=submit]")
        page.wait_for_selector("form[data-calc=loan] .calc-out dl")
        check("loan calculator shows 599.55", "599.55" in page.inner_text("form[data-calc=loan] .calc-out"))

        page.fill("form[data-calc=compound] [name=initial]", "1000")
        page.fill("form[data-calc=compound] [name=monthly]", "0")
        page.fill("form[data-calc=compound] [name=rate]", "12")
        page.fill("form[data-calc=compound] [name=years]", "1")
        page.click("form[data-calc=compound] button[type=submit]")
        page.wait_for_selector("form[data-calc=compound] .calc-out dl")
        # The page displays whole currency units here; the exact 1126.83 is covered by the API tests.
        check("compound calculator shows $1,127 final balance", "$1,127" in page.inner_text("form[data-calc=compound] .calc-out"),
              page.inner_text("form[data-calc=compound] .calc-out"))

        page.fill("form[data-calc=goal] [name=target]", "1200")
        page.fill("form[data-calc=goal] [name=current]", "0")
        page.fill("form[data-calc=goal] [name=monthly]", "100")
        page.fill("form[data-calc=goal] [name=rate]", "0")
        page.click("form[data-calc=goal] button[type=submit]")
        page.wait_for_selector("form[data-calc=goal] .calc-out dl")
        check("goal calculator shows 12 months", "12 months" in page.inner_text("form[data-calc=goal] .calc-out"))

        # calculator validation error
        page.fill("form[data-calc=loan] [name=years]", "0")
        page.click("form[data-calc=loan] button[type=submit]")
        page.wait_for_selector("form[data-calc=loan] [data-error-for=years]:not(:empty)")
        check("calculator shows its own field error",
              "between" in page.inner_text("form[data-calc=loan] [data-error-for=years]"),
              page.inner_text("form[data-calc=loan] [data-error-for=years]"))

        page.reload()
        check("calculator inputs persisted", page.input_value("form[data-calc=loan] [name=principal]") == "100000")

        # --- the session ends while the page is open: the user is told, keeps their inputs, and Retry works
        #     once they have logged in again (the login-expired banner links to a login page in a new tab)
        page.context.clear_cookies()
        page.click("#btn-analyze")
        page.wait_for_selector("#banner:not([hidden])")
        check("expired session: the app asks the user to log in", "log in" in page.inner_text("#banner-text").lower(),
              page.inner_text("#banner-text"))
        page.wait_for_selector("#banner .wa-login-link")
        check("expired session: 'Log in again' link (new tab) sits in the app's own banner",
              page.get_attribute("#banner .wa-login-link", "target") == "_blank"
              and page.get_attribute("#banner .wa-login-link", "href") == "/login?popup=1")
        page.wait_for_timeout(700)   # the widget only adds its own banner if the app's banner is NOT showing
        check("expired session: only one banner is showing", page.locator("#wa-auth-banner").count() == 0)
        check("expired session: typed inputs preserved", page.input_value("#profile-form [name=age]") == "34"
              and page.input_value("#profile-form [name=total_debt]") == "38000")
        relogin = page.request.post(f"{BASE}/api/login", data={"username": TEST_USER, "password": TEST_PASSWORD})
        check("logging in again (as the new tab would) works", relogin.ok)
        page.click("#banner-retry")
        page.wait_for_selector("#banner .wa-login-link", state="detached")   # only goes away after a successful request
        check("expired session: Retry succeeds after logging in again", page.locator("#banner").is_hidden())

        # --- log out: back to the login page, saved inputs removed, the app is protected again
        check("inputs are saved in this browser before logging out", page.evaluate("localStorage.getItem('wa:profile')") is not None)
        page.click("#wa-auth-bar button")
        page.wait_for_selector("#form-login")
        check("Log out returns to the login page", page.url.endswith("/login"), page.url)
        check("Log out removes the saved inputs from this browser",
              page.evaluate("Object.keys(localStorage).filter(k => k.startsWith('wa:')).length") == 0)
        page.goto(BASE)
        page.wait_for_selector("#form-login")
        check("the app is protected again after logging out", page.url.endswith("/login"), page.url)

        check("no uncaught JS errors", not console_errors, "; ".join(console_errors))
        browser.close()
    server.stop()

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} browser checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
