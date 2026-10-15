"""用户与登录审计的持久化存储（SQLite）。

为什么选 SQLite 而不是内存 dict / JSON 文件：
- **真实持久化**：进程重启、多 worker 都能读到同一份用户数据（本项目单机部署形态下
  比引入 Postgres 更合适；表结构与 SQL 都是标准 SQL，迁库只需换连接层）；
- 事务与 UNIQUE 约束由数据库保证——邮箱唯一性不能靠应用层「查一下再插」（有竞态）；
- 审计表天然可做合规查询（谁、何时、哪个 IP 登录失败了几次）。

并发策略：每次操作开一个短连接（sqlite3 连接非线程安全，不能跨线程复用），
写操作加进程内锁 + ``WAL`` 模式，避免 "database is locked"。
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from customer_support.common.logging import get_logger
from customer_support.models.user import AuditEntry, UserRecord

logger = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    email           TEXT NOT NULL UNIQUE,
    full_name       TEXT NOT NULL DEFAULT '',
    role            TEXT NOT NULL DEFAULT 'agent',
    password_hash   TEXT NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    last_login_at   TEXT,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT
);

CREATE TABLE IF NOT EXISTS login_audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT NOT NULL,
    user_id    TEXT,
    event      TEXT NOT NULL,
    ip         TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_email ON login_audit(email);
CREATE INDEX IF NOT EXISTS idx_audit_created ON login_audit(created_at);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    jti        TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_refresh_user ON refresh_tokens(user_id);
"""


def _to_iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def _from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class UserStore:
    """用户、刷新令牌与审计日志的数据访问层。"""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._init_schema()

    # ---------------- 基础设施 ----------------
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
        logger.info("用户库就绪: %s", self.db_path)

    # ---------------- 用户 ----------------
    def count_users(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return int(row["n"])

    def create_user(self, record: UserRecord) -> UserRecord:
        """插入用户；邮箱重复由 UNIQUE 约束兜底（并发下也安全）。"""
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (id, email, full_name, role, password_hash, is_active,
                                   created_at, last_login_at, failed_attempts, locked_until)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.email,
                    record.full_name,
                    record.role,
                    record.password_hash,
                    int(record.is_active),
                    _to_iso(record.created_at),
                    _to_iso(record.last_login_at),
                    record.failed_attempts,
                    _to_iso(record.locked_until),
                ),
            )
        return record

    def get_user_by_email(self, email: str) -> UserRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (email.lower(),)).fetchone()
        return self._row_to_user(row) if row else None

    def get_user_by_id(self, user_id: str) -> UserRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._row_to_user(row) if row else None

    def list_users(self, *, limit: int = 100) -> list[UserRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_user(r) for r in rows]

    def record_login_success(self, user_id: str, *, at: datetime) -> None:
        """登录成功：清零失败计数、解除锁定、记录最近登录时间。"""
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE users
                   SET failed_attempts = 0, locked_until = NULL, last_login_at = ?
                 WHERE id = ?
                """,
                (_to_iso(at), user_id),
            )

    def record_login_failure(self, user_id: str, *, failed_attempts: int, locked_until: datetime | None) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?",
                (failed_attempts, _to_iso(locked_until), user_id),
            )

    def set_password_hash(self, user_id: str, password_hash: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, failed_attempts = 0, locked_until = NULL WHERE id = ?",
                (password_hash, user_id),
            )

    def set_active(self, user_id: str, *, is_active: bool) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (int(is_active), user_id))

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> UserRecord:
        return UserRecord(
            id=row["id"],
            email=row["email"],
            full_name=row["full_name"],
            role=row["role"],
            password_hash=row["password_hash"],
            is_active=bool(row["is_active"]),
            created_at=_from_iso(row["created_at"]) or datetime.now(timezone.utc),
            last_login_at=_from_iso(row["last_login_at"]),
            failed_attempts=int(row["failed_attempts"]),
            locked_until=_from_iso(row["locked_until"]),
        )

    # ---------------- 刷新令牌（可撤销） ----------------
    def save_refresh_token(self, *, jti: str, user_id: str, expires_at: datetime) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO refresh_tokens (jti, user_id, expires_at, revoked, created_at) "
                "VALUES (?, ?, ?, 0, ?)",
                (jti, user_id, _to_iso(expires_at), _to_iso(datetime.now(timezone.utc))),
            )

    def is_refresh_token_valid(self, jti: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT revoked, expires_at FROM refresh_tokens WHERE jti = ?", (jti,)).fetchone()
        if not row:
            return False
        if int(row["revoked"]):
            return False
        expires_at = _from_iso(row["expires_at"])
        return bool(expires_at and expires_at > datetime.now(timezone.utc))

    def revoke_refresh_token(self, jti: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("UPDATE refresh_tokens SET revoked = 1 WHERE jti = ?", (jti,))

    def revoke_all_for_user(self, user_id: str) -> None:
        """改密/停用/检测到异常时，一次踢掉该用户所有刷新令牌。"""
        with self._write_lock, self._connect() as conn:
            conn.execute("UPDATE refresh_tokens SET revoked = 1 WHERE user_id = ?", (user_id,))

    # ---------------- 审计 ----------------
    def add_audit(
        self,
        *,
        email: str,
        event: str,
        user_id: str | None = None,
        ip: str = "",
        user_agent: str = "",
        detail: str = "",
    ) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO login_audit (email, user_id, event, ip, user_agent, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (email, user_id, event, ip, user_agent[:200], detail[:500], _to_iso(datetime.now(timezone.utc))),
            )

    def list_audit(self, *, limit: int = 100, email: str | None = None) -> list[AuditEntry]:
        query = "SELECT * FROM login_audit"
        params: list[object] = []
        if email:
            query += " WHERE email = ?"
            params.append(email.lower())
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            AuditEntry(
                id=int(r["id"]),
                email=r["email"],
                user_id=r["user_id"],
                event=r["event"],
                ip=r["ip"],
                user_agent=r["user_agent"],
                detail=r["detail"],
                created_at=_from_iso(r["created_at"]) or datetime.now(timezone.utc),
            )
            for r in rows
        ]
