"""test_dashboard.py — account store, worker supervisor env composition, and the
Flask control panel. Does not import bot.py, so no Kalshi credentials are needed.
"""

import json
import os

import pytest

from dashboard.accounts import Account, AccountStore
from dashboard.supervisor import Supervisor, compose_env


# ── account store ─────────────────────────────────────────────────────────────
def test_store_add_get_update(tmp_path):
    store = AccountStore(path=str(tmp_path / "accounts.json"))
    acct = store.add(Account(label="Client A"))
    assert store.get(acct.id).label == "Client A"
    acct.trading_format = "aggressive"
    store.update(acct)
    # Reload from disk to prove persistence.
    store2 = AccountStore(path=str(tmp_path / "accounts.json"))
    assert store2.get(acct.id).trading_format == "aggressive"


def test_store_is_a_list_multitenant(tmp_path):
    store = AccountStore(path=str(tmp_path / "accounts.json"))
    store.add(Account(label="A"))
    store.add(Account(label="B"))
    assert len(store.all()) == 2


def test_ensure_default_creates_one(tmp_path):
    store = AccountStore(path=str(tmp_path / "accounts.json"))
    acct = store.ensure_default()
    assert acct is not None
    assert len(store.all()) == 1
    # idempotent
    assert store.ensure_default().id == acct.id


def test_public_dict_hides_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    acct = Account(label="A", kalshi_key_id="abcd1234efgh5678",
                   kalshi_pem_path=str(tmp_path / "k.pem"),
                   telegram_bot_token="secret-token")
    pub = acct.public_dict()
    assert "kalshi_pem_path" not in pub
    assert "telegram_bot_token" not in pub
    assert pub["kalshi_key_id_masked"].startswith("abcd")
    assert "secret-token" not in json.dumps(pub)


# ── supervisor env composition ────────────────────────────────────────────────
def test_compose_env_sets_format_and_isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    pem = tmp_path / "k.pem"
    pem.write_text("-----BEGIN KEY-----\nx\n-----END KEY-----\n")
    acct = Account(label="A", id="acct123", kalshi_key_id="KID",
                   kalshi_pem_path=str(pem), trading_format="aggressive",
                   demo_mode=True)
    env = compose_env(acct)

    assert env["TRADING_FORMAT"] == "aggressive"
    assert env["DEMO_MODE"] == "true"
    assert env["KALSHI_API_KEY_ID"] == "KID"
    assert "BEGIN KEY" in env["KALSHI_PRIVATE_KEY_PEM"]
    # Every state path is pinned under this account's own directory.
    for key in ("RECOVERY_STATE_PATH", "PROBATION_STATE_PATH", "LADDER_STATE_PATH",
                "BUCKET_STATS_PATH", "STATUS_SNAPSHOT_PATH"):
        assert acct.id in env[key], f"{key} not isolated to the account dir"


def test_compose_env_live_mode_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    acct = Account(label="A", demo_mode=False)
    assert compose_env(acct)["DEMO_MODE"] == "false"


def test_compose_env_omits_telegram_when_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    acct = Account(label="A")
    env = compose_env(acct)
    assert "TELEGRAM_BOT_TOKEN" not in env


# ── supervisor status / liveness ──────────────────────────────────────────────
def test_supervisor_not_running_initially(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    acct = Account(label="A")
    acct.ensure_dirs()
    sup = Supervisor()
    assert sup.is_running(acct) is False
    assert sup.status(acct)["snapshot"] is None


def test_supervisor_reads_status_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    acct = Account(label="A")
    acct.ensure_dirs()
    snap = {"balance": 1234.5, "active_mode": "normal"}
    with open(os.path.join(acct.state_dir, "status.json"), "w") as f:
        json.dump(snap, f)
    sup = Supervisor()
    assert sup.status(acct)["snapshot"]["balance"] == 1234.5


def test_supervisor_start_requires_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    acct = Account(label="A")  # no creds
    sup = Supervisor()
    with pytest.raises(RuntimeError):
        sup.start(acct)


# ── flask app ─────────────────────────────────────────────────────────────────
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    # Import inside the fixture so DASHBOARD_DATA_DIR is honored per-test.
    import importlib
    import dashboard.app as appmod
    importlib.reload(appmod)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def test_requires_login(client):
    r = client.get("/")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_login_and_dashboard(client):
    r = client.post("/login", data={"password": "pw"}, follow_redirects=True)
    assert r.status_code == 200
    r = client.get("/")
    assert b"Balance" in r.data


def test_select_format(client):
    client.post("/login", data={"password": "pw"})
    r = client.post("/formats/select", data={"trading_format": "aggressive"},
                    follow_redirects=True)
    assert r.status_code == 200
    import dashboard.app as appmod
    assert appmod.store.ensure_default().trading_format == "aggressive"


def test_live_requires_typed_confirmation(client):
    client.post("/login", data={"password": "pw"})
    import dashboard.app as appmod
    # Without confirmation, stays paper.
    client.post("/settings/mode", data={"mode": "live"}, follow_redirects=True)
    assert appmod.store.ensure_default().demo_mode is True
    # With the exact confirmation, flips to live.
    client.post("/settings/mode", data={"mode": "live", "confirm": "LIVE"},
                follow_redirects=True)
    assert appmod.store.ensure_default().demo_mode is False
