"""Operator Telegram command bot for the dashboard.

Send commands to the same operator bot that posts watchdog alerts and it will
report status, summarize logs, run a rule-based analysis, and (with an explicit
confirmation) adjust a worker's Trading Format or a tunable parameter, or
pause/resume/restart it.

Design:
  * Long-polls Telegram getUpdates in a background thread (no inbound webhook
    needed) — same pattern as the watchdog.
  * Only messages from authorized chat ids (DASHBOARD_TELEGRAM_CHAT_ID,
    comma-separated) are acted on; anything else is ignored.
  * Read commands (/status /logs /analyze /help) run immediately. Every command
    that CHANGES something (/format /set /pause /resume /restart) is staged and
    requires a follow-up `/confirm` before it takes effect.
  * It only ever changes the safe, structured levers — Trading Format and the
    formats.ALLOWED_PARAM_KEYS overrides — never algorithm code, never the paper
    safety (overrides cannot set DEMO_MODE).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, List, Optional, Tuple

import requests

from formats import FORMATS, coerce_param, list_formats
from .accounts import AccountStore
from .supervisor import Supervisor
from .watchdog import evaluate

log = logging.getLogger("dashboard.telegram_bot")

PENDING_TTL = 120  # seconds a staged action waits for /confirm

HELP = (
    "MarkeyMachine operator commands:\n"
    "/status [acct] — worker health + stats\n"
    "/logs [acct] [n] — recent log lines\n"
    "/analyze [acct] — rule-based diagnosis + recommendation\n"
    "/format <name> [acct] — switch Trading Format (needs /confirm)\n"
    "/set <PARAM> <value> [acct] — override a parameter (needs /confirm)\n"
    "/pause [acct] — stop a worker (needs /confirm)\n"
    "/resume [acct] — start a worker (needs /confirm)\n"
    "/restart [acct] — restart a worker (needs /confirm)\n"
    "/confirm — apply the last staged change · /cancel — discard it\n"
    "/formats — list available formats · /help"
)


# ── rule-based analyzer (pure, testable) ──────────────────────────────────────
def build_analysis(health: dict, snapshot: Optional[dict],
                   log_tail: List[str]) -> Tuple[List[str], Optional[str]]:
    """Return (findings, recommended_command). Deterministic — no external AI."""
    findings: List[str] = []
    rec: Optional[str] = None
    state = health.get("state")

    if state == "crashed":
        findings.append("Worker has CRASHED (exited without a clean stop).")
        rec = "/restart"
    elif state == "stalled":
        findings.append(f"Worker STALLED — no status update for "
                        f"{health.get('stale_seconds')}s.")
        rec = "/restart"
    elif state == "stopped":
        findings.append("Worker is stopped.")

    errs = [ln for ln in log_tail
            if any(k in ln for k in ("Error", "Traceback", "Exception", "CRITICAL"))]
    if errs:
        findings.append("Recent errors in log: " + errs[-1][:160])

    if snapshot:
        wins = snapshot.get("wins", 0) or 0
        losses = snapshot.get("losses", 0) or 0
        total = wins + losses
        if snapshot.get("halted"):
            findings.append("Session is HALTED (catastrophic backstop tripped).")
        if total >= 10:
            ci = snapshot.get("wilson_ci") or [0, 0]
            wr = snapshot.get("win_rate", 0)
            findings.append(f"Win rate {wr}% over {total} trades "
                            f"(95% CI {ci[0]}–{ci[1]}%).")
            if ci[1] < 50:
                findings.append("Edge unproven — upper CI bound below breakeven.")
                rec = rec or "/format conservative"
            elif ci[0] > 55 and total >= 20:
                findings.append("Edge looks solid.")
        elif total == 0 and state == "healthy":
            findings.append("Running but no trades yet — gates may be strict or "
                            "the market is quiet.")
        pnl = snapshot.get("session_pnl")
        bal = snapshot.get("balance")
        if isinstance(pnl, (int, float)) and isinstance(bal, (int, float)) and bal:
            dd = pnl / bal
            if dd <= -0.10:
                findings.append(f"Session drawdown {dd*100:.0f}%.")
                rec = rec or "/format conservative"

    if not findings:
        findings.append("No issues detected — worker healthy.")
    return findings, rec


# ── command handler (pure of network; testable) ───────────────────────────────
class CommandHandler:
    def __init__(self, supervisor: Supervisor, store: AccountStore,
                 authorized_chats: set, stale_secs: int = 180) -> None:
        self.supervisor = supervisor
        self.store = store
        self.authorized = {str(c) for c in authorized_chats}
        self.stale_secs = stale_secs
        self._pending: dict = {}   # chat_id -> (description, callable, expires_at)

    # account resolution
    def _resolve_account(self, arg: Optional[str]):
        self.store.load()
        accts = self.store.all()
        if arg:
            a = next((x for x in accts
                      if x.id == arg or x.label.lower() == arg.lower()), None)
            return (a, None) if a else (None, f"No account '{arg}'. See /status.")
        if len(accts) == 1:
            return accts[0], None
        if not accts:
            return None, "No accounts yet."
        return None, "Multiple accounts — specify an id (see /status)."

    def _stage(self, chat_id: str, desc: str, fn: Callable[[], str]) -> str:
        self._pending[chat_id] = (desc, fn, time.time() + PENDING_TTL)
        return f"Staged: {desc}\nReply /confirm to apply or /cancel to discard."

    # main entrypoint
    def handle(self, chat_id, text: str) -> Optional[str]:
        chat_id = str(chat_id)
        if chat_id not in self.authorized:
            return None  # ignore strangers
        text = (text or "").strip()
        if not text.startswith("/"):
            return None
        parts = text.split()
        cmd, args = parts[0].lower().lstrip("/"), parts[1:]

        if cmd in ("help", "start"):
            return HELP
        if cmd == "formats":
            return "Formats: " + ", ".join(f["name"] for f in list_formats())
        if cmd == "confirm":
            return self._do_confirm(chat_id)
        if cmd == "cancel":
            self._pending.pop(chat_id, None)
            return "Discarded."
        if cmd == "status":
            return self._do_status(args[0] if args else None)
        if cmd == "logs":
            return self._do_logs(args)
        if cmd == "analyze":
            return self._do_analyze(args[0] if args else None)
        if cmd == "format":
            return self._stage_format(chat_id, args)
        if cmd == "set":
            return self._stage_set(chat_id, args)
        if cmd in ("pause", "resume", "restart"):
            return self._stage_control(chat_id, cmd, args)
        return f"Unknown command. {HELP}"

    # ── read commands ─────────────────────────────────────────────────────────
    def _do_status(self, arg) -> str:
        self.store.load()
        accts = [a for a in self.store.all() if not arg or a.id == arg or a.label.lower() == arg.lower()]
        if not accts:
            return "No accounts."
        out = []
        for a in accts:
            h = evaluate(a, self.supervisor, self.stale_secs)
            bal = h.get("balance")
            pnl = h.get("session_pnl")
            out.append(f"{a.label} [{a.id}] — {h['state']} · fmt={a.trading_format}"
                       + (f" · ${bal:.2f}" if isinstance(bal, (int, float)) else "")
                       + (f" · PnL ${pnl:+.2f}" if isinstance(pnl, (int, float)) else ""))
        return "\n".join(out)

    def _do_logs(self, args) -> str:
        arg = args[0] if args and not args[0].isdigit() else None
        n = next((int(x) for x in args if x.isdigit()), 30)
        acct, err = self._resolve_account(arg)
        if err:
            return err
        lines = self.supervisor.tail_log(acct, min(60, max(5, n)))
        return f"{acct.label} last {len(lines)} lines:\n" + ("\n".join(lines) or "(empty)")

    def _do_analyze(self, arg) -> str:
        acct, err = self._resolve_account(arg)
        if err:
            return err
        h = evaluate(acct, self.supervisor, self.stale_secs)
        snap = self.supervisor.status(acct).get("snapshot")
        findings, rec = build_analysis(h, snap, self.supervisor.tail_log(acct, 50))
        msg = f"Analysis — {acct.label} [{acct.id}]:\n• " + "\n• ".join(findings)
        if rec:
            msg += f"\n\nRecommended: {rec} {acct.id}"
        return msg

    # ── staged (confirm) commands ─────────────────────────────────────────────
    def _stage_format(self, chat_id, args) -> str:
        if not args:
            return "Usage: /format <name> [acct]"
        name = args[0].lower()
        if name not in FORMATS:
            return "Unknown format. /formats to list."
        acct, err = self._resolve_account(args[1] if len(args) > 1 else None)
        if err:
            return err

        def apply():
            acct.trading_format = name
            self.store.update(acct)
            restarted = self._maybe_restart(acct)
            return f"Format → {name} for {acct.label}." + (" Restarted." if restarted else "")
        return self._stage(chat_id, f"set format={name} on {acct.label}", apply)

    def _stage_set(self, chat_id, args) -> str:
        if len(args) < 2:
            return "Usage: /set <PARAM> <value> [acct]"
        key, value = args[0], args[1]
        try:
            normalized = coerce_param(key, value)
        except ValueError as e:
            return str(e)
        acct, err = self._resolve_account(args[2] if len(args) > 2 else None)
        if err:
            return err

        def apply():
            acct.overrides[key.strip().upper()] = normalized
            self.store.update(acct)
            restarted = self._maybe_restart(acct)
            return (f"{key.strip().upper()}={normalized} on {acct.label}."
                    + (" Restarted." if restarted else ""))
        return self._stage(chat_id, f"set {key.strip().upper()}={normalized} on {acct.label}", apply)

    def _stage_control(self, chat_id, cmd, args) -> str:
        acct, err = self._resolve_account(args[0] if args else None)
        if err:
            return err

        def apply():
            try:
                if cmd == "pause":
                    self.supervisor.stop(acct)
                    return f"Paused {acct.label}."
                if cmd == "resume":
                    self.supervisor.start(acct)
                    return f"Started {acct.label}."
                self.supervisor.restart(acct)
                return f"Restarted {acct.label}."
            except RuntimeError as e:
                return f"Could not {cmd} {acct.label}: {e}"
        return self._stage(chat_id, f"{cmd} {acct.label}", apply)

    def _maybe_restart(self, acct) -> bool:
        if self.supervisor.is_running(acct):
            try:
                self.supervisor.restart(acct)
                return True
            except RuntimeError:
                return False
        return False

    def _do_confirm(self, chat_id) -> str:
        pending = self._pending.pop(chat_id, None)
        if not pending:
            return "Nothing staged."
        desc, fn, expires = pending
        if time.time() > expires:
            return "That request expired — re-issue it."
        try:
            return fn()
        except Exception as e:  # pragma: no cover
            return f"Failed: {e}"


# ── long-poll listener thread ─────────────────────────────────────────────────
class TelegramListener:
    def __init__(self, handler: CommandHandler, token: str) -> None:
        self.handler = handler
        self.token = token
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._offset = 0

    def _api(self, method: str, params: dict) -> Optional[dict]:
        try:
            r = requests.get(f"https://api.telegram.org/bot{self.token}/{method}",
                             params=params, timeout=60)
            return r.json() if r.status_code == 200 else None
        except Exception as e:  # pragma: no cover - network
            log.debug("telegram %s error: %s", method, e)
            return None

    def _send(self, chat_id, text: str) -> None:
        self._api("sendMessage", {"chat_id": chat_id, "text": text})

    def poll_once(self) -> None:
        data = self._api("getUpdates", {"offset": self._offset, "timeout": 50})
        if not data or not data.get("ok"):
            return
        for upd in data.get("result", []):
            self._offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = (msg.get("chat") or {}).get("id")
            text = msg.get("text", "")
            if chat is None or not text:
                continue
            try:
                reply = self.handler.handle(chat, text)
            except Exception as e:  # pragma: no cover
                reply = f"Error: {e}"
            if reply:
                self._send(chat, reply)

    def _run(self) -> None:
        log.info("Telegram command listener started.")
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as e:  # pragma: no cover
                log.debug("listener cycle error: %s", e)
                time.sleep(5)

    def start(self) -> "TelegramListener":
        if self._thread and self._thread.is_alive():
            return self
        self._thread = threading.Thread(target=self._run, name="tg-commands", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()


_listener: Optional[TelegramListener] = None


def start_command_bot(supervisor: Supervisor, store: AccountStore) -> Optional[TelegramListener]:
    """Idempotent singleton. Enabled only when an operator bot token + authorized
    chat id are configured and DASHBOARD_TELEGRAM_COMMANDS != false."""
    global _listener
    if os.environ.get("DASHBOARD_TELEGRAM_COMMANDS", "true").lower() == "false":
        return None
    token = os.environ.get("DASHBOARD_TELEGRAM_BOT_TOKEN", "").strip()
    chats = {c.strip() for c in os.environ.get("DASHBOARD_TELEGRAM_CHAT_ID", "").split(",") if c.strip()}
    if not token or not chats:
        return None
    if _listener is None:
        handler = CommandHandler(supervisor, store, chats,
                                 int(os.environ.get("WATCHDOG_STALE_SECS", "180") or "180"))
        _listener = TelegramListener(handler, token).start()
    return _listener
