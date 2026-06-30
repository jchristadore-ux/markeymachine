"""User accounts + authentication for the multi-tenant dashboard.

Phase 1 (paper) turns the single-admin console into a self-serve site: each
customer signs up with an email + password and is fully isolated from every
other customer. A User owns one or more trading Accounts (see accounts.py); all
data access is scoped to the logged-in user.

Storage is a JSON file (one row per user) guarded by a process lock — adequate
for a single-worker gunicorn beta. Passwords are stored only as salted hashes
(werkzeug). Migrating to a real database is a Phase 2 hardening step.

SECURITY NOTE: customer Kalshi private keys are still written to per-account
files on disk (see accounts.py). That is acceptable for a small paper beta but
MUST be replaced with encryption-at-rest / a secrets manager before real keys
are held at scale or any account trades live (Phase 2).
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from werkzeug.security import check_password_hash, generate_password_hash

from .accounts import data_dir

_LOCK = threading.RLock()


def users_path() -> str:
    return os.path.join(data_dir(), "users.json")


def _admin_emails() -> set:
    """Comma-separated ADMIN_EMAILS get is_admin on signup/login."""
    raw = os.environ.get("ADMIN_EMAILS", os.environ.get("ADMIN_EMAIL", ""))
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


@dataclass
class User:
    email: str
    password_hash: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    is_admin: bool = False
    created_at: str = ""

    def check_password(self, password: str) -> bool:
        return bool(self.password_hash) and check_password_hash(self.password_hash, password)

    def public_dict(self) -> dict:
        return {"id": self.id, "email": self.email, "is_admin": self.is_admin}


class UserStore:
    """JSON-backed user table. Whole-file rewrite under a process lock."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or users_path()
        self._users: List[User] = []
        self.load()

    def load(self) -> None:
        with _LOCK:
            if os.path.exists(self.path):
                with open(self.path) as f:
                    raw = json.load(f)
                self._users = [User(**u) for u in raw.get("users", [])]
            else:
                self._users = []

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"users": [asdict(u) for u in self._users]}, f, indent=2)
        os.replace(tmp, self.path)

    # ── lookups ───────────────────────────────────────────────────────────────
    # Reads reload from disk first so a user created by a different gunicorn
    # worker/instance (or after this object was constructed) is always found —
    # otherwise a fresh signup is invisible to the login request and the user
    # sees "Incorrect email or password".
    def all(self) -> List[User]:
        with _LOCK:
            self.load()
            return list(self._users)

    def get(self, user_id: str) -> Optional[User]:
        with _LOCK:
            self.load()
            return next((u for u in self._users if u.id == user_id), None)

    def get_by_email(self, email: str) -> Optional[User]:
        e = (email or "").strip().lower()
        with _LOCK:
            self.load()
            return next((u for u in self._users if u.email == e), None)

    # ── mutations ─────────────────────────────────────────────────────────────
    def create(self, email: str, password: str) -> User:
        """Create a user. Raises ValueError on a bad/blank email, a too-short
        password, or a duplicate email (case-insensitive)."""
        from datetime import datetime, timezone
        e = (email or "").strip().lower()
        if not e or "@" not in e or "." not in e.split("@")[-1]:
            raise ValueError("Enter a valid email address.")
        if len(password or "") < 8:
            raise ValueError("Password must be at least 8 characters.")
        with _LOCK:
            self.load()  # re-read so concurrent signups don't clobber each other
            if self.get_by_email(e):
                raise ValueError("An account with that email already exists.")
            user = User(
                email=e,
                # pbkdf2 is universally supported; werkzeug's default (scrypt)
                # fails on some containers (hashlib.scrypt maxmem limits).
                password_hash=generate_password_hash(password, method="pbkdf2:sha256"),
                is_admin=(e in _admin_emails()),
                created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            self._users.append(user)
            self._save()
            return user

    def verify(self, email: str, password: str) -> Optional[User]:
        user = self.get_by_email(email)
        if user and user.check_password(password):
            # Promote to admin if the email was added to ADMIN_EMAILS after signup.
            if not user.is_admin and user.email in _admin_emails():
                with _LOCK:
                    user.is_admin = True
                    self._save()
            return user
        return None
