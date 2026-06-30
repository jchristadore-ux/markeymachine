"""Flask control panel for MarkeyMachine — multi-tenant (Phase 1, paper).

Run locally:  DASHBOARD_SECRET_KEY=dev python -m dashboard.app
Production:   served by gunicorn (see railway.dashboard.toml).

Each customer signs up with an email + password and is fully isolated: they see
and control only their own account, enter their own Kalshi key, pick a Trading
Format, and run their own PAPER bot. Live (real-money) trading is disabled in
Phase 1 — it is gated behind an admin + an explicit env flag and is not exposed
to customers — pending the legal/compliance and key-encryption work in Phase 2.
"""

from __future__ import annotations

import functools
import os
import secrets

from flask import (Flask, abort, flash, redirect, render_template, request,
                   session, url_for)

from formats import list_formats, FORMATS, DEFAULT_FORMAT
from .accounts import Account, AccountStore
from .supervisor import Supervisor
from .users import UserStore

app = Flask(__name__)
app.secret_key = os.environ.get("DASHBOARD_SECRET_KEY") or secrets.token_hex(16)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Set DASHBOARD_COOKIE_SECURE=true in production (HTTPS). Off by default so
    # local http and tests work.
    SESSION_COOKIE_SECURE=os.environ.get("DASHBOARD_COOKIE_SECURE", "").lower() == "true",
)

# Live trading is OFF unless an admin explicitly enables it for the whole site.
# Phase 1 is paper-only; this stays false until Phase 2 (legal + key encryption).
LIVE_ENABLED = os.environ.get("DASHBOARD_ALLOW_LIVE", "").lower() == "true"

users = UserStore()
store = AccountStore()
supervisor = Supervisor()


# ── auth plumbing ─────────────────────────────────────────────────────────────
def current_user():
    uid = session.get("user_id")
    return users.get(uid) if uid else None


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        u = current_user()
        if not u:
            return redirect(url_for("login", next=request.path))
        if not u.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_user():
    u = current_user()
    return {"current_user": u.public_dict() if u else None}


def current_account() -> Account:
    """The logged-in user's account (created on first need). All account access
    goes through here so a user can only ever touch their own data."""
    return store.ensure_for_user(current_user().id)


# ── signup / login / logout ───────────────────────────────────────────────────
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user():
        return redirect(url_for("index"))
    if request.method == "POST":
        try:
            user = users.create(request.form.get("email", ""),
                                 request.form.get("password", ""))
        except ValueError as e:
            flash(str(e), "error")
            return render_template("signup.html", email=request.form.get("email", ""))
        store.ensure_for_user(user.id)
        session.clear()
        session["user_id"] = user.id
        flash("Welcome! Add your Kalshi key in Settings to get started.", "ok")
        return redirect(url_for("index"))
    return render_template("signup.html", email="")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("index"))
    if request.method == "POST":
        user = users.verify(request.form.get("email", ""), request.form.get("password", ""))
        if user:
            session.clear()
            session["user_id"] = user.id
            nxt = request.args.get("next") or url_for("index")
            return redirect(nxt)
        flash("Incorrect email or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── dashboard ─────────────────────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    account = current_account()
    status = supervisor.status(account)
    fmt = FORMATS.get(account.trading_format, FORMATS[DEFAULT_FORMAT])
    return render_template(
        "dashboard.html",
        account=account.public_dict(),
        status=status,
        format_spec=fmt,
        live_enabled=LIVE_ENABLED,
    )


@app.route("/api/status")
@login_required
def api_status():
    return supervisor.status(current_account())


@app.route("/formats")
@login_required
def formats_page():
    account = current_account()
    return render_template(
        "formats.html",
        account=account.public_dict(),
        formats=list_formats(),
        running=supervisor.is_running(account),
    )


@app.route("/formats/select", methods=["POST"])
@login_required
def select_format():
    account = current_account()
    name = request.form.get("trading_format", "")
    if name not in FORMATS:
        flash("Unknown format.", "error")
        return redirect(url_for("formats_page"))
    account.trading_format = name
    store.update(account)
    msg = f"Format set to {FORMATS[name]['display_name']}."
    if supervisor.is_running(account):
        msg += " Restart the worker for it to take effect."
    flash(msg, "ok")
    return redirect(url_for("formats_page"))


@app.route("/settings")
@login_required
def settings_page():
    account = current_account()
    return render_template(
        "settings.html",
        account=account.public_dict(),
        running=supervisor.is_running(account),
        live_enabled=LIVE_ENABLED,
    )


@app.route("/settings/save", methods=["POST"])
@login_required
def save_settings():
    account = current_account()
    account.ensure_dirs()
    account.label = request.form.get("label", account.label).strip() or account.label
    account.kalshi_key_id = request.form.get("kalshi_key_id", account.kalshi_key_id).strip()

    pem = request.form.get("kalshi_pem", "").strip()
    if pem:
        pem_path = os.path.join(account.state_dir, "kalshi.pem")
        with open(pem_path, "w") as f:
            f.write(pem)
        os.chmod(pem_path, 0o600)
        account.kalshi_pem_path = pem_path

    account.telegram_bot_token = request.form.get(
        "telegram_bot_token", account.telegram_bot_token).strip()
    account.telegram_chat_id = request.form.get(
        "telegram_chat_id", account.telegram_chat_id).strip()
    try:
        account.paper_balance = float(request.form.get("paper_balance", account.paper_balance))
    except ValueError:
        pass

    store.update(account)
    flash("Settings saved.", "ok")
    return redirect(url_for("settings_page"))


@app.route("/settings/mode", methods=["POST"])
@login_required
def set_mode():
    """Phase 1 is paper-only. Live is gated behind both the site-wide
    DASHBOARD_ALLOW_LIVE flag and an admin user; everyone else is forced to
    paper. (Even when enabled, the supervisor still requires per-account opt-in.)"""
    account = current_account()
    target = request.form.get("mode", "paper")
    user = current_user()
    if target == "live":
        if not (LIVE_ENABLED and user.is_admin):
            abort(403)
        if request.form.get("confirm", "") != "LIVE":
            flash("Type LIVE to confirm switching to real-money trading.", "error")
            return redirect(url_for("settings_page"))
        account.demo_mode = False
        flash("⚠️ LIVE mode enabled — real money. Restart the worker to apply.", "error")
    else:
        account.demo_mode = True
        flash("Switched to PAPER (demo) mode. Restart the worker to apply.", "ok")
    store.update(account)
    return redirect(url_for("settings_page"))


# ── controls ──────────────────────────────────────────────────────────────────
@app.route("/control/<action>", methods=["POST"])
@login_required
def control(action):
    account = current_account()
    try:
        if action == "start":
            started = supervisor.start(account)
            flash("Worker started." if started else "Worker already running.", "ok")
        elif action == "stop":
            supervisor.stop(account)
            flash("Worker stopped.", "ok")
        elif action == "restart":
            supervisor.restart(account)
            flash("Worker restarted.", "ok")
        else:
            abort(404)
    except RuntimeError as e:
        flash(str(e), "error")
    return redirect(request.referrer or url_for("index"))


# ── admin (operator) overview ─────────────────────────────────────────────────
@app.route("/admin")
@admin_required
def admin():
    rows = []
    for acct in store.all():
        owner = users.get(acct.owner_user_id)
        rows.append({
            "account": acct.public_dict(),
            "owner_email": owner.email if owner else "(unknown)",
            "running": supervisor.is_running(acct),
        })
    return render_template("admin.html", rows=rows, user_count=len(users.all()))


def main():
    host = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", os.environ.get("DASHBOARD_PORT", "8080")))
    app.run(host=host, port=port)


if __name__ == "__main__":
    main()
