"""Worker health classification + a background watchdog.

`evaluate()` is the single source of truth for a worker's health — used by both
the `/health` endpoint and the watchdog thread so they always agree. The
`Watchdog` polls every account, alerts the operator on Telegram when a worker
changes state (crash / stall / recovery), and can optionally auto-restart a
crashed worker within a rate cap.

States:
    healthy  — running and its status snapshot is fresh
    stalled  — running but the snapshot has not updated within STALE seconds
    crashed  — exited without a clean stop (pidfile present, process gone)
    stopped  — not running and not crashed (intentionally stopped / never started)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable, Optional

import telegram_utils as tg
from .accounts import AccountStore
from .supervisor import Supervisor

log = logging.getLogger("dashboard.watchdog")


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def evaluate(account, supervisor: Supervisor, stale_secs: int,
             now: Optional[datetime] = None) -> dict:
    """Classify one account's worker. Pure w.r.t. the supervisor/filesystem —
    no side effects. Returns the dict the /health endpoint serializes."""
    now = now or datetime.now(timezone.utc)
    crashed = supervisor.crashed(account)
    running = supervisor.is_running(account)
    st = supervisor.status(account)
    snap = st.get("snapshot") or {}

    updated = _parse_iso(snap.get("updated_at", ""))
    stale_seconds = int((now - updated).total_seconds()) if updated else None

    if crashed:
        state = "crashed"
    elif running:
        if stale_seconds is not None and stale_seconds > stale_secs:
            state = "stalled"
        else:
            state = "healthy"
    else:
        state = "stopped"

    return {
        "id": account.id,
        "label": account.label,
        "running": running,
        "state": state,
        "demo_mode": snap.get("demo_mode", account.demo_mode),
        "active_mode": snap.get("active_mode"),
        "balance": snap.get("balance"),
        "session_pnl": snap.get("session_pnl"),
        "last_signal": snap.get("last_signal"),
        "updated_at": snap.get("updated_at"),
        "stale_seconds": stale_seconds,
    }


# ── watchdog thread ───────────────────────────────────────────────────────────
class Watchdog:
    """Polls every account and alerts the operator on state transitions."""

    ALERTING = {"crashed", "stalled"}  # states worth a notification

    def __init__(self, supervisor: Supervisor, store: AccountStore,
                 notify: Optional[Callable[[str], None]] = None) -> None:
        self.supervisor = supervisor
        self.store = store
        self.interval = _env_int("WATCHDOG_INTERVAL", 60)
        self.stale_secs = _env_int("WATCHDOG_STALE_SECS", 180)
        self.autorestart = os.environ.get("WATCHDOG_AUTORESTART", "true").lower() == "true"
        self.max_restarts = _env_int("WATCHDOG_MAX_RESTARTS", 3)  # per rolling hour
        self._notify = notify or self._telegram_notify
        self._last_state: dict[str, str] = {}
        self._restarts: dict[str, list] = defaultdict(list)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── alerting ──────────────────────────────────────────────────────────────
    @staticmethod
    def _telegram_notify(text: str) -> None:
        tg.notify(os.environ.get("DASHBOARD_TELEGRAM_BOT_TOKEN", ""),
                  os.environ.get("DASHBOARD_TELEGRAM_CHAT_ID", ""), text)

    def _can_restart(self, account_id: str, now: float) -> bool:
        recent = [t for t in self._restarts[account_id] if now - t < 3600]
        self._restarts[account_id] = recent
        return len(recent) < self.max_restarts

    # ── one pass (also unit-testable) ─────────────────────────────────────────
    def check_once(self) -> None:
        self.store.load()  # pick up new signups/accounts
        now = time.time()
        for account in self.store.all():
            health = evaluate(account, self.supervisor, self.stale_secs)
            state = health["state"]
            prev = self._last_state.get(account.id)
            if state == prev:
                continue
            self._last_state[account.id] = state

            if state == "crashed":
                if self.autorestart and account.has_credentials() and self._can_restart(account.id, now):
                    self._restarts[account.id].append(now)
                    try:
                        self.supervisor.restart(account)
                        self._notify(f"♻️ {account.label} ({account.id}) crashed — auto-restarted.")
                        self._last_state[account.id] = "healthy"
                    except Exception as e:  # pragma: no cover
                        self._notify(f"🛑 {account.label} ({account.id}) crashed; auto-restart FAILED: {e}")
                else:
                    self._notify(f"🛑 {account.label} ({account.id}) crashed and is down.")
            elif state == "stalled":
                age = health["stale_seconds"]
                self._notify(f"⚠️ {account.label} ({account.id}) stalled — no update for {age}s.")
            elif state == "healthy" and prev in self.ALERTING:
                self._notify(f"✅ {account.label} ({account.id}) recovered.")

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def _run(self) -> None:
        log.info("Watchdog started (interval=%ds, stale=%ds, autorestart=%s).",
                 self.interval, self.stale_secs, self.autorestart)
        while not self._stop.wait(self.interval):
            try:
                self.check_once()
            except Exception as e:  # pragma: no cover - never let the loop die
                log.warning("Watchdog cycle error: %s", e)

    def start(self) -> "Watchdog":
        if self._thread and self._thread.is_alive():
            return self
        self._thread = threading.Thread(target=self._run, name="watchdog", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()


_watchdog: Optional[Watchdog] = None


def start_watchdog(supervisor: Supervisor, store: AccountStore) -> Optional[Watchdog]:
    """Idempotent singleton starter. Disabled with DASHBOARD_WATCHDOG=false."""
    global _watchdog
    if os.environ.get("DASHBOARD_WATCHDOG", "true").lower() != "true":
        return None
    if _watchdog is None:
        _watchdog = Watchdog(supervisor, store).start()
    return _watchdog
