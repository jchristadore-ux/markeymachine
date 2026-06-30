"""Worker supervisor.

Runs the unmodified `bot.py` as a subprocess, one per account, with a fully
composed environment (Kalshi creds + the chosen Trading Format + per-account
state-file paths so workers never collide). Tracks the process, exposes
start/stop/restart, and reads the worker's status snapshot for the dashboard.

bot.py is treated as an opaque worker — nothing here imports it — so the trading
logic and this control plane stay decoupled.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from typing import Dict, Optional

from .accounts import Account

# Repo root (parent of the dashboard package) — where bot.py lives.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _state_path(account: Account, name: str) -> str:
    return os.path.join(account.state_dir, name)


def compose_env(account: Account) -> Dict[str, str]:
    """Build the environment for an account's worker.

    Inherits the current environment, then layers the account's identity and
    posture on top. Every persisted-state path is pinned under the account's own
    directory so concurrent workers are fully isolated. The Trading Format is
    passed through; bot.py's apply_format() seeds the preset defaults while these
    explicit values still win where set.
    """
    env = dict(os.environ)
    env["TRADING_FORMAT"] = account.trading_format
    # Hard safety: a worker may only run live if the site-wide flag is on AND the
    # account opted in. In Phase 1 (paper) DASHBOARD_ALLOW_LIVE is unset, so every
    # worker is forced to paper regardless of the account's stored flag.
    live_allowed = os.environ.get("DASHBOARD_ALLOW_LIVE", "").lower() == "true"
    demo = True if not live_allowed else account.demo_mode
    env["DEMO_MODE"] = "true" if demo else "false"
    env["KALSHI_API_KEY_ID"] = account.kalshi_key_id
    if account.kalshi_pem_path and os.path.exists(account.kalshi_pem_path):
        with open(account.kalshi_pem_path) as f:
            env["KALSHI_PRIVATE_KEY_PEM"] = f.read()
    env["PAPER_BALANCE"] = str(account.paper_balance)

    # Per-account isolation — reuse the bot's existing *_STATE_PATH env vars.
    env["RECOVERY_STATE_PATH"] = _state_path(account, "recovery_state.json")
    env["PROBATION_STATE_PATH"] = _state_path(account, "probation_state.json")
    env["LADDER_STATE_PATH"] = _state_path(account, "ladder_state.json")
    env["BUCKET_STATS_PATH"] = _state_path(account, "bucket_stats.json")
    env["STATUS_SNAPSHOT_PATH"] = _state_path(account, "status.json")

    if account.telegram_bot_token and account.telegram_chat_id:
        env["TELEGRAM_BOT_TOKEN"] = account.telegram_bot_token
        env["TELEGRAM_CHAT_ID"] = account.telegram_chat_id
    return env


class Supervisor:
    """In-process registry of running workers, durable via per-account pidfiles."""

    def __init__(self) -> None:
        self._procs: Dict[str, subprocess.Popen] = {}

    # ── pidfile helpers (survive a dashboard restart) ─────────────────────────
    @staticmethod
    def _pidfile(account: Account) -> str:
        return _state_path(account, "worker.pid")

    @staticmethod
    def _logfile(account: Account) -> str:
        return _state_path(account, "worker.log")

    def _read_pid(self, account: Account) -> Optional[int]:
        path = self._pidfile(account)
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                return int(f.read().strip())
        except (ValueError, OSError):
            return None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def is_running(self, account: Account) -> bool:
        proc = self._procs.get(account.id)
        if proc is not None and proc.poll() is None:
            return True
        pid = self._read_pid(account)
        return bool(pid and self._pid_alive(pid))

    def start(self, account: Account) -> bool:
        if self.is_running(account):
            return False
        if not account.has_credentials():
            raise RuntimeError("Account has no Kalshi credentials configured.")
        account.ensure_dirs()
        env = compose_env(account)
        logf = open(self._logfile(account), "a")
        proc = subprocess.Popen(
            [sys.executable, "bot.py"],
            cwd=REPO_ROOT,
            env=env,
            stdout=logf,
            stderr=subprocess.STDOUT,
        )
        self._procs[account.id] = proc
        with open(self._pidfile(account), "w") as f:
            f.write(str(proc.pid))
        return True

    def stop(self, account: Account) -> bool:
        stopped = False
        proc = self._procs.get(account.id)
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
            stopped = True
        else:
            pid = self._read_pid(account)
            if pid and self._pid_alive(pid):
                try:
                    os.kill(pid, signal.SIGTERM)
                    stopped = True
                except ProcessLookupError:
                    pass
        self._procs.pop(account.id, None)
        try:
            os.remove(self._pidfile(account))
        except OSError:
            pass
        return stopped

    def restart(self, account: Account) -> bool:
        self.stop(account)
        return self.start(account)

    # ── status ────────────────────────────────────────────────────────────────
    def status(self, account: Account) -> dict:
        """Merge process liveness with the worker's status snapshot."""
        running = self.is_running(account)
        snapshot = None
        snap_path = _state_path(account, "status.json")
        if os.path.exists(snap_path):
            try:
                with open(snap_path) as f:
                    snapshot = json.load(f)
            except (OSError, json.JSONDecodeError):
                snapshot = None
        return {"running": running, "snapshot": snapshot}
