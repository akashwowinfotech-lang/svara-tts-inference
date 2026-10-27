"""Local API-key storage, IP policy, concurrency limits, and request accounting."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import secrets
import sqlite3
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_networks(values: list[str]) -> list[str]:
    if len(values) > 100:
        raise ValueError("Use at most 100 IP addresses or CIDR ranges")
    networks = []
    for value in values:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError("Each IP address or CIDR range must be at most 64 characters")
        value = value.strip()
        if value:
            networks.append(str(ipaddress.ip_network(value, strict=False)))
    return sorted(set(networks))


def ip_allowed(address: str, networks: list[str]) -> bool:
    if not networks:
        return True
    try:
        ip = ipaddress.ip_address(address)
        return any(ip in ipaddress.ip_network(item) for item in networks)
    except ValueError:
        return False


def client_ip(connection) -> str:
    """Use a proxy-supplied IP only when the immediate peer is trusted."""
    peer = connection.client.host if connection.client else "unknown"
    trusted = normalize_networks(os.getenv("TRUSTED_PROXY_IPS", "").split(","))
    if ip_allowed(peer, trusted) and trusted:
        forwarded = connection.headers.get("x-real-ip", "").strip()
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return peer


class AccessDenied(Exception):
    def __init__(self, message: str, status_code: int = 403):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True)
class ClientKey:
    id: str
    label: str
    allowed_ips: list[str]
    max_concurrent: int
    requests_per_minute: int
    priority: int = 2  # 1 = High, 2 = Normal, 3 = Low


class AccessStore:
    def __init__(self, db_path: str, admin_secret: str):
        if len(admin_secret) < 32:
            raise ValueError("Admin secret must have at least 32 characters")
        self.db_path = Path(db_path)
        self.admin_secret = admin_secret.strip()
        self._next_prune = 0.0
        self.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def _initialize(self):
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS client_keys (
                    id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    secret_hash TEXT NOT NULL UNIQUE,
                    prefix TEXT NOT NULL,
                    allowed_ips TEXT NOT NULL,
                    max_concurrent INTEGER NOT NULL,
                    requests_per_minute INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS request_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    key_id TEXT,
                    key_label TEXT,
                    client_ip TEXT NOT NULL,
                    transport TEXT NOT NULL,
                    route TEXT NOT NULL,
                    voice TEXT,
                    status TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    first_audio_ms INTEGER,
                    audio_bytes INTEGER NOT NULL,
                    error_code TEXT
                );
                CREATE INDEX IF NOT EXISTS request_logs_created ON request_logs(created_at);
                CREATE INDEX IF NOT EXISTS request_logs_key ON request_logs(key_id, created_at);
                CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(request_logs)")}
            if "audio_seconds" not in columns:
                db.execute("ALTER TABLE request_logs ADD COLUMN audio_seconds REAL")
            key_columns = {row["name"] for row in db.execute("PRAGMA table_info(client_keys)")}
            if "priority" not in key_columns:
                db.execute("ALTER TABLE client_keys ADD COLUMN priority INTEGER NOT NULL DEFAULT 2 CHECK(priority BETWEEN 1 AND 3)")
            db.execute("INSERT OR IGNORE INTO settings(name,value) VALUES('allowed_ips','[]')")
        os.chmod(self.db_path, 0o600)

    def check_admin(self, raw_secret: str, address: str) -> None:
        allowed = normalize_networks(os.getenv("ADMIN_ALLOWED_IPS", "127.0.0.1/32,::1/128").split(","))
        if not ip_allowed(address, allowed):
            raise AccessDenied("Admin access denied", 403)
        if not secrets.compare_digest(raw_secret, self.admin_secret):
            raise AccessDenied("Invalid admin key", 401)

    def create_key(self, label: str, allowed_ips: list[str], expires_days: int,
                   max_concurrent: int, requests_per_minute: int, priority: int = 2) -> dict:
        label = label.strip()
        if not 1 <= len(label) <= 80:
            raise ValueError("Label must contain 1–80 characters")
        if not 1 <= expires_days <= 3650:
            raise ValueError("Expiry must be 1–3650 days")
        if not 1 <= max_concurrent <= 16 or not 1 <= requests_per_minute <= 600:
            raise ValueError("Invalid request limits")
        if type(priority) is not int or priority not in (1, 2, 3):
            raise ValueError("Choose High, Normal or Low priority")
        networks = normalize_networks(allowed_ips)
        raw = "spk_" + secrets.token_urlsafe(32)
        key_id = str(uuid.uuid4())
        expires = (datetime.now(timezone.utc) + timedelta(days=expires_days)).isoformat(timespec="seconds")
        with self._connect() as db:
            db.execute("""INSERT INTO client_keys
                (id,label,secret_hash,prefix,allowed_ips,max_concurrent,requests_per_minute,created_at,expires_at,priority)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (key_id, label, hashlib.sha256(raw.encode()).hexdigest(), raw[:10],
                 json.dumps(networks), max_concurrent, requests_per_minute, utc_now(), expires, priority))
        return {"id": key_id, "key": raw, "label": label, "expires_at": expires, "priority": priority}

    def list_keys(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("""SELECT id,label,prefix,allowed_ips,max_concurrent,
                requests_per_minute,created_at,expires_at,revoked_at,priority FROM client_keys
                ORDER BY created_at DESC""").fetchall()
        return [{**dict(row), "allowed_ips": json.loads(row["allowed_ips"])} for row in rows]

    def update_key(self, key_id: str, allowed_ips: list[str], max_concurrent: int,
                   requests_per_minute: int, priority: int | None = None) -> bool:
        if not 1 <= max_concurrent <= 16 or not 1 <= requests_per_minute <= 600:
            raise ValueError("Invalid request limits")
        if priority is not None and (type(priority) is not int or priority not in (1, 2, 3)):
            raise ValueError("Choose High, Normal or Low priority")
        networks = normalize_networks(allowed_ips)
        with self._connect() as db:
            cursor = db.execute("""UPDATE client_keys SET allowed_ips=?,max_concurrent=?,
                requests_per_minute=?,priority=COALESCE(?,priority) WHERE id=? AND revoked_at IS NULL""",
                (json.dumps(networks), max_concurrent, requests_per_minute, priority, key_id))
        return cursor.rowcount > 0

    def revoke_key(self, key_id: str) -> bool:
        with self._connect() as db:
            cursor = db.execute("UPDATE client_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                                (utc_now(), key_id))
        return cursor.rowcount > 0

    def delete_revoked_key(self, key_id: str) -> bool:
        """Remove a revoked credential while retaining its request history."""
        with self._connect() as db:
            cursor = db.execute("DELETE FROM client_keys WHERE id=? AND revoked_at IS NOT NULL", (key_id,))
        return cursor.rowcount > 0

    def allowed_ips(self) -> list[str]:
        with self._connect() as db:
            value = db.execute("SELECT value FROM settings WHERE name='allowed_ips'").fetchone()[0]
        return json.loads(value)

    def set_allowed_ips(self, values: list[str]) -> list[str]:
        networks = normalize_networks(values)
        with self._connect() as db:
            db.execute("UPDATE settings SET value=? WHERE name='allowed_ips'", (json.dumps(networks),))
        return networks

    def authenticate(self, raw_key: str, address: str) -> ClientKey:
        if not isinstance(raw_key, str) or not raw_key or len(raw_key) > 200:
            raise AccessDenied("API key required", 401)
        digest = hashlib.sha256(raw_key.encode()).hexdigest()
        with self._connect() as db:
            row = db.execute("SELECT * FROM client_keys WHERE secret_hash=?", (digest,)).fetchone()
        return self._authenticated_row(row, address)

    def refresh_client(self, key: ClientKey, address: str) -> ClientKey:
        """Recheck revocation, expiry and IP rules after a request has queued."""
        with self._connect() as db:
            row = db.execute("SELECT * FROM client_keys WHERE id=?", (key.id,)).fetchone()
        return self._authenticated_row(row, address)

    def _authenticated_row(self, row, address: str) -> ClientKey:
        if row is None or row["revoked_at"]:
            raise AccessDenied("Invalid API key", 401)
        if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
            raise AccessDenied("API key expired", 401)
        if not ip_allowed(address, self.allowed_ips()) or not ip_allowed(address, json.loads(row["allowed_ips"])):
            raise AccessDenied("IP address not allowed", 403)
        return ClientKey(row["id"], row["label"], json.loads(row["allowed_ips"]),
                         row["max_concurrent"], row["requests_per_minute"], row["priority"])

    def log_request(self, *, key: ClientKey | None, address: str, transport: str,
                    route: str, voice: str | None, status: str, duration_ms: int,
                    first_audio_ms: int | None = None, audio_bytes: int = 0,
                    error_code: str | None = None, audio_seconds: float | None = None):
        with self._connect() as db:
            db.execute("""INSERT INTO request_logs
                (created_at,key_id,key_label,client_ip,transport,route,voice,status,
                 duration_ms,first_audio_ms,audio_bytes,error_code,audio_seconds)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (utc_now(), key.id if key else None, key.label if key else None,
                 address, transport, route, voice, status, max(0, duration_ms),
                 first_audio_ms, audio_bytes, error_code,
                 max(0.0, float(audio_seconds)) if audio_seconds is not None else None))
            if time.time() >= self._next_prune:
                retention = max(1, int(os.getenv("ACCESS_LOG_RETENTION_DAYS", "30")))
                cutoff = (datetime.now(timezone.utc) - timedelta(days=retention)).isoformat(timespec="seconds")
                db.execute("DELETE FROM request_logs WHERE created_at < ?", (cutoff,))
                self._next_prune = time.time() + 3600

    def report(self, hours: int = 24, *, status: str | None = None,
               transport: str | None = None, route: str | None = None,
               key_id: str | None = None, ip: str | None = None,
               voice: str | None = None, offset: int = 0,
               start_at: datetime | None = None, end_at: datetime | None = None) -> dict:
        hours = max(1, min(hours, 720))
        now = datetime.now(timezone.utc)
        if (start_at is None) != (end_at is None):
            raise ValueError("Select both start and end dates")
        if start_at is not None:
            if start_at.tzinfo is None or end_at.tzinfo is None:
                raise ValueError("Date range must include a timezone")
            start_at = start_at.astimezone(timezone.utc)
            end_at = end_at.astimezone(timezone.utc)
            if not start_at < end_at or end_at - start_at > timedelta(days=366):
                raise ValueError("Choose a date range of at most 366 days")
            clauses = ["created_at >= ?", "created_at < ?"]
            params: list[str] = [start_at.isoformat(timespec="seconds"), end_at.isoformat(timespec="seconds")]
        else:
            start_at = now - timedelta(hours=hours)
            end_at = now
            clauses = ["created_at >= ?"]
            params = [start_at.isoformat(timespec="seconds")]
        for column, value in (("status", status), ("transport", transport), ("route", route),
                              ("key_id", key_id), ("client_ip", ip), ("voice", voice)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses)
        with self._connect() as db:
            counts = db.execute("""SELECT COUNT(*) AS total_requests,
                SUM(CASE WHEN route='/v1/audio/speech' THEN 1 ELSE 0 END) AS speech_requests,
                SUM(CASE WHEN route='/v1/audio/speech' AND status='ok' THEN 1 ELSE 0 END) AS successful_speech,
                SUM(CASE WHEN status='denied' THEN 1 ELSE 0 END) AS denied_requests,
                SUM(CASE WHEN route='/v1/audio/speech' THEN audio_bytes ELSE 0 END) AS audio_bytes
                FROM request_logs""" + where, params).fetchone()
            recent = [dict(row) for row in db.execute(
                "SELECT * FROM request_logs" + where + " ORDER BY id DESC LIMIT 100 OFFSET ?", [*params, offset])]
            usage = db.execute("""SELECT COALESCE(SUM(audio_seconds),0) AS audio_seconds,
                COUNT(audio_seconds) AS metered_requests FROM request_logs""" + where, params).fetchone()
            breakdown = [dict(row) for row in db.execute("""SELECT
                COALESCE(key_label,'Unknown') AS client, route,
                COUNT(*) AS requests, SUM(audio_seconds) AS audio_seconds
                FROM request_logs""" + where + " AND audio_seconds IS NOT NULL" +
                " GROUP BY key_id,key_label,route ORDER BY audio_seconds DESC LIMIT 20", params)]
            bucket = "substr(created_at,1,13)" if end_at - start_at <= timedelta(hours=24) else "substr(created_at,1,10)"
            trend = [dict(row) for row in db.execute("SELECT " + bucket + " AS period, " +
                "COUNT(*) AS requests, SUM(audio_seconds) AS audio_seconds FROM request_logs" + where +
                " AND audio_seconds IS NOT NULL GROUP BY period ORDER BY period", params)]
            latency = [dict(row) for row in db.execute("""SELECT first_audio_ms,duration_ms,status
                FROM request_logs""" + where + " AND route='/v1/audio/speech' ORDER BY id DESC LIMIT 10000", params)]
        first = sorted(row["first_audio_ms"] for row in latency if row["first_audio_ms"] is not None)
        complete = sorted(row["duration_ms"] for row in latency if row["status"] == "ok")
        def p95(values):
            return values[min(len(values) - 1, round((len(values) - 1) * .95))] if values else None
        return {
            "hours": hours,
            "period_start": start_at.isoformat(timespec="seconds"),
            "period_end": end_at.isoformat(timespec="seconds"),
            "total_requests": counts["total_requests"],
            "speech_requests": counts["speech_requests"] or 0,
            "successful_speech": counts["successful_speech"] or 0,
            "denied_requests": counts["denied_requests"] or 0,
            "first_audio_p95_ms": p95(first),
            "completion_p95_ms": p95(complete),
            "audio_bytes": counts["audio_bytes"] or 0,
            "audio_minutes": round(usage["audio_seconds"] / 60, 2),
            "metered_requests": usage["metered_requests"],
            "usage_breakdown": [{**row, "audio_minutes": round(row["audio_seconds"] / 60, 2)} for row in breakdown],
            "usage_trend": [{**row, "audio_minutes": round(row["audio_seconds"] / 60, 2)} for row in trend],
            "latency_sampled": len(latency),
            "offset": offset,
            "page_size": 100,
            "recent": recent,
        }


@dataclass(eq=False)
class _WaitingRequest:
    key: ClientKey
    future: asyncio.Future
    created: float
    sequence: int


class RequestLimiter:
    """Shared HTTP/WS admission queue; active streams are never preempted."""

    def __init__(self, global_concurrent: int = 4, max_queue: int = 64,
                 wait_timeout: float = 30, per_key_queue: int = 8,
                 aging_seconds: float = 10):
        if (global_concurrent < 1 or max_queue < 0 or wait_timeout <= 0
                or per_key_queue < 1 or aging_seconds <= 0):
            raise ValueError("Invalid speech queue configuration")
        self.global_concurrent = global_concurrent
        self.max_queue = max_queue
        self.wait_timeout = wait_timeout
        self.per_key_queue = per_key_queue
        self.aging_seconds = aging_seconds
        self.active_total = 0
        self.active_by_key = defaultdict(int)
        self.history = defaultdict(deque)
        self.lock = asyncio.Lock()
        self.pending: list[_WaitingRequest] = []
        self._sequence = 0

    def status(self) -> dict:
        return {"queued_speech": len(self.pending), "queue_limit": self.max_queue,
                "queue_wait_seconds": self.wait_timeout,
                "queued_by_priority": {name: sum(item.key.priority == priority for item in self.pending)
                                       for priority, name in ((1, "high"), (2, "normal"), (3, "low"))}}

    def _free_slot(self, key: ClientKey):
        self.active_total -= 1
        self.active_by_key[key.id] -= 1
        if not self.active_by_key[key.id]:
            del self.active_by_key[key.id]

    def _dispatch(self):
        # Called under lock. A key at its own limit cannot block other keys.
        now = time.monotonic()
        while self.active_total < self.global_concurrent:
            eligible = [item for item in self.pending
                        if self.active_by_key[item.key.id] < item.key.max_concurrent]
            if not eligible:
                return
            item = min(eligible, key=lambda item: (
                max(0, item.key.priority - int((now - item.created) / self.aging_seconds)),
                item.sequence))
            self.pending.remove(item)
            self.active_total += 1
            self.active_by_key[item.key.id] += 1
            item.future.set_result(None)

    async def acquire(self, key: ClientKey):
        async with self.lock:
            now = time.monotonic()
            recent = self.history[key.id]
            while recent and recent[0] < now - 60:
                recent.popleft()
            if len(recent) >= key.requests_per_minute:
                raise AccessDenied("Request rate limit reached", 429)
            can_start = (self.active_total < self.global_concurrent
                         and self.active_by_key[key.id] < key.max_concurrent)
            if not can_start and (len(self.pending) >= self.max_queue or
                    sum(item.key.id == key.id for item in self.pending) >= self.per_key_queue):
                raise AccessDenied("Speech queue is full; try again shortly", 429)
            self._sequence += 1
            item = _WaitingRequest(key, asyncio.get_running_loop().create_future(), now, self._sequence)
            self.pending.append(item)
            recent.append(now)
            self._dispatch()
        try:
            await asyncio.wait_for(asyncio.shield(item.future), self.wait_timeout)
        except BaseException as exc:
            # Also covers cancellation just after dispatch reserved a slot.
            async with self.lock:
                if item in self.pending:
                    self.pending.remove(item)
                    item.future.cancel()
                elif item.future.done() and not item.future.cancelled():
                    self._free_slot(key)
                self._dispatch()
            if isinstance(exc, TimeoutError):
                raise AccessDenied("Speech queue wait timed out; try again shortly", 429) from None
            raise

    async def release(self, key: ClientKey):
        async with self.lock:
            if self.active_by_key[key.id] > 0:
                self._free_slot(key)
            self._dispatch()
