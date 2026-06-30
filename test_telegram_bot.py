"""test_telegram_bot.py — operator Telegram command bot: rule-based analyzer,
authorization, the confirm-before-apply flow, and the /format, /set, and control
actions. No real Telegram, processes, or Kalshi creds needed.
"""

import pytest

from dashboard.accounts import Account, AccountStore
from dashboard.telegram_bot import CommandHandler, build_analysis


# ── fake supervisor (duck-typed; records control calls) ───────────────────────
class FakeSup:
    def __init__(self):
        self.running = False
        self._crashed = False
        self.snap = None
        self.log = []
        self.calls = []

    def crashed(self, a):       return self._crashed
    def is_running(self, a):    return self.running
    def status(self, a):        return {"running": self.running, "snapshot": self.snap}
    def tail_log(self, a, n=50): return self.log
    def start(self, a):   self.calls.append(("start", a.id));   self.running = True;  return True
    def stop(self, a):    self.calls.append(("stop", a.id));    self.running = False; return True
    def restart(self, a): self.calls.append(("restart", a.id)); self.running = True;  return True


@pytest.fixture
def setup(tmp_path):
    store = AccountStore(path=str(tmp_path / "accounts.json"))
    acct = store.add(Account(label="Bot A", owner_user_id="u1"))
    sup = FakeSup()
    handler = CommandHandler(sup, store, authorized_chats={"123"})
    return handler, sup, store, acct


# ── analyzer ──────────────────────────────────────────────────────────────────
def test_analysis_crashed_recommends_restart():
    findings, rec = build_analysis({"state": "crashed"}, None, [])
    assert rec == "/restart"
    assert any("CRASHED" in f for f in findings)


def test_analysis_low_winrate_recommends_conservative():
    snap = {"wins": 4, "losses": 11, "win_rate": 27.0, "wilson_ci": [12, 46]}
    findings, rec = build_analysis({"state": "healthy"}, snap, [])
    assert rec == "/format conservative"


def test_analysis_healthy_no_rec():
    snap = {"wins": 15, "losses": 5, "win_rate": 75.0, "wilson_ci": [56, 88]}
    findings, rec = build_analysis({"state": "healthy"}, snap, [])
    assert rec is None


# ── authorization ─────────────────────────────────────────────────────────────
def test_unauthorized_chat_ignored(setup):
    handler, sup, store, acct = setup
    assert handler.handle("999", "/status") is None
    assert handler.handle("123", "/help").startswith("MarkeyMachine")


# ── read commands ─────────────────────────────────────────────────────────────
def test_status_lists_account(setup):
    handler, sup, store, acct = setup
    out = handler.handle("123", "/status")
    assert "Bot A" in out and acct.id in out


def test_logs_returns_tail(setup):
    handler, sup, store, acct = setup
    sup.log = ["line1", "boom Traceback"]
    out = handler.handle("123", "/logs")
    assert "boom Traceback" in out


# ── confirm-before-apply: format ──────────────────────────────────────────────
def test_format_requires_confirm(setup):
    handler, sup, store, acct = setup
    staged = handler.handle("123", "/format aggressive")
    assert "Staged" in staged
    # not applied yet
    assert store.get(acct.id).trading_format == "balanced"
    # confirm applies
    applied = handler.handle("123", "/confirm")
    assert "aggressive" in applied
    assert store.get(acct.id).trading_format == "aggressive"


def test_format_restarts_running_worker_on_confirm(setup):
    handler, sup, store, acct = setup
    sup.running = True
    handler.handle("123", "/format conservative")
    handler.handle("123", "/confirm")
    assert ("restart", acct.id) in sup.calls


def test_unknown_format_rejected(setup):
    handler, sup, store, acct = setup
    assert "Unknown format" in handler.handle("123", "/format nonsense")


# ── confirm-before-apply: set ─────────────────────────────────────────────────
def test_set_param_applies_on_confirm(setup):
    handler, sup, store, acct = setup
    handler.handle("123", "/set OB_IMBALANCE_THRESH 0.8")
    handler.handle("123", "/confirm")
    assert store.get(acct.id).overrides["OB_IMBALANCE_THRESH"] == "0.8"


def test_set_rejects_unknown_param(setup):
    handler, sup, store, acct = setup
    out = handler.handle("123", "/set NOT_A_PARAM 5")
    assert "not an adjustable parameter" in out
    assert handler.handle("123", "/confirm") == "Nothing staged."


def test_set_rejects_bad_value(setup):
    handler, sup, store, acct = setup
    out = handler.handle("123", "/set MIN_CONFIDENCE abc")
    assert "must be a int" in out


# ── control + cancel/expiry ───────────────────────────────────────────────────
def test_pause_requires_confirm(setup):
    handler, sup, store, acct = setup
    sup.running = True
    handler.handle("123", "/pause")
    assert ("stop", acct.id) not in sup.calls   # not yet
    handler.handle("123", "/confirm")
    assert ("stop", acct.id) in sup.calls


def test_cancel_discards(setup):
    handler, sup, store, acct = setup
    handler.handle("123", "/format aggressive")
    assert handler.handle("123", "/cancel") == "Discarded."
    assert handler.handle("123", "/confirm") == "Nothing staged."


def test_analyze_runs(setup):
    handler, sup, store, acct = setup
    sup.snap = {"wins": 3, "losses": 12, "win_rate": 20.0, "wilson_ci": [8, 42]}
    out = handler.handle("123", "/analyze")
    assert "Analysis" in out and "Recommended" in out
