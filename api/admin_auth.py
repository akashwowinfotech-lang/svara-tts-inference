"""Password-based dashboard login with short-lived, revocable sessions."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from api.access import AccessDenied, utc_now


COOKIE_NAME = "speech_admin_session"
SESSION_HOURS = 8


def password_hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1).hex()


def create_credentials(path: Path, username: str, password: str) -> None:
    if not 3 <= len(username) <= 80 or not username.isascii() or not username.replace("_", "").replace("-", "").isalnum():
        raise ValueError("Username must be 3–80 ASCII letters, numbers, underscores, or hyphens")
    if not 12 <= len(password) <= 1024:
        raise ValueError("Password must be 12–1024 characters")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    salt = secrets.token_bytes(32)
    payload = {"username": username, "salt": salt.hex(), "hash": password_hash(password, salt)}
    tmp = path.with_name(path.name + ".tmp-" + secrets.token_hex(8))
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle)
            handle.write("\n")
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        tmp.unlink(missing_ok=True)


class AdminAuth:
    def __init__(self, db_path: str, credentials_path: str):
        self.db_path = db_path
        path = Path(credentials_path)
        if not path.is_file():
            raise RuntimeError("Dashboard credentials are missing. Run set-admin-login.py first.")
        data = json.loads(path.read_text())
        self.username = data["username"]
        self.salt = bytes.fromhex(data["salt"])
        self.expected_hash = data["hash"]
        if len(self.salt) != 32 or len(self.expected_hash) != 128:
            raise ValueError("Invalid dashboard credential file")
        self.attempts = defaultdict(deque)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS admin_sessions (
                token_hash TEXT PRIMARY KEY,
                csrf_token TEXT NOT NULL,
                client_ip TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS admin_sessions_expiry ON admin_sessions(expires_at)")

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def login(self, username: str, password: str, address: str) -> tuple[str, str]:
        now = time.monotonic()
        attempts = self.attempts[address]
        while attempts and attempts[0] < now - 900:
            attempts.popleft()
        if len(attempts) >= 5:
            raise AccessDenied("Too many login attempts. Try again in 15 minutes.", 429)
        expected_user = hmac.compare_digest(username.encode("utf-8"), self.username.encode("utf-8"))
        expected_password = hmac.compare_digest(password_hash(password, self.salt), self.expected_hash)
        if not (expected_user and expected_password):
            attempts.append(now)
            raise AccessDenied("Invalid username or password", 401)
        attempts.clear()
        token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)).isoformat(timespec="seconds")
        with self._connect() as db:
            db.execute("DELETE FROM admin_sessions WHERE expires_at <= ?", (utc_now(),))
            db.execute("INSERT INTO admin_sessions(token_hash,csrf_token,client_ip,expires_at) VALUES(?,?,?,?)",
                       (hashlib.sha256(token.encode()).hexdigest(), csrf_token, address, expires))
        return token, csrf_token

    def session(self, token: str | None, address: str) -> str:
        if not token or len(token) > 200:
            raise AccessDenied("Login required", 401)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._connect() as db:
            row = db.execute("SELECT csrf_token,client_ip,expires_at FROM admin_sessions WHERE token_hash=?", (digest,)).fetchone()
        if row is None or not hmac.compare_digest(row["client_ip"], address) or row["expires_at"] <= utc_now():
            raise AccessDenied("Session expired. Log in again.", 401)
        return row["csrf_token"]

    def logout(self, token: str | None) -> None:
        if token:
            with self._connect() as db:
                db.execute("DELETE FROM admin_sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
