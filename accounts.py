"""Sign-in by username and PIN. The server derives user_id from the session cookie; the client
never supplies it (SPEC §4: user_id is server-issued)."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Optional

USERNAME = re.compile(r"^[a-z0-9_.-]{3,32}$")
PIN = re.compile(r"[0-9]{4,12}")  # used with fullmatch: ASCII digits, no trailing newline
SESSION_SECONDS = 30 * 24 * 60 * 60
LOGIN_FAILURES_ALLOWED = 5          # per username, within the window below
LOGIN_WINDOW_SECONDS = 15 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    username   TEXT NOT NULL UNIQUE,
    pin_salt   BLOB NOT NULL,
    pin_hash   BLOB NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL
);
"""


class AccountError(ValueError):
    pass


class TooManyAttempts(AccountError):
    """Too many failed sign-ins for this username; wait for the window to pass."""


def _hash(pin: str, salt: bytes) -> bytes:
    return hashlib.scrypt(pin.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _session_cutoff() -> str:
    # ISO-8601 UTC strings in one format compare in time order
    return (_now() - timedelta(seconds=SESSION_SECONDS)).isoformat()


class Accounts:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=10)
        self._db.executescript(_SCHEMA)
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}  # username -> recent failed sign-in times

    def register(self, username: str, pin: str) -> str:
        username = username.strip().lower()
        if not USERNAME.match(username):
            raise AccountError("Usernames are 3-32 characters: letters, numbers, dot, dash or underscore.")
        if not PIN.fullmatch(pin):
            raise AccountError("PINs are 4-12 digits.")
        salt = secrets.token_bytes(16)
        user_id, pin_hash = str(uuid.uuid4()), _hash(pin, salt)  # slow; done outside the lock
        with self._lock:
            try:
                self._db.execute("INSERT INTO users VALUES (?,?,?,?,?)",
                                 (user_id, username, salt, pin_hash, _now().isoformat()))
            except sqlite3.IntegrityError:
                raise AccountError("That username is taken. Sign in instead, or pick another.") from None
        return user_id

    def login(self, username: str, pin: str) -> Optional[str]:
        """user_id, or None for a wrong username or PIN. Raises TooManyAttempts once a
        username has LOGIN_FAILURES_ALLOWED failures within LOGIN_WINDOW_SECONDS."""
        name, now = username.strip().lower(), monotonic()
        with self._lock:
            self._failures = {n: ts for n, ts in self._failures.items()
                              if now - ts[-1] < LOGIN_WINDOW_SECONDS}
            recent = [t for t in self._failures.get(name, ()) if now - t < LOGIN_WINDOW_SECONDS]
            if len(recent) >= LOGIN_FAILURES_ALLOWED:
                raise TooManyAttempts("Too many sign-in attempts for this username. "
                                      f"Try again in {LOGIN_WINDOW_SECONDS // 60} minutes.")
            # counted as a failure until it succeeds, so parallel guesses can't skip the limit
            self._failures[name] = recent + [now]
            row = self._db.execute("SELECT user_id, pin_salt, pin_hash FROM users WHERE username = ?",
                                   (name,)).fetchone()
        if row is None:
            _hash(pin, b"0" * 16)  # same work either way, so timing doesn't reveal usernames
            return None
        user_id, salt, stored = row
        if not hmac.compare_digest(_hash(pin, salt), stored):
            return None
        with self._lock:
            self._failures.pop(name, None)
        return user_id

    def start_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._db.execute("DELETE FROM sessions WHERE created_at <= ?", (_session_cutoff(),))
            self._db.execute("INSERT INTO sessions VALUES (?,?,?)", (token, user_id, _now().isoformat()))
        return token

    def session_user(self, token: Optional[str]) -> Optional[tuple[str, str]]:
        if not token:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT u.user_id, u.username FROM sessions s JOIN users u USING (user_id) "
                "WHERE s.token = ? AND s.created_at > ?", (token, _session_cutoff())).fetchone()
        return (row[0], row[1]) if row else None

    def end_session(self, token: Optional[str]) -> None:
        if token:
            with self._lock:
                self._db.execute("DELETE FROM sessions WHERE token = ?", (token,))

    def close(self) -> None:
        self._db.close()
