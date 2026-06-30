"""test_dashboard.py — multi-tenant dashboard: users/auth, per-user account
scoping, supervisor env composition, forced-paper safety, and the Flask routes.
Does not import bot.py, so no Kalshi credentials are needed.
"""

import importlib
import json
import os

import pytest

from dashboard.accounts import Account, AccountStore
from dashboard.supervisor import Supervisor, compose_env
from dashboard.users import UserStore


# ── user store / auth ─────────────────────────────────────────────────────────
def test_user_create_and_verify(tmp_path):
    us = UserStore(path=str(tmp_path / "users.json"))
    u = us.create("Alice@Example.com", "supersecret")
    assert u.email == "alice@example.com"           # normalized
    assert u.password_hash and "supersecret" not in u.password_hash  # hashed
    assert us.verify("alice@example.com", "supersecret").id == u.id
    assert us.verify("alice@example.com", "wrong") is None


def test_user_hash_is_pbkdf2(tmp_path):
    # pbkdf2 avoids the scrypt/maxmem container failures.
    u = UserStore(path=str(tmp_path / "users.json")).create("a@b.com", "password1")
    assert u.password_hash.startswith("pbkdf2:")


def test_login_works_across_store_instances(tmp_path):
    # Simulates two gunicorn workers: user created via one store, verified via a
    # second store pointed at the same file. Before the reload-on-read fix this
    # returned None → the deployed "Incorrect email or password" bug.
    path = str(tmp_path / "users.json")
    UserStore(path=path).create("a@b.com", "password1")
    other_worker = UserStore(path=path)
    assert other_worker.verify("a@b.com", "password1") is not None
    assert other_worker.get_by_email("a@b.com") is not None


def test_user_duplicate_email_rejected(tmp_path):
    us = UserStore(path=str(tmp_path / "users.json"))
    us.create("a@b.com", "password1")
    with pytest.raises(ValueError):
        us.create("A@B.COM", "password2")


def test_user_validation(tmp_path):
    us = UserStore(path=str(tmp_path / "users.json"))
    with pytest.raises(ValueError):
        us.create("not-an-email", "password1")
    with pytest.raises(ValueError):
        us.create("a@b.com", "short")   # < 8 chars


def test_admin_flag_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@corp.com")
    us = UserStore(path=str(tmp_path / "users.json"))
    assert us.create("boss@corp.com", "password1").is_admin is True
    assert us.create("user@corp.com", "password1").is_admin is False


def test_users_persist(tmp_path):
    p = str(tmp_path / "users.json")
    UserStore(path=p).create("a@b.com", "password1")
    assert UserStore(path=p).get_by_email("a@b.com") is not None


# ── account scoping ───────────────────────────────────────────────────────────
def test_accounts_scoped_to_user(tmp_path):
    store = AccountStore(path=str(tmp_path / "accounts.json"))
    a = store.add(Account(label="A", owner_user_id="u1"))
    store.add(Account(label="B", owner_user_id="u2"))
    assert [x.id for x in store.for_user("u1")] == [a.id]
    assert len(store.for_user("u2")) == 1
    assert store.for_user("nobody") == []


def test_ensure_for_user_is_idempotent(tmp_path):
    store = AccountStore(path=str(tmp_path / "accounts.json"))
    a1 = store.ensure_for_user("u1")
    a2 = store.ensure_for_user("u1")
    assert a1.id == a2.id
    assert len(store.for_user("u1")) == 1


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


# ── supervisor env composition + forced-paper safety ──────────────────────────
def test_compose_env_sets_format_and_isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    pem = tmp_path / "k.pem"
    pem.write_text("-----BEGIN KEY-----\nx\n-----END KEY-----\n")
    acct = Account(label="A", id="acct123", kalshi_key_id="KID",
                   kalshi_pem_path=str(pem), trading_format="aggressive")
    env = compose_env(acct)
    assert env["TRADING_FORMAT"] == "aggressive"
    assert env["KALSHI_API_KEY_ID"] == "KID"
    assert "BEGIN KEY" in env["KALSHI_PRIVATE_KEY_PEM"]
    for key in ("RECOVERY_STATE_PATH", "PROBATION_STATE_PATH", "LADDER_STATE_PATH",
                "BUCKET_STATS_PATH", "STATUS_SNAPSHOT_PATH"):
        assert acct.id in env[key]


def test_compose_env_forces_paper_when_live_not_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DASHBOARD_ALLOW_LIVE", raising=False)
    acct = Account(label="A", demo_mode=False)   # account says live...
    assert compose_env(acct)["DEMO_MODE"] == "true"   # ...but site forces paper


def test_compose_env_allows_live_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DASHBOARD_ALLOW_LIVE", "true")
    assert compose_env(Account(label="A", demo_mode=False))["DEMO_MODE"] == "false"
    assert compose_env(Account(label="A", demo_mode=True))["DEMO_MODE"] == "true"


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
    with open(os.path.join(acct.state_dir, "status.json"), "w") as f:
        json.dump({"balance": 1234.5}, f)
    assert Supervisor().status(acct)["snapshot"]["balance"] == 1234.5


def test_supervisor_start_requires_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    with pytest.raises(RuntimeError):
        Supervisor().start(Account(label="A"))


# ── flask app: signup / login / scoping ───────────────────────────────────────
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DASHBOARD_ALLOW_LIVE", raising=False)
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-secret")
    import dashboard.app as appmod
    importlib.reload(appmod)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def _signup(client, email="user@example.com", password="password1"):
    return client.post("/signup", data={"email": email, "password": password},
                       follow_redirects=True)


def test_requires_login(client):
    r = client.get("/")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_signup_then_dashboard(client):
    r = _signup(client)
    assert r.status_code == 200
    r = client.get("/")
    assert b"Balance" in r.data
    # A paper account was auto-created for this user.
    import dashboard.app as appmod
    assert len(appmod.store.all()) == 1


def test_signup_duplicate_blocked(client):
    _signup(client)
    client.get("/logout")
    r = client.post("/signup", data={"email": "user@example.com", "password": "password1"},
                    follow_redirects=True)
    assert b"already exists" in r.data


def test_login_logout(client):
    _signup(client)
    client.get("/logout")
    assert client.get("/").status_code == 302   # logged out
    r = client.post("/login", data={"email": "user@example.com", "password": "password1"},
                    follow_redirects=True)
    assert b"Balance" in r.data


def test_signup_unexpected_error_is_surfaced(client, monkeypatch):
    # A non-validation failure (e.g. unwritable data dir / hashing) must not 500;
    # it shows a friendly message and is logged, not swallowed silently.
    import dashboard.app as appmod

    def boom(*a, **k):
        raise OSError("disk on fire")
    monkeypatch.setattr(appmod.users, "create", boom)
    r = client.post("/signup", data={"email": "x@y.com", "password": "password1"},
                    follow_redirects=True)
    assert r.status_code == 200
    assert b"Couldn&#39;t create your account" in r.data or b"Couldn't create your account" in r.data


def test_users_only_see_their_own_account(client):
    # User A signs up and names their account.
    _signup(client, "a@x.com")
    client.post("/settings/save", data={"label": "Alice Account"}, follow_redirects=True)
    client.get("/logout")
    # User B signs up — must NOT see Alice's account.
    _signup(client, "b@x.com")
    r = client.get("/")
    assert b"Alice Account" not in r.data
    import dashboard.app as appmod
    assert len(appmod.store.all()) == 2          # two isolated accounts exist
    bob = appmod.users.get_by_email("b@x.com")
    assert len(appmod.store.for_user(bob.id)) == 1


def test_select_format_scoped(client):
    _signup(client)
    client.post("/formats/select", data={"trading_format": "aggressive"},
                follow_redirects=True)
    import dashboard.app as appmod
    user = appmod.users.get_by_email("user@example.com")
    assert appmod.store.for_user(user.id)[0].trading_format == "aggressive"


def test_live_toggle_blocked_in_paper_phase(client):
    _signup(client)
    # Live is not enabled site-wide → switching to live is forbidden.
    r = client.post("/settings/mode", data={"mode": "live", "confirm": "LIVE"})
    assert r.status_code == 403
    import dashboard.app as appmod
    user = appmod.users.get_by_email("user@example.com")
    assert appmod.store.for_user(user.id)[0].demo_mode is True


def test_admin_route_forbidden_for_normal_user(client):
    _signup(client)
    assert client.get("/admin").status_code == 403
