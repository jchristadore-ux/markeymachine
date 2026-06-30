"""Flask control panel for MarkeyMachine.

Run:  DASHBOARD_PASSWORD=secret python -m dashboard.app

Single admin password (session cookie). Single account shown today; the routes
take an account id so a multi-tenant console is an incremental step. Defaults to
PAPER everywhere — switching an account to LIVE requires typing a confirmation.
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

app = Flask(__name__)
app.secret_key = os.environ.get("DASHBOARD_SECRET_KEY") or secrets.token_hex(16)

# A blank password disables auth only for local dev; warn loudly.
ADMIN_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

store = AccountStore()
supervisor = Supervisor()


# ── auth ──────────────────────────────────────────────────────────────────────
def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if ADMIN_PASSWORD and not session.get("authed"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if not ADMIN_PASSWORD:
        session["authed"] = True
        return redirect(url_for("index"))
    if request.method == "POST":
        if secrets.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD):
            session["authed"] = True
            return redirect(request.args.get("next") or url_for("index"))
        flash("Incorrect password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── helpers ───────────────────────────────────────────────────────────────────
def _account(account_id: str | None) -> Account:
    if account_id:
        acct = store.get(account_id)
        if not acct:
            abort(404)
        return acct
    return store.ensure_default()


# ── pages ─────────────────────────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    account = store.ensure_default()
    status = supervisor.status(account)
    fmt = FORMATS.get(account.trading_format, FORMATS[DEFAULT_FORMAT])
    return render_template(
        "dashboard.html",
        account=account.public_dict(),
        status=status,
        format_spec=fmt,
        password_set=bool(ADMIN_PASSWORD),
    )


@app.route("/api/status")
@login_required
def api_status():
    account = store.ensure_default()
    return supervisor.status(account)


@app.route("/formats")
@login_required
def formats_page():
    account = store.ensure_default()
    return render_template(
        "formats.html",
        account=account.public_dict(),
        formats=list_formats(),
        running=supervisor.is_running(account),
    )


@app.route("/formats/select", methods=["POST"])
@login_required
def select_format():
    account = store.ensure_default()
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
    account = store.ensure_default()
    return render_template(
        "settings.html",
        account=account.public_dict(),
        running=supervisor.is_running(account),
    )


@app.route("/settings/save", methods=["POST"])
@login_required
def save_settings():
    account = store.ensure_default()
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
    """Switch an account between PAPER and LIVE. Going live demands typed
    confirmation so it can never happen by a stray click."""
    account = store.ensure_default()
    target = request.form.get("mode", "paper")
    if target == "live":
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
    account = store.ensure_default()
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


def main():
    host = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", os.environ.get("DASHBOARD_PORT", "8080")))
    if not ADMIN_PASSWORD:
        app.logger.warning("DASHBOARD_PASSWORD is not set — the dashboard is UNAUTHENTICATED.")
    app.run(host=host, port=port)


if __name__ == "__main__":
    main()
