"""Login system for the Personal Finance Assistant.

What this module gives the app
  * Accounts: username + password stored in SQLite. Passwords are salted and hashed
    (werkzeug scrypt); the plain password is never stored or logged.
  * Sessions: a signed, HttpOnly cookie that holds only the user's id.
  * Authorization: every saved record belongs to one user_id, and every query for records
    filters on the logged-in user's id taken from the session - never from the request.
  * Brute-force protection: repeated failed logins are throttled.

Endpoints
  GET    /login                page with the "Log in" and "Create account" forms
  POST   /api/register         {username, password} -> creates the account and logs in
  POST   /api/login            {username, password}
  POST   /api/logout
  GET    /api/me               who am I (401 when not logged in)
  GET    /api/records          this user's saved analyses / advice, newest first
  GET    /api/records/<id>     one record, only if it belongs to this user
  DELETE /api/records/<id>     delete one record, only if it belongs to this user

Cross-site request forgery: the cookie is SameSite=Lax (browsers do not send it on cross-site
POST/DELETE requests) and the API only accepts JSON bodies.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import sqlite3
import time
from datetime import timedelta
from functools import wraps

from flask import Blueprint, current_app, g, jsonify, redirect, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

bp = Blueprint("auth", __name__)

USERNAME_RE = re.compile(r"[A-Za-z0-9._-]{3,32}")
PASSWORD_MIN = 8
PASSWORD_MAX = 128

# Failed-login throttling. Counts are kept in the database so they survive restarts and are
# shared by all server processes.
FAIL_WINDOW_SECONDS = 600
MAX_FAILS_PER_ACCOUNT_AND_IP = 5
MAX_FAILS_PER_IP = 30

MAX_RECORDS_PER_USER = 100

# Checked when the username does not exist, so a wrong username takes as long as a wrong
# password and cannot be told apart by timing.
_DUMMY_HASH = generate_password_hash(secrets.token_hex(8))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS finance_records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,
    profile_json  TEXT NOT NULL,
    analysis_json TEXT NOT NULL,
    advice_text   TEXT,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_records_user ON finance_records(user_id, id DESC);
CREATE TABLE IF NOT EXISTS login_failures (
    username TEXT NOT NULL,
    ip       TEXT NOT NULL,
    ts       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_failures_account ON login_failures(username, ip, ts);
CREATE INDEX IF NOT EXISTS idx_failures_ip ON login_failures(ip, ts);
"""


# --------------------------------------------------------------------------- setup

def load_secret_key(base_dir: str) -> str:
    """SECRET_KEY from the environment, else a random key kept in instance/secret_key.

    The key signs the session cookie. Setting SECRET_KEY in .env is best; the file fallback
    just stops a missing key from silently logging everybody out on every restart.
    """
    env_key = os.environ.get("SECRET_KEY", "").strip()
    if env_key:
        return env_key
    folder = os.path.join(base_dir, "instance")
    path = os.path.join(folder, "secret_key")
    os.makedirs(folder, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        for _ in range(20):  # another worker may still be writing it
            with open(path, "r", encoding="utf-8") as fh:
                key = fh.read().strip()
            if key:
                return key
            time.sleep(0.1)
        raise RuntimeError(f"{path} exists but is empty; delete it or set SECRET_KEY.")
    key = secrets.token_hex(32)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key)
    return key


def init_db(path: str) -> None:
    """Create the database file and tables if they do not exist yet."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    is_new = not os.path.exists(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
    if is_new:
        try:
            os.chmod(path, 0o600)  # only the account running the app can read it
        except OSError:
            pass


def init_app(app, base_dir: str) -> None:
    """Configure sessions, register the auth routes and create the database."""
    app.config.setdefault("DATABASE", os.environ.get("WEALTH_DB") or os.path.join(base_dir, "wealth.db"))
    app.config["SECRET_KEY"] = load_secret_key(base_dir)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Set COOKIE_SECURE=1 once the site is served over HTTPS.
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "").strip() == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    )
    app.register_blueprint(bp)
    app.teardown_appcontext(_close_db)
    app.after_request(_no_store_for_api)
    init_db(app.config["DATABASE"])


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def _close_db(_exc) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def _no_store_for_api(response):
    # Personal data must not be kept in browser or proxy caches.
    if request.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


# --------------------------------------------------------------------------- helpers

def _json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def _error(message: str, status: int, **extra):
    return jsonify(ok=False, error=message, **extra), status


def _client_ip() -> str:
    # Behind a reverse proxy remote_addr is the proxy's address; configure the proxy fix-up
    # (werkzeug.middleware.proxy_fix.ProxyFix) before relying on per-IP throttling there.
    return request.remote_addr or "unknown"


def _validate_new_account(username: str, password: str) -> dict:
    errors = {}
    if not USERNAME_RE.fullmatch(username):
        errors["username"] = "Use 3-32 characters: letters, numbers, dot, dash or underscore."
    if len(password) < PASSWORD_MIN:
        errors["password"] = f"Password must be at least {PASSWORD_MIN} characters."
    elif len(password) > PASSWORD_MAX:
        errors["password"] = f"Password must be at most {PASSWORD_MAX} characters."
    elif password.lower() == username.lower():
        errors["password"] = "Password must not be the same as the username."
    return errors


def _start_session(user_id: int) -> None:
    session.clear()  # drop anything left over from a previous login
    session["user_id"] = user_id
    session.permanent = True


def current_user():
    """The logged-in user's row (id, username), or None. Looked up once per request."""
    if "current_user" in g:
        return g.current_user
    user = None
    user_id = session.get("user_id")
    if isinstance(user_id, int):
        user = get_db().execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone()
        if user is None:
            session.clear()  # the account no longer exists
    g.current_user = user
    return user


def login_required(view):
    """Protect an API endpoint: 401 JSON unless a user is logged in."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            return _error("Please log in to continue.", 401, login_required=True)
        return view(*args, **kwargs)

    return wrapped


# --------------------------------------------------------------------------- throttling

def _lockout_seconds(db, username: str, ip: str) -> int:
    """Seconds the caller must wait before trying again, or 0 if allowed."""
    now = time.time()
    db.execute("DELETE FROM login_failures WHERE ts < ?", (now - FAIL_WINDOW_SECONDS,))
    db.commit()
    key = username.lower()[:64]
    checks = (
        ("SELECT COUNT(*), MIN(ts) FROM login_failures WHERE username = ? AND ip = ?", (key, ip),
         MAX_FAILS_PER_ACCOUNT_AND_IP),
        ("SELECT COUNT(*), MIN(ts) FROM login_failures WHERE ip = ?", (ip,), MAX_FAILS_PER_IP),
    )
    wait = 0
    for sql, params, limit in checks:
        count, oldest = db.execute(sql, params).fetchone()
        if count >= limit:
            wait = max(wait, math.ceil(oldest + FAIL_WINDOW_SECONDS - now))
    return max(wait, 1) if wait else 0


def _record_failure(db, username: str, ip: str) -> None:
    db.execute("INSERT INTO login_failures (username, ip, ts) VALUES (?, ?, ?)",
               (username.lower()[:64], ip, time.time()))
    db.commit()


def _clear_failures(db, username: str, ip: str) -> None:
    db.execute("DELETE FROM login_failures WHERE username = ? AND ip = ?", (username.lower()[:64], ip))
    db.commit()


# --------------------------------------------------------------------------- routes

@bp.get("/login")
def login_page():
    if current_user() is not None:
        return redirect("/")
    return render_template("login.html")


@bp.post("/api/register")
def register():
    data = _json_body()
    if data is None:
        return _error("Request body must be a JSON object.", 400)
    raw_username = data.get("username")
    raw_password = data.get("password")
    username = raw_username.strip() if isinstance(raw_username, str) else ""
    password = raw_password if isinstance(raw_password, str) else ""
    field_errors = _validate_new_account(username, password)
    if field_errors:
        return _error("Please fix the highlighted fields.", 400, field_errors=field_errors)
    db = get_db()
    try:
        cur = db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         (username, generate_password_hash(password)))
        db.commit()
    except sqlite3.IntegrityError:
        taken = "That username is already taken."
        return _error(taken, 409, field_errors={"username": taken})
    _start_session(cur.lastrowid)
    return jsonify(ok=True, username=username), 201


@bp.post("/api/login")
def login():
    data = _json_body()
    if data is None:
        return _error("Request body must be a JSON object.", 400)
    raw_username = data.get("username")
    raw_password = data.get("password")
    username = raw_username.strip() if isinstance(raw_username, str) else ""
    password = raw_password if isinstance(raw_password, str) else ""
    db = get_db()
    ip = _client_ip()

    wait = _lockout_seconds(db, username, ip)
    if wait:
        minutes = max(1, math.ceil(wait / 60))
        response = jsonify(ok=False, error=f"Too many failed attempts. Please try again in about {minutes} minute(s).")
        response.status_code = 429
        response.headers["Retry-After"] = str(wait)
        return response

    row = db.execute("SELECT id, username, password_hash FROM users WHERE username = ?", (username,)).fetchone()
    if row is None:
        check_password_hash(_DUMMY_HASH, password)
        valid = False
    else:
        valid = check_password_hash(row["password_hash"], password)
    if not valid:
        _record_failure(db, username, ip)
        current_app.logger.warning("Failed login for %r from %s", username[:64], ip)
        # Same message whether the username or the password was wrong.
        return _error("Wrong username or password.", 401)

    _clear_failures(db, username, ip)
    _start_session(row["id"])
    return jsonify(ok=True, username=row["username"])


@bp.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@bp.get("/api/me")
@login_required
def me():
    return jsonify(ok=True, username=current_user()["username"])


# --------------------------------------------------------------------------- per-user records

def save_record(user_id: int, kind: str, profile: dict, analysis: dict, advice_text: str | None = None):
    """Store one analysis (and advice text) for a user. Never raises: a history problem must
    not stop the user from getting their analysis."""
    try:
        db = get_db()
        cur = db.execute(
            "INSERT INTO finance_records (user_id, kind, profile_json, analysis_json, advice_text) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, kind, json.dumps(profile, default=str), json.dumps(analysis, default=str), advice_text),
        )
        db.execute(
            "DELETE FROM finance_records WHERE user_id = ? AND id NOT IN "
            "(SELECT id FROM finance_records WHERE user_id = ? ORDER BY id DESC LIMIT ?)",
            (user_id, user_id, MAX_RECORDS_PER_USER),
        )
        db.commit()
        return cur.lastrowid
    except (sqlite3.Error, TypeError, ValueError):
        current_app.logger.exception("Could not save a finance record for user %s", user_id)
        return None


def _record_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "created_at": row["created_at"].replace(" ", "T") + "Z",
        "profile": json.loads(row["profile_json"]),
        "analysis": json.loads(row["analysis_json"]),
        "advice_text": row["advice_text"],
    }


@bp.get("/api/records")
@login_required
def list_records():
    rows = get_db().execute(
        "SELECT * FROM finance_records WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (current_user()["id"], MAX_RECORDS_PER_USER),
    ).fetchall()
    return jsonify(ok=True, records=[_record_to_dict(r) for r in rows])


@bp.get("/api/records/<int:record_id>")
@login_required
def get_record(record_id: int):
    # The user id always comes from the session. A record that exists but belongs to someone
    # else is reported as "not found", so its existence is not revealed.
    row = get_db().execute(
        "SELECT * FROM finance_records WHERE id = ? AND user_id = ?",
        (record_id, current_user()["id"]),
    ).fetchone()
    if row is None:
        return _error("Record not found.", 404)
    return jsonify(ok=True, record=_record_to_dict(row))


@bp.delete("/api/records/<int:record_id>")
@login_required
def delete_record(record_id: int):
    db = get_db()
    cur = db.execute("DELETE FROM finance_records WHERE id = ? AND user_id = ?",
                     (record_id, current_user()["id"]))
    db.commit()
    if cur.rowcount == 0:
        return _error("Record not found.", 404)
    return jsonify(ok=True)
