"""Account store for the dashboard.

An *account* is the only thing unique to each client: their Kalshi credentials
plus the Trading Format and risk posture chosen for them. Everything else (the
strategy, the code) is shared. The store is a JSON **list** from day one so the
single-account dashboard is already multi-tenant-shaped — adding clients is just
appending entries, no schema change.

Secrets handling: the Kalshi private key (PEM) is written to a file under the
account's own directory and referenced by path; it is never stored inline in
accounts.json and never echoed back to the browser.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from typing import List, Optional

_LOCK = threading.RLock()

# Root for all dashboard-managed state. Resolved dynamically (not captured at
# import) so DASHBOARD_DATA_DIR can be set by the host — or per-test — at any time.
def data_dir() -> str:
    return os.environ.get("DASHBOARD_DATA_DIR", "dashboard_data")


def accounts_path() -> str:
    return os.path.join(data_dir(), "accounts.json")


@dataclass
class Account:
    """One managed Kalshi account + the posture chosen for it."""

    label: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    # Which user owns this account. Empty for legacy single-tenant rows.
    owner_user_id: str = ""
    kalshi_key_id: str = ""
    # Path to the PEM file on disk (content lives in the account dir, not here).
    kalshi_pem_path: str = ""
    trading_format: str = "balanced"
    # Per-account trading-parameter overrides (set via the Telegram /set command).
    # Keys are restricted to formats.ALLOWED_PARAM_KEYS; applied in compose_env.
    overrides: dict = field(default_factory=dict)
    demo_mode: bool = True  # default PAPER — going live is an explicit opt-in
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    paper_balance: float = 1000.0

    @property
    def state_dir(self) -> str:
        return os.path.join(data_dir(), "accounts", self.id)

    def ensure_dirs(self) -> None:
        os.makedirs(self.state_dir, exist_ok=True)

    def has_credentials(self) -> bool:
        return bool(self.kalshi_key_id and self.kalshi_pem_path
                    and os.path.exists(self.kalshi_pem_path))

    def public_dict(self) -> dict:
        """Serializable view for templates — never includes secret material."""
        d = asdict(self)
        d["state_dir"] = self.state_dir
        d["has_credentials"] = self.has_credentials()
        d["has_telegram"] = bool(self.telegram_bot_token and self.telegram_chat_id)
        # Surface only that a key exists, not the key itself.
        d["kalshi_key_id_masked"] = (
            (self.kalshi_key_id[:4] + "…" + self.kalshi_key_id[-4:])
            if len(self.kalshi_key_id) >= 8 else ("set" if self.kalshi_key_id else "")
        )
        d.pop("kalshi_pem_path", None)
        d.pop("telegram_bot_token", None)
        return d


class AccountStore:
    """JSON-backed list of accounts. Small enough that we rewrite the whole file
    on every change — simple and safe for the single-/few-account scale."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or accounts_path()
        self._accounts: List[Account] = []
        self.load()

    def load(self) -> None:
        if os.path.exists(self.path):
            with open(self.path) as f:
                raw = json.load(f)
            self._accounts = [Account(**a) for a in raw.get("accounts", [])]
        else:
            self._accounts = []

    def save(self) -> None:
        with _LOCK:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"accounts": [asdict(a) for a in self._accounts]}, f, indent=2)
            os.replace(tmp, self.path)

    def all(self) -> List[Account]:
        return list(self._accounts)

    def get(self, account_id: str) -> Optional[Account]:
        return next((a for a in self._accounts if a.id == account_id), None)

    def for_user(self, user_id: str) -> List[Account]:
        """Every account owned by this user — the only ones they may see."""
        return [a for a in self._accounts if a.owner_user_id == user_id]

    def add(self, account: Account) -> Account:
        with _LOCK:
            self.load()  # re-read so concurrent writers don't clobber each other
            account.ensure_dirs()
            self._accounts.append(account)
            self.save()
        return account

    def update(self, account: Account) -> None:
        with _LOCK:
            self.load()
            for i, a in enumerate(self._accounts):
                if a.id == account.id:
                    self._accounts[i] = account
                    self.save()
                    return
            raise KeyError(account.id)

    def ensure_for_user(self, user_id: str, label: str = "My Kalshi Account") -> Account:
        """Return this user's account, creating one (paper, no creds) on first
        login so every customer always has exactly one to configure."""
        existing = self.for_user(user_id)
        if existing:
            return existing[0]
        return self.add(Account(label=label, owner_user_id=user_id))
