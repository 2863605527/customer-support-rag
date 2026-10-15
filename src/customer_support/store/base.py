"""SQLite 存储基类：连接管理、建表、写锁、时间戳工具。

设计要点：

- **每次操作开一个短连接**：``sqlite3.Connection`` 不能跨线程复用，短连接最省心；
- **写操作用进程内锁串行化**：SQLite 单写者模型下，多线程并发写在拿到锁前就
  互相等待，加锁可避免 ``database is locked`` 频繁冒泡；
- **WAL 模式**：读不阻塞写、写不阻塞读，值班面板读数据时不会卡住客服写工单；
- **建表语句集中一处**：所有表在 ``_SCHEMA`` 里，启动即幂等建表，无需迁移脚本
  （新增列时用 ``ALTER TABLE`` + 忽略异常，见 ``_ensure_column``）。
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from customer_support.common.logging import get_logger

logger = get_logger(__name__)

_SCHEMA = """
-- ==================== 会话与消息 ====================
CREATE TABLE IF NOT EXISTS conversations (
    session_id      TEXT PRIMARY KEY,
    buyer_id        TEXT NOT NULL DEFAULT '',
    language        TEXT NOT NULL DEFAULT 'unknown',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    message_count   INTEGER NOT NULL DEFAULT 0,
    handoff_count   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    role            TEXT NOT NULL,              -- buyer | agent
    text            TEXT NOT NULL DEFAULT '',
    intent          TEXT NOT NULL DEFAULT 'unknown',
    need_human      INTEGER NOT NULL DEFAULT 0,
    human_reason    TEXT NOT NULL DEFAULT '',
    source          TEXT NOT NULL DEFAULT '',
    confidence      REAL NOT NULL DEFAULT 0,
    ticket_id       TEXT,
    trace_id        TEXT NOT NULL DEFAULT '',
    latency_ms      INTEGER NOT NULL DEFAULT 0,
    references_json TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

-- ==================== 工单 ====================
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id          TEXT PRIMARY KEY,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'open',    -- open|in_progress|resolved|closed
    priority           TEXT NOT NULL DEFAULT 'normal',  -- low|normal|high|urgent
    assignee           TEXT NOT NULL DEFAULT '',
    question           TEXT NOT NULL,
    language           TEXT NOT NULL DEFAULT 'unknown',
    intent             TEXT NOT NULL DEFAULT 'unknown',
    confidence         REAL NOT NULL DEFAULT 0,
    reason             TEXT NOT NULL DEFAULT '',
    asked_answer       TEXT NOT NULL DEFAULT '',
    sources_json       TEXT NOT NULL DEFAULT '[]',
    buyer_id           TEXT,
    session_id         TEXT,
    trace_id           TEXT NOT NULL DEFAULT '',
    first_response_at  TEXT,
    resolved_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_created ON tickets(created_at DESC);

CREATE TABLE IF NOT EXISTS ticket_replies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id       TEXT NOT NULL,
    author          TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL,
    internal        INTEGER NOT NULL DEFAULT 0,   -- 1=内部备注（不发给买家）
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_replies_ticket ON ticket_replies(ticket_id, id);

-- ==================== 质量反馈 ====================
CREATE TABLE IF NOT EXISTS feedback (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id     TEXT NOT NULL,
    session_id   TEXT,
    rating       TEXT NOT NULL,                  -- up | down
    reasons_json TEXT NOT NULL DEFAULT '[]',
    comment      TEXT NOT NULL DEFAULT '',
    question     TEXT NOT NULL DEFAULT '',
    answer       TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
-- 同一条回答只保留最后一次评价（买家改主意时不产生重复样本）
CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_trace ON feedback(trace_id);
CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback(created_at DESC);

-- ==================== 精确问答缓存 ====================
CREATE TABLE IF NOT EXISTS answer_cache (
    cache_key   TEXT PRIMARY KEY,
    question    TEXT NOT NULL,
    language    TEXT NOT NULL,
    payload     TEXT NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_expires ON answer_cache(expires_at);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# 写锁按「库文件」注册，而不是按实例持有。
#
# 为什么必须这样：``ConversationStore`` / ``TicketStore`` / ``FeedbackStore`` /
# ``AnswerCacheStore`` 是**四个实例、同一个文件**（``CONSOLE_DB_PATH``）。
# 若每个实例各持一把自己的 ``threading.Lock``，它们互相之间根本不互斥——
# 一次聊天（写会话+消息）与一次工单更新（写 tickets）会同时进入 SQLite 的
# 单写者临界区，表现为并发下偶发 ``database is locked``（10s busy_timeout 耗尽）。
# 早期实现就是实例级锁，与「共享写锁」的注释不符，这里改为按文件路径注册。
_write_locks: dict[str, threading.Lock] = {}
_write_locks_guard = threading.Lock()


def _write_lock_for(db_path: Path) -> threading.Lock:
    """取（或创建）该库文件全局唯一的写锁。"""
    key = str(db_path.resolve())
    with _write_locks_guard:
        lock = _write_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _write_locks[key] = lock
        return lock


class BaseStore:
    """SQLite 存储基类。指向同一个库文件的实例**共享同一把写锁**。"""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = _write_lock_for(self.db_path)
        self._init_schema()

    # ---------------- 基础设施 ----------------
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        # foreign_keys 是**连接级**开关（默认 OFF），只在建表那一条连接上开是没用的：
        # 之后每个业务连接都要重新开，否则加了外键约束也不会生效。
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock, self._connect() as conn:
            yield conn

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            yield conn

    def _init_schema(self) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(_SCHEMA)
        logger.info("运营库就绪: %s", self.db_path)
