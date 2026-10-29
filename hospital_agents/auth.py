"""Authentication and authorization.

The role is a property of an *account stored on the server*, never of the request. Passwords are
stored as salted PBKDF2-HMAC-SHA256 hashes; sessions are random bearer tokens held in memory
(a restart signs everyone out); repeated failures lock the account for a short time.

Roles, least to most privileged:
    viewer              read dashboards
    analyst             + run agents and simulations
    operations_manager  + approve / reject / roll back decisions, change policies
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from .store import atomic_write

ROLES = ("viewer", "analyst", "operations_manager")
ROLE_LABELS = {"viewer": "Viewer", "analyst": "Analyst", "operations_manager": "Operations Manager"}
PERMISSIONS = {
    "read": ROLES,
    "run": ("analyst", "operations_manager"),
    "decide": ("operations_manager",),      # approve / reject / roll back
    "policy": ("operations_manager",),      # change structured policies
}
ITERATIONS = 200_000
SESSION_TTL_S = 8 * 3600
LOCK_AFTER, LOCK_FOR_S = 5, 60


class AuthError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


@dataclass(frozen=True)
class Principal:
    username: str
    role: str

    @property
    def label(self) -> str:
        return ROLE_LABELS[self.role]

    def can(self, permission: str) -> bool:
        return self.role in PERMISSIONS[permission]


def _hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS).hex()


class Auth:
    def __init__(self, users_file: Path, clock=time.time) -> None:
        self.file, self.clock = Path(users_file), clock
        self.sessions: dict[str, tuple[Principal, float]] = {}
        self.failures: dict[str, tuple[int, float]] = {}
        self.users: dict[str, dict] = json.loads(self.file.read_text()) if self.file.exists() else {}
        self.initial_credentials: dict[str, str] | None = None
        if not self.users:
            self.initial_credentials = self._bootstrap()

    # ── accounts ──
    def _save(self) -> None:
        atomic_write(self.file, json.dumps(self.users, indent=1), private=True)

    def create_user(self, username: str, password: str, role: str) -> None:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        if not username.strip() or len(password) < 8:
            raise ValueError("username is required and the password needs at least 8 characters")
        salt = secrets.token_bytes(16)
        self.users[username] = {"role": role, "salt": salt.hex(), "hash": _hash(password, salt)}
        self._save()

    def _bootstrap(self) -> dict[str, str]:
        """First run: one account per role. Passwords come from HDT_<ROLE>_PASSWORD or are random."""
        out = {}
        for name, role in (("manager", "operations_manager"), ("analyst", "analyst"), ("viewer", "viewer")):
            password = os.environ.get(f"HDT_{name.upper()}_PASSWORD") or secrets.token_urlsafe(9)
            self.create_user(name, password, role)
            out[name] = password
        return out

    # ── sessions ──
    def login(self, username: str, password: str) -> tuple[str, Principal]:
        now = self.clock()
        count, until = self.failures.get(username, (0, 0.0))
        if until > now:
            raise AuthError(429, "too many failed attempts; try again in a minute")
        user = self.users.get(username)
        salt = bytes.fromhex(user["salt"]) if user else b"\0" * 16          # equal work either way
        ok = hmac.compare_digest(_hash(password, salt), user["hash"] if user else "0" * 64)
        if not (user and ok):
            count += 1
            self.failures[username] = (count, now + LOCK_FOR_S if count >= LOCK_AFTER else 0.0)
            raise AuthError(401, "wrong username or password")
        self.failures.pop(username, None)
        token = secrets.token_urlsafe(32)
        principal = Principal(username, user["role"])
        self.sessions[token] = (principal, now + SESSION_TTL_S)
        return token, principal

    def principal(self, token: str | None) -> Principal | None:
        entry = self.sessions.get(token or "")
        if entry is None:
            return None
        if entry[1] < self.clock():
            self.sessions.pop(token, None)
            return None
        return entry[0]

    def logout(self, token: str | None) -> None:
        self.sessions.pop(token or "", None)

    @staticmethod
    def require(principal: Principal | None, permission: str) -> Principal:
        if principal is None:
            raise AuthError(401, "sign in required")
        if not principal.can(permission):
            raise AuthError(403, f"your role ({principal.label}) is not allowed to do this")
        return principal
