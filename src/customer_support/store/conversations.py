"""会话与消息的持久化存储（SQLite）。

写入形态是「一问一答成对落库」：``record_turn`` 一次写两条（买家问题 + 客服回答），
并 upsert 会话摘要（消息数、转人工次数、最近更新时间）。

这样做的理由：客服链路的天然事务边界就是「一轮问答」，把它作为原子写单元，
既避免了「只写了问题没写回答」的中间态，也让上层（ChatService）只需调用一次。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from customer_support.models.conversation import (
    ConversationDetail,
    ConversationSummary,
    StoredMessage,
)
from customer_support.models.message import ChatResult, Reference
from customer_support.store.base import BaseStore, from_iso, to_iso, utcnow


def _row_to_message(row: Any) -> StoredMessage:
    refs = json.loads(row["references_json"] or "[]")
    return StoredMessage(
        id=int(row["id"]),
        session_id=row["session_id"],
        role=row["role"],
        text=row["text"],
        intent=row["intent"],
        need_human=bool(row["need_human"]),
        human_reason=row["human_reason"],
        source=row["source"],
        confidence=float(row["confidence"] or 0),
        ticket_id=row["ticket_id"],
        trace_id=row["trace_id"],
        latency_ms=int(row["latency_ms"] or 0),
        references=[Reference(**r) for r in refs],
        created_at=from_iso(row["created_at"]) or utcnow(),
    )


class ConversationStore(BaseStore):
    """会话 / 消息读写。"""

    # ---------------- 写入 ----------------
    def record_turn(
        self,
        *,
        session_id: str,
        question: str,
        result: ChatResult,
        buyer_id: str | None = None,
        at: datetime | None = None,
    ) -> None:
        """一轮问答成对落库（买家问题 + 客服回答），并更新会话摘要。"""
        now = at or utcnow()
        stamp = to_iso(now)
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO conversations (session_id, buyer_id, language, created_at,
                                           updated_at, message_count, handoff_count)
                VALUES (?,?,?,?,?,0,0)
                ON CONFLICT(session_id) DO UPDATE SET
                    buyer_id = COALESCE(NULLIF(excluded.buyer_id, ''), conversations.buyer_id),
                    language = excluded.language,
                    updated_at = excluded.updated_at
                """,
                (session_id, buyer_id or "", result.language, stamp, stamp),
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, text, created_at) VALUES (?,?,?,?)",
                (session_id, "buyer", question, stamp),
            )
            conn.execute(
                """
                INSERT INTO messages (session_id, role, text, intent, need_human, human_reason,
                                      source, confidence, ticket_id, trace_id, latency_ms,
                                      references_json, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    session_id,
                    "agent",
                    result.answer,
                    result.intent,
                    int(result.need_human),
                    result.human_reason,
                    result.source,
                    result.confidence,
                    result.ticket_id,
                    result.trace_id,
                    result.latency_ms,
                    json.dumps([r.model_dump() for r in result.references], ensure_ascii=False),
                    stamp,
                ),
            )
            conn.execute(
                """
                UPDATE conversations
                   SET message_count = message_count + 2,
                       handoff_count = handoff_count + ?,
                       updated_at = ?
                 WHERE session_id = ?
                """,
                (1 if result.need_human else 0, stamp, session_id),
            )

    def delete(self, session_id: str) -> None:
        with self._write() as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM conversations WHERE session_id = ?", (session_id,))

    # ---------------- 读取 ----------------
    def list_conversations(
        self, *, limit: int = 30, offset: int = 0, search: str | None = None, days: int | None = None
    ) -> tuple[list[ConversationSummary], int]:
        where: list[str] = []
        params: list[Any] = []
        if days:
            cutoff = to_iso(utcnow() - timedelta(days=days))
            where.append("updated_at >= ?")
            params.append(cutoff)
        if search:
            where.append("session_id LIKE ?")
            params.append(f"%{search}%")
        clause = f"WHERE {' AND '.join(where)}" if where else ""

        with self._read() as conn:
            total = int(
                conn.execute(f"SELECT COUNT(*) AS n FROM conversations {clause}", params).fetchone()["n"]
            )
            rows = conn.execute(
                f"SELECT * FROM conversations {clause} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (*params, max(1, min(limit, 100)), max(0, offset)),
            ).fetchall()
            summaries: list[ConversationSummary] = []
            for row in rows:
                summaries.append(self._summarize(conn, row))
        return summaries, total

    def get_conversation(self, session_id: str) -> ConversationDetail | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE session_id = ?", (session_id,)
            ).fetchone()
            if not row:
                return None
            base = self._summarize(conn, row)
            msg_rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id", (session_id,)
            ).fetchall()
        messages = [_row_to_message(r) for r in msg_rows]
        return ConversationDetail(**base.model_dump(), messages=messages)

    @staticmethod
    def _summarize(conn: Any, row: Any) -> ConversationSummary:
        session_id = row["session_id"]
        last = conn.execute(
            "SELECT text FROM messages WHERE session_id = ? AND role = 'buyer' ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        tickets = conn.execute(
            "SELECT DISTINCT ticket_id FROM messages WHERE session_id = ? AND ticket_id IS NOT NULL",
            (session_id,),
        ).fetchall()
        return ConversationSummary(
            session_id=session_id,
            buyer_id=row["buyer_id"],
            language=row["language"],
            created_at=from_iso(row["created_at"]) or utcnow(),
            updated_at=from_iso(row["updated_at"]) or utcnow(),
            message_count=int(row["message_count"] or 0),
            handoff_count=int(row["handoff_count"] or 0),
            last_question=last["text"] if last else "",
            ticket_ids=[t["ticket_id"] for t in tickets if t["ticket_id"]],
        )

    def stats(self) -> dict[str, int]:
        with self._read() as conn:
            conv = int(conn.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"])
            msg = int(conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"])
        return {"conversations": conv, "messages": msg}

    def metrics(self, *, days: int = 7) -> dict[str, Any]:
        """会话/消息聚合指标（运营仪表盘与 /metrics 的数据源）。

        口径说明：

        - ``containment_rate``（自助解决率）= 没有转人工的回答数 / 回答总数。
          这是智能客服最核心的业务指标——值越高，人工被解放得越多；
        - 延迟只统计 ``latency_ms > 0`` 的回答，把缓存命中的 0 排除在外，
          否则缓存会把平均延迟"刷"得很好看，掩盖真实的服务端耗时；
        - 按 UTC 日期分桶（与 RAGFlow / 日志一致），避免跨时区口径打架。
        """
        cutoff = to_iso(utcnow() - timedelta(days=days))
        with self._read() as conn:
            agg = conn.execute(
                """
                SELECT
                    COUNT(*)                                        AS answers,
                    COALESCE(SUM(need_human), 0)                    AS handoffs,
                    COALESCE(AVG(CASE WHEN latency_ms > 0 THEN latency_ms END), 0) AS avg_latency
                  FROM messages
                 WHERE role = 'agent' AND created_at >= ?
                """,
                (cutoff,),
            ).fetchone()
            questions = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM messages WHERE role = 'buyer' AND created_at >= ?",
                    (cutoff,),
                ).fetchone()["n"]
            )
            conversations = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM conversations WHERE updated_at >= ?", (cutoff,)
                ).fetchone()["n"]
            )
            by_source = {
                row["source"] or "unknown": int(row["n"])
                for row in conn.execute(
                    "SELECT source, COUNT(*) AS n FROM messages "
                    "WHERE role = 'agent' AND created_at >= ? GROUP BY source",
                    (cutoff,),
                ).fetchall()
            }
            by_intent = {
                row["intent"] or "unknown": int(row["n"])
                for row in conn.execute(
                    "SELECT intent, COUNT(*) AS n FROM messages "
                    "WHERE role = 'agent' AND created_at >= ? GROUP BY intent",
                    (cutoff,),
                ).fetchall()
            }
            by_reason = {
                row["human_reason"] or "unknown": int(row["n"])
                for row in conn.execute(
                    "SELECT human_reason, COUNT(*) AS n FROM messages "
                    "WHERE role = 'agent' AND need_human = 1 AND created_at >= ? "
                    "GROUP BY human_reason",
                    (cutoff,),
                ).fetchall()
            }
            daily = [
                {
                    "day": row["day"],
                    "questions": int(row["questions"]),
                    "answers": int(row["answers"]),
                    "handoffs": int(row["handoffs"]),
                    "avg_latency_ms": round(float(row["avg_latency"] or 0)),
                }
                for row in conn.execute(
                    """
                    SELECT substr(created_at, 1, 10) AS day,
                           COALESCE(SUM(role = 'buyer'), 0)  AS questions,
                           COALESCE(SUM(role = 'agent'), 0)  AS answers,
                           COALESCE(SUM(role = 'agent' AND need_human = 1), 0) AS handoffs,
                           COALESCE(AVG(CASE WHEN role = 'agent' AND latency_ms > 0
                                             THEN latency_ms END), 0) AS avg_latency
                      FROM messages
                     WHERE created_at >= ?
                     GROUP BY day ORDER BY day
                    """,
                    (cutoff,),
                ).fetchall()
            ]

        answers = int(agg["answers"] or 0)
        handoffs = int(agg["handoffs"] or 0)
        return {
            "window_days": days,
            "conversations": conversations,
            "questions": questions,
            "answers": answers,
            "handoffs": handoffs,
            "containment_rate": round(1 - handoffs / answers, 4) if answers else None,
            "handoff_rate": round(handoffs / answers, 4) if answers else None,
            "avg_latency_ms": round(float(agg["avg_latency"] or 0)),
            "by_source": by_source,
            "by_intent": by_intent,
            "by_handoff_reason": by_reason,
            "daily": daily,
        }
