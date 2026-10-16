"""Tests for the login system.  Run from the project folder with:

    python -m unittest discover -s tests -v

They use a throw-away database, and replace the scoring functions with simple stand-ins so the
tests check the login/authorization behaviour only (not your scoring rules).
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

# Settings must be in place before app.py is imported.
_TMP = tempfile.mkdtemp(prefix="wealth-test-")
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production"
os.environ["WEALTH_DB"] = os.path.join(_TMP, "import-time.db")

import app as app_module  # noqa: E402
import auth  # noqa: E402
from werkzeug.security import check_password_hash  # noqa: E402

PASSWORD = "correct horse battery"


class AuthTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wealth-test-")
        self.db_path = os.path.join(self.dir, "test.db")
        app_module.app.config.update(DATABASE=self.db_path, TESTING=True)
        auth.init_db(self.db_path)

        patches = [
            mock.patch.object(app_module.scoring, "validate_profile", lambda d: (dict(d), {})),
            mock.patch.object(app_module.scoring, "score_profile", lambda p: {"score": 72}),
            mock.patch.object(app_module.scoring, "rule_based_advice", lambda p, a: ["Save more."]),
            mock.patch.object(app_module, "render_template", lambda name, **kw: "rendered " + name),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

        self.alice = app_module.app.test_client()
        self.bob = app_module.app.test_client()

    # helpers
    def register(self, client, username="alice", password=PASSWORD):
        return client.post("/api/register", json={"username": username, "password": password})

    def login(self, client, username="alice", password=PASSWORD):
        return client.post("/api/login", json={"username": username, "password": password})

    def analyze(self, client, income=3000):
        return client.post("/api/analyze", json={"income": income})

    def db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn


class NotLoggedInTests(AuthTestCase):
    def test_personal_endpoints_need_login(self):
        for method, path in [
            ("post", "/api/analyze"), ("post", "/api/advice"), ("post", "/api/whatif"),
            ("get", "/api/records"), ("get", "/api/records/1"), ("delete", "/api/records/1"),
            ("get", "/api/me"),
        ]:
            with self.subTest(path=path):
                kwargs = {"json": {}} if method == "post" else {}
                res = getattr(self.alice, method)(path, **kwargs)
                self.assertEqual(res.status_code, 401)
                self.assertFalse(res.get_json()["ok"])
                self.assertTrue(res.get_json()["login_required"])

    def test_public_endpoints_stay_public(self):
        self.assertEqual(self.alice.get("/api/ping").status_code, 200)
        self.assertNotEqual(self.alice.post("/api/calc/compound", json={}).status_code, 401)

    def test_main_page_redirects_to_login(self):
        res = self.alice.get("/")
        self.assertEqual(res.status_code, 302)
        self.assertTrue(res.headers["Location"].endswith("/login"))

    def test_login_page_and_widget_are_served(self):
        res = self.alice.get("/login")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Create account", res.data)
        res = self.alice.get("/static/auth-widget.js")
        self.assertEqual(res.status_code, 200)
        res.close()

    def test_tampered_cookie_is_rejected(self):
        self.alice.set_cookie("session", "not-a-real-session")
        self.assertEqual(self.alice.get("/api/me").status_code, 401)

    def test_api_responses_are_not_cached(self):
        self.assertEqual(self.alice.get("/api/ping").headers.get("Cache-Control"), "no-store")


class RegisterTests(AuthTestCase):
    def test_register_logs_the_user_in(self):
        res = self.register(self.alice)
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.get_json()["username"], "alice")
        me = self.alice.get("/api/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.get_json()["username"], "alice")

    def test_password_is_stored_hashed(self):
        self.register(self.alice)
        stored = self.db().execute("SELECT password_hash FROM users").fetchone()[0]
        self.assertNotIn(PASSWORD, stored)
        self.assertTrue(check_password_hash(stored, PASSWORD))

    def test_validation_errors_use_field_errors(self):
        res = self.register(self.alice, username="a!", password="short")
        self.assertEqual(res.status_code, 400)
        errors = res.get_json()["field_errors"]
        self.assertIn("username", errors)
        self.assertIn("password", errors)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)

    def test_username_with_trailing_newline_is_rejected(self):
        res = self.alice.post("/api/register", json={"username": "alice\n", "password": PASSWORD})
        # surrounding whitespace is trimmed, but a newline inside the name is not allowed
        res_inner = self.alice.post("/api/register", json={"username": "al\nice", "password": PASSWORD})
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res_inner.status_code, 400)

    def test_password_may_not_equal_username(self):
        res = self.register(self.alice, username="samename1", password="SameName1")
        self.assertEqual(res.status_code, 400)
        self.assertIn("password", res.get_json()["field_errors"])

    def test_duplicate_username_is_case_insensitive(self):
        self.assertEqual(self.register(self.alice, "Alice").status_code, 201)
        res = self.register(self.bob, "alice")
        self.assertEqual(res.status_code, 409)
        self.assertIn("username", res.get_json()["field_errors"])

    def test_non_json_and_wrong_types_are_rejected(self):
        self.assertEqual(self.alice.post("/api/register", data="x=1").status_code, 400)
        res = self.alice.post("/api/register", json={"username": 123, "password": ["x"]})
        self.assertEqual(res.status_code, 400)


class LoginTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.register(self.alice)
        self.alice.post("/api/logout")

    def test_correct_login_then_logout(self):
        res = self.login(self.alice)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.alice.get("/api/me").status_code, 200)
        self.assertEqual(self.alice.post("/api/logout").status_code, 200)
        self.assertEqual(self.alice.get("/api/me").status_code, 401)

    def test_login_is_case_insensitive_for_username(self):
        self.assertEqual(self.login(self.alice, "ALICE").status_code, 200)

    def test_wrong_password_and_unknown_user_look_identical(self):
        wrong_password = self.login(self.alice, "alice", "not the password")
        unknown_user = self.login(self.alice, "nobody", PASSWORD)
        self.assertEqual(wrong_password.status_code, 401)
        self.assertEqual(unknown_user.status_code, 401)
        self.assertEqual(wrong_password.get_json(), unknown_user.get_json())
        self.assertEqual(self.alice.get("/api/me").status_code, 401)

    def test_session_cookie_flags(self):
        res = self.login(self.alice)
        cookie = res.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_main_page_works_when_logged_in_and_login_page_redirects(self):
        self.login(self.alice)
        res = self.alice.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"rendered index.html", res.data)
        self.assertEqual(self.alice.get("/login").status_code, 302)

    def test_session_for_a_deleted_account_stops_working(self):
        self.login(self.alice)
        conn = self.db()
        conn.execute("DELETE FROM users")
        conn.commit()
        self.assertEqual(self.alice.get("/api/me").status_code, 401)

    def test_lockout_after_repeated_failures(self):
        for _ in range(auth.MAX_FAILS_PER_ACCOUNT_AND_IP):
            self.assertEqual(self.login(self.alice, "alice", "wrong password").status_code, 401)
        locked = self.login(self.alice, "alice", PASSWORD)   # even the right password is refused now
        self.assertEqual(locked.status_code, 429)
        self.assertGreater(int(locked.headers["Retry-After"]), 0)
        self.assertEqual(self.alice.get("/api/me").status_code, 401)

    def test_lockout_does_not_block_other_accounts(self):
        self.register(self.bob, "bobby", PASSWORD)
        self.bob.post("/api/logout")
        for _ in range(auth.MAX_FAILS_PER_ACCOUNT_AND_IP):
            self.login(self.alice, "alice", "wrong password")
        self.assertEqual(self.login(self.bob, "bobby", PASSWORD).status_code, 200)

    def test_successful_login_resets_the_failure_count(self):
        for _ in range(auth.MAX_FAILS_PER_ACCOUNT_AND_IP - 1):
            self.login(self.alice, "alice", "wrong password")
        self.assertEqual(self.login(self.alice).status_code, 200)
        self.alice.post("/api/logout")
        for _ in range(auth.MAX_FAILS_PER_ACCOUNT_AND_IP - 1):
            self.assertEqual(self.login(self.alice, "alice", "wrong password").status_code, 401)

    def test_old_failures_expire(self):
        for _ in range(auth.MAX_FAILS_PER_ACCOUNT_AND_IP):
            self.login(self.alice, "alice", "wrong password")
        conn = self.db()
        conn.execute("UPDATE login_failures SET ts = ts - ?", (auth.FAIL_WINDOW_SECONDS + 5,))
        conn.commit()
        self.assertEqual(self.login(self.alice).status_code, 200)

    def test_per_ip_limit_applies_across_usernames(self):
        with mock.patch.object(auth, "MAX_FAILS_PER_IP", 6):
            for i in range(6):
                self.login(self.alice, f"ghost{i}", "x" * 10)
            res = self.login(self.alice, "alice", PASSWORD)
        self.assertEqual(res.status_code, 429)


class AuthorizationTests(AuthTestCase):
    """The core promise: a user can only ever see and change their own data."""

    def setUp(self):
        super().setUp()
        self.register(self.alice, "alice")
        self.register(self.bob, "bobby")
        self.analyze(self.alice, income=1111)
        self.analyze(self.bob, income=2222)
        self.alice_record = self.alice.get("/api/records").get_json()["records"][0]
        self.bob_record = self.bob.get("/api/records").get_json()["records"][0]

    def test_each_user_lists_only_their_own_records(self):
        alice_records = self.alice.get("/api/records").get_json()["records"]
        bob_records = self.bob.get("/api/records").get_json()["records"]
        self.assertEqual([r["profile"]["income"] for r in alice_records], [1111])
        self.assertEqual([r["profile"]["income"] for r in bob_records], [2222])

    def test_cannot_read_another_users_record(self):
        res = self.bob.get(f"/api/records/{self.alice_record['id']}")
        self.assertEqual(res.status_code, 404)
        self.assertNotIn("1111", res.get_data(as_text=True))
        self.assertEqual(self.alice.get(f"/api/records/{self.alice_record['id']}").status_code, 200)

    def test_cannot_delete_another_users_record(self):
        res = self.bob.delete(f"/api/records/{self.alice_record['id']}")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(self.alice.get(f"/api/records/{self.alice_record['id']}").status_code, 200)

    def test_user_can_delete_their_own_record(self):
        res = self.alice.delete(f"/api/records/{self.alice_record['id']}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.alice.get("/api/records").get_json()["records"], [])
        self.assertEqual(len(self.bob.get("/api/records").get_json()["records"]), 1)

    def test_user_id_in_the_request_body_is_ignored(self):
        self.bob.post("/api/analyze", json={"income": 5, "user_id": 1, "userId": 1})
        alice_incomes = [r["profile"]["income"] for r in self.alice.get("/api/records").get_json()["records"]]
        self.assertNotIn(5, alice_incomes)

    def test_advice_saves_text_for_the_right_user(self):
        with mock.patch.object(app_module, "call_ai", lambda p, a: ("gemini", "Pay down the card first.")):
            res = self.alice.post("/api/advice", json={"income": 3000})
        self.assertEqual(res.status_code, 200)
        newest = self.alice.get("/api/records").get_json()["records"][0]
        self.assertEqual(newest["kind"], "advice")
        self.assertEqual(newest["advice_text"], "Pay down the card first.")
        self.assertTrue(all(r["kind"] != "advice" for r in self.bob.get("/api/records").get_json()["records"]))

    def test_advice_falls_back_and_still_saves_when_ai_is_down(self):
        def down(profile, analysis):
            raise app_module.AIUnavailable("Could not reach the AI service.")

        with mock.patch.object(app_module, "call_ai", down):
            res = self.alice.post("/api/advice", json={"income": 3000})
        body = res.get_json()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(body["source"], "rule-based")
        newest = self.alice.get("/api/records").get_json()["records"][0]
        self.assertEqual(newest["kind"], "advice")
        self.assertIsNone(newest["advice_text"])

    def test_history_is_capped_per_user(self):
        with mock.patch.object(auth, "MAX_RECORDS_PER_USER", 3):
            for income in range(10, 15):
                self.analyze(self.alice, income=income)
            incomes = [r["profile"]["income"] for r in self.alice.get("/api/records").get_json()["records"]]
        self.assertEqual(incomes, [14, 13, 12])
        self.assertEqual(len(self.bob.get("/api/records").get_json()["records"]), 1)

    def test_a_failed_save_does_not_break_the_analysis(self):
        conn = self.db()
        conn.execute("DROP TABLE finance_records")   # makes every save fail
        conn.commit()
        with self.assertLogs(app_module.app.logger, level="ERROR"):
            res = self.analyze(self.alice, income=77)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["analysis"], {"score": 72})


if __name__ == "__main__":
    unittest.main()
