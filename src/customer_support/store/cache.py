"""精确问答缓存（SQLite，带 TTL 与命中计数）。

跨境电商客服的问题高度重复：「送料はいくらですか？」「返品はできますか？」这类
FAQ 每天被不同买家问几百遍。全量走一遍「意图识别 → 检索 → 生成」既费钱又慢，
这里把结果缓存下来。

**只在安全前提下命中**（见 ``ChatService``）：

1. 只缓存 ``source == "knowledge"`` 且未转人工的回答——人工话术与兜底话术不该被复用；
2. 只缓存**无会话上下文**（会话首轮）的问题——多轮追问的答案依赖上文，缓存会答错；
3. 缓存键包含语言与规范化后的问题文本，避免中英日同形不同义串味。

命中时返回的 ``ChatResult`` 会把 ``cached`` 置 True，日志与前端都会标明"来自缓存"。
"""
from __future__ import annotations

import hashlib
import re
from datetime import timedelta

from customer_support.common.logging import get_logger
from customer_support.models.message import ChatResult
from customer_support.store.base import BaseStore, to_iso, utcnow

logger = get_logger(__name__)

_WS_RE = re.compile(r"\s+")


def normalize(question: str) -> str:
    """问题规范化：去首尾空白、压缩连续空白、统一全角空格、大小写归一。"""
    q = question.replace("\u3000", " ").strip()
    q = _WS_RE.sub(" ", q)
    return q.lower()


def cache_key(question: str, language: str) -> str:
    return hashlib.sha1(f"{language}\u0000{normalize(question)}".encode()).hexdigest()


class AnswerCacheStore(BaseStore):
    """问答缓存读写。"""

    def get(self, key: str) -> ChatResult | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT payload, expires_at FROM answer_cache WHERE cache_key = ?", (key,)
            ).fetchone()
        if not row:
            return None
        expires_at = row["expires_at"]
        if expires_at <= to_iso(utcnow()):
            self._delete(key)
            return None
        try:
            result = ChatResult.model_validate_json(row["payload"])
        except Exception:  # noqa: BLE001 - 历史 payload 结构变化时按未命中处理
            self._delete(key)
            return None
        self._bump(key)
        return result.model_copy(update={"cached": True, "latency_ms": 0})

    def put(self, key: str, question: str, language: str, result: ChatResult, *, ttl_seconds: int) -> None:
        now = utcnow()
        # 缓存里存"原始"结果（cached=False），命中时再打标，避免缓存里存了 True 又二次打标
        payload = result.model_copy(update={"cached": False}).model_dump_json()
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO answer_cache (cache_key, question, language, payload, hits,
                                          created_at, expires_at)
                VALUES (?,?,?,?,0,?,?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    payload = excluded.payload,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (key, question[:2000], language, payload, to_iso(now),
                 to_iso(now + timedelta(seconds=ttl_seconds))),
            )

    def _bump(self, key: str) -> None:
        with self._write() as conn:
            conn.execute("UPDATE answer_cache SET hits = hits + 1 WHERE cache_key = ?", (key,))

    def _delete(self, key: str) -> None:
        with self._write() as conn:
            conn.execute("DELETE FROM answer_cache WHERE cache_key = ?", (key,))

    def purge_expired(self) -> int:
        with self._write() as conn:
            cur = conn.execute("DELETE FROM answer_cache WHERE expires_at <= ?", (to_iso(utcnow()),))
        return int(cur.rowcount or 0)

    def clear(self) -> int:
        with self._write() as conn:
            cur = conn.execute("DELETE FROM answer_cache")
        return int(cur.rowcount or 0)

    def stats(self) -> dict[str, int]:
        with self._read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS entries, COALESCE(SUM(hits), 0) AS hits FROM answer_cache"
            ).fetchone()
        return {"entries": int(row["entries"]), "hits": int(row["hits"])}
