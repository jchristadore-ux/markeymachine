"""test_monitoring.py — watchdog classifier, /health + admin log endpoints,
stateless Telegram notify, and the Railway logs helper. No Kalshi creds or real
network/processes required.
"""

import importlib
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from dashboard.accounts import Account, AccountStore
from dashboard.supervisor import Supervisor
from dashboard.watchdog import Watchdog, evaluate


def _utc(offset_secs=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_secs)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _seed(account, *, pid=None, status=None):
    account.ensure_dirs()
    if pid is not None:
        with open(Supervisor()._pidfile(account), "w") as f:
            f.write(str(pid))
    if status is not None:
        with open(os.path.join(account.state_dir, "status.json"), "w") as f:
            json.dump(status, f)


# ── classifier ────────────────────────────────────────────────────────────────
def test_state_stopped(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    acct = Account(label="A")
    acct.ensure_dirs()
    assert evaluate(acct, Supervisor(), 180)["state"] == "stopped"


def test_state_crashed(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    acct = Account(label="A")
    # pidfile points at a PID that is not alive → crashed (no clean stop).
    _seed(acct, pid=2147480000)
    assert evaluate(acct, Supervisor(), 180)["state"] == "crashed"


def test_state_healthy_and_stalled(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    acct = Account(label="A")
    # Use our own live PID so the worker looks "running".
    _seed(acct, pid=os.getpid(), status={"updated_at": _utc(0), "balance": 100})
    assert evaluate(acct, Supervisor(), 180)["state"] == "healthy"
    # Now make the snapshot old → stalled.
    with open(os.path.join(acct.state_dir, "status.json"), "w") as f:
        json.dump({"updated_at": _utc(-9999), "balance": 100}, f)
    h = evaluate(acct, Supervisor(), 180)
    assert h["state"] == "stalled"
    assert h["stale_seconds"] > 180


# ── watchdog transitions / alerts ─────────────────────────────────────────────
def test_watchdog_alerts_on_transition_only(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WATCHDOG_AUTORESTART", "false")
    store = AccountStore(path=str(tmp_path / "accounts.json"))
    acct = store.add(Account(label="A", owner_user_id="u1"))
    _seed(acct, pid=2147480000)  # crashed

    sent = []
    wd = Watchdog(Supervisor(), store, notify=sent.append)
    wd.check_once()
    wd.check_once()  # same state — must not alert again
    assert len(sent) == 1
    assert "crashed" in sent[0].lower()


def test_watchdog_autorestart_requires_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WATCHDOG_AUTORESTART", "true")
    store = AccountStore(path=str(tmp_path / "accounts.json"))
    acct = store.add(Account(label="A", owner_user_id="u1"))  # no creds
    _seed(acct, pid=2147480000)
    sent = []
    Watchdog(Supervisor(), store, notify=sent.append).check_once()
    # No creds → cannot auto-restart; alerts that it is down.
    assert any("down" in m.lower() for m in sent)


# ── telegram notify (stateless) ───────────────────────────────────────────────
def test_telegram_notify_builds_request(monkeypatch):
    import telegram_utils as tg
    captured = {}

    class _Resp:
        status_code = 200

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(tg.requests, "post", fake_post)
    assert tg.notify("TOKEN123", "CHAT456", "hello") is True
    assert "botTOKEN123/sendMessage" in captured["url"]
    assert captured["json"] == {"chat_id": "CHAT456", "text": "hello"}
    # Missing token/chat → no send.
    assert tg.notify("", "CHAT", "x") is False


# ── flask: /health + admin logs ───────────────────────────────────────────────
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-secret")
    monkeypatch.setenv("DASHBOARD_WATCHDOG", "false")  # don't spawn the thread in tests
    monkeypatch.setenv("MONITOR_TOKEN", "secret-monitor-token")
    monkeypatch.setenv("ADMIN_EMAILS", "admin@x.com")
    import dashboard.app as appmod
    importlib.reload(appmod)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def test_health_requires_token(client):
    assert client.get("/health").status_code == 401          # token set, none supplied
    assert client.get("/health?token=wrong").status_code == 401


def test_health_disabled_without_monitor_token(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "s")
    monkeypatch.setenv("DASHBOARD_WATCHDOG", "false")
    monkeypatch.delenv("MONITOR_TOKEN", raising=False)
    import dashboard.app as appmod
    importlib.reload(appmod)
    appmod.app.config["TESTING"] = True
    assert appmod.app.test_client().get("/health?token=anything").status_code == 404


def test_health_reports_accounts(client):
    import dashboard.app as appmod
    user = appmod.users.create("u@x.com", "password1")
    acct = appmod.store.ensure_for_user(user.id)
    _seed(acct, pid=os.getpid(), status={"updated_at": _utc(0), "balance": 42.0,
                                          "active_mode": "normal"})
    r = client.get("/health?token=secret-monitor-token")
    assert r.status_code == 200
    data = r.get_json()
    assert data["accounts"][0]["state"] == "healthy"
    assert data["accounts"][0]["owner_email"] == "u@x.com"


def test_health_token_via_header(client):
    r = client.get("/health", headers={"X-Monitor-Token": "secret-monitor-token"})
    assert r.status_code == 200


def test_admin_logs_scoped(client):
    import dashboard.app as appmod
    # normal user forbidden
    appmod.users.create("u@x.com", "password1")
    client.post("/login", data={"email": "u@x.com", "password": "password1"})
    assert client.get("/admin/logs").status_code == 403
    client.get("/logout")
    # admin allowed, sees the log tail
    admin = appmod.users.create("admin@x.com", "password1")
    acct = appmod.store.ensure_for_user(admin.id)
    acct.ensure_dirs()
    with open(appmod.supervisor._logfile(acct), "w") as f:
        f.write("line one\nTraceback boom\n")
    client.post("/login", data={"email": "admin@x.com", "password": "password1"})
    r = client.get("/admin/logs")
    assert r.status_code == 200 and b"Traceback boom" in r.data
    j = client.get(f"/admin/api/logs?account={acct.id}&lines=10").get_json()
    assert "Traceback boom" in j["lines"]


# ── railway logs helper ───────────────────────────────────────────────────────
def test_railway_client_fetches_logs():
    from tools.railway_logs import RailwayClient, format_logs

    class _Resp:
        status_code = 200
        text = ""

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    calls = []

    def fake_post(url, headers, json_body):
        calls.append((url, headers, json_body))
        if "deployments(" in json_body["query"]:
            return _Resp({"data": {"deployments": {"edges": [{"node": {"id": "dep1"}}]}}})
        return _Resp({"data": {"deploymentLogs": [
            {"timestamp": "t1", "severity": "info", "message": "hello"},
            {"timestamp": "t2", "severity": "error", "message": "boom"},
        ]}})

    client = RailwayClient("tok", post_fn=fake_post)
    dep = client.latest_deployment_id("p", "e", "s")
    assert dep == "dep1"
    logs = client.deployment_logs(dep, 50)
    assert "Bearer tok" in calls[0][1]["Authorization"]
    assert "boom" in format_logs(logs)


def test_railway_client_requires_token():
    from tools.railway_logs import RailwayClient, RailwayError
    with pytest.raises(RailwayError):
        RailwayClient("")
