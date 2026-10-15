"""工单存储与生命周期管理（SQLite）。

一张工单从 AI 转出到真人闭环，需要四件事，这里逐一对齐：

1. **可查**：按状态/优先级/处理人/原因/日期/关键词筛选，支持分页；
2. **可改**：状态流转、优先级调整、指派处理人（每次变更都刷新 ``updated_at``）；
3. **可回复**：人工回复与内部备注分开记录；首次非内部回复写入 ``first_response_at``
   （SLA 的「首次响应时长」就靠它）；
4. **可度量**：积压量、按状态/优先级/原因分布、平均首次响应与解决时长。

状态机：``open`` ⇄ ``in_progress`` → ``resolved`` → ``closed``。
``resolved`` 时写 ``resolved_at``；回退到 ``open``/``in_progress`` 时清空 ``resolved_at``
（避免「又开了但解决时间还留着」的脏数据）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from customer_support.common.logging import get_logger
from customer_support.models.ticket import Ticket, TicketReply
from customer_support.store.base import BaseStore, from_iso, to_iso, utcnow

logger = get_logger(__name__)

_RESOLVED_STATES = {"resolved", "closed"}


def _row_to_ticket(row: Any, replies: list[TicketReply] | None = None) -> Ticket:
    return Ticket(
        ticket_id=row["ticket_id"],
        created_at=from_iso(row["created_at"]) or utcnow(),
        updated_at=from_iso(row["updated_at"]) or utcnow(),
        status=row["status"],
        priority=row["priority"],
        assignee=row["assignee"] or "",
        question=row["question"],
        language=row["language"],
        intent=row["intent"],
        confidence=float(row["confidence"] or 0),
        reason=row["reason"],
        asked_answer=row["asked_answer"],
        buyer_id=row["buyer_id"],
        session_id=row["session_id"],
        trace_id=row["trace_id"],
        sources=json.loads(row["sources_json"] or "[]"),
        first_response_at=from_iso(row["first_response_at"]),
        resolved_at=from_iso(row["resolved_at"]),
        replies=replies or [],
    )


def _row_to_reply(row: Any) -> TicketReply:
    return TicketReply(
        id=int(row["id"]),
        ticket_id=row["ticket_id"],
        author=row["author"],
        body=row["body"],
        internal=bool(row["internal"]),
        created_at=from_iso(row["created_at"]) or utcnow(),
    )


class TicketStore(BaseStore):
    """工单数据访问与生命周期。"""

    # ---------------- 写入 ----------------
    def create(self, ticket: Ticket) -> Ticket:
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO tickets (ticket_id, created_at, updated_at, status, priority,
                                     assignee, question, language, intent, confidence, reason,
                                     asked_answer, sources_json, buyer_id, session_id, trace_id,
                                     first_response_at, resolved_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    ticket.ticket_id,
                    to_iso(ticket.created_at),
                    to_iso(ticket.updated_at),
                    ticket.status,
                    ticket.priority,
                    ticket.assignee,
                    ticket.question,
                    ticket.language,
                    ticket.intent,
                    ticket.confidence,
                    ticket.reason,
                    ticket.asked_answer,
                    json.dumps(ticket.sources, ensure_ascii=False),
                    ticket.buyer_id,
                    ticket.session_id,
                    ticket.trace_id,
                    to_iso(ticket.first_response_at),
                    to_iso(ticket.resolved_at),
                ),
            )
        logger.info(
            "创建转人工工单 %s (reason=%s, priority=%s, intent=%s, confidence=%.2f)",
            ticket.ticket_id, ticket.reason, ticket.priority, ticket.intent, ticket.confidence,
        )
        return ticket

    def update(self, ticket_id: str, **fields: Any) -> Ticket | None:
        """更新工单字段（只允许白名单字段）。返回更新后的工单。"""
        allowed = {"status", "priority", "assignee"}
        changes = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not changes:
            return self.get(ticket_id)

        now = utcnow()
        sets = [f"{k} = ?" for k in changes]
        params: list[Any] = list(changes.values())
        sets.append("updated_at = ?")
        params.append(to_iso(now))

        # 解决时间跟随状态变化：进入终态写入，回退则清空
        if "status" in changes:
            if changes["status"] in _RESOLVED_STATES:
                sets.append("resolved_at = COALESCE(resolved_at, ?)")
                params.append(to_iso(now))
            else:
                sets.append("resolved_at = NULL")
        params.append(ticket_id)

        with self._write() as conn:
            conn.execute(f"UPDATE tickets SET {', '.join(sets)} WHERE ticket_id = ?", params)
        return self.get(ticket_id)

    def add_reply(self, ticket_id: str, reply: TicketReply) -> Ticket | None:
        """追加人工回复；首条**非内部**回复写入 ``first_response_at``（SLA）。"""
        now = reply.created_at or utcnow()
        with self._write() as conn:
            conn.execute(
                "INSERT INTO ticket_replies (ticket_id, author, body, internal, created_at) "
                "VALUES (?,?,?,?,?)",
                (ticket_id, reply.author, reply.body, int(reply.internal), to_iso(now)),
            )
            if reply.internal:
                conn.execute(
                    "UPDATE tickets SET updated_at = ? WHERE ticket_id = ?",
                    (to_iso(now), ticket_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE tickets
                       SET updated_at = ?,
                           first_response_at = COALESCE(first_response_at, ?),
                           status = CASE WHEN status = 'open' THEN 'in_progress' ELSE status END
                     WHERE ticket_id = ?
                    """,
                    (to_iso(now), to_iso(now), ticket_id),
                )
        logger.info("工单 %s 新增%s回复（%s）", ticket_id, "内部" if reply.internal else "对外", reply.author)
        return self.get(ticket_id)

    # ---------------- 读取 ----------------
    def get(self, ticket_id: str, *, with_replies: bool = True) -> Ticket | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if not row:
                return None
            replies: list[TicketReply] = []
            if with_replies:
                rows = conn.execute(
                    "SELECT * FROM ticket_replies WHERE ticket_id = ? ORDER BY id", (ticket_id,)
                ).fetchall()
                replies = [_row_to_reply(r) for r in rows]
        return _row_to_ticket(row, replies)

    def query(
        self,
        *,
        status: str | None = None,
        priority: str | None = None,
        assignee: str | None = None,
        reason: str | None = None,
        day: datetime | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Ticket], int]:
        """筛选工单，返回 ``(当页列表, 命中总数)``。

        排序规则：**未处理优先于已处理，同组内优先级高的靠前，再按时间倒序**——
        值班同学打开页面第一眼就该看到「最该处理的那条」，而不是最近的那条。
        """
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("status = ?")
            params.append(status)
        if priority:
            where.append("priority = ?")
            params.append(priority)
        if assignee:
            where.append("assignee = ?")
            params.append(assignee)
        if reason:
            where.append("reason = ?")
            params.append(reason)
        if search:
            where.append("(question LIKE ? OR ticket_id LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like])
        if day:
            start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
            end = start + timedelta(days=1)
            where.append("created_at >= ? AND created_at < ?")
            params.extend([to_iso(start), to_iso(end)])

        clause = f"WHERE {' AND '.join(where)}" if where else ""
        order = (
            "ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'in_progress' THEN 1 "
            "WHEN 'resolved' THEN 2 ELSE 3 END, "
            "CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 "
            "WHEN 'normal' THEN 2 ELSE 3 END, created_at DESC"
        )
        with self._read() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) AS n FROM tickets {clause}", params).fetchone()["n"])
            rows = conn.execute(
                f"SELECT * FROM tickets {clause} {order} LIMIT ? OFFSET ?",
                (*params, max(1, min(limit, 200)), max(0, offset)),
            ).fetchall()
        return [_row_to_ticket(r) for r in rows], total

    def list_tickets(self, day: datetime | None = None) -> list[Ticket]:
        """兼容旧接口：某天的全部工单（默认全部）。"""
        tickets, _ = self.query(day=day, limit=200)
        return tickets

    def recent(self, *, days: int = 30, limit: int = 200) -> list[Ticket]:
        """最近 N 天的工单（趋势统计用）。"""
        cutoff = utcnow() - timedelta(days=days)
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM tickets WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
                (to_iso(cutoff), limit),
            ).fetchall()
        return [_row_to_ticket(r) for r in rows]

    # ---------------- 统计 ----------------
    def stats(self, *, days: int = 7) -> dict[str, Any]:
        """工单运营看板数据：积压、分布与 SLA 时长。"""
        tickets = self.recent(days=days, limit=5000)
        now = utcnow()

        by_status: dict[str, int] = {}
        by_priority: dict[str, int] = {}
        by_reason: dict[str, int] = {}
        by_assignee: dict[str, int] = {}
        response_minutes: list[float] = []
        resolve_minutes: list[float] = []
        overdue = 0

        for t in tickets:
            by_status[t.status] = by_status.get(t.status, 0) + 1
            by_priority[t.priority] = by_priority.get(t.priority, 0) + 1
            by_reason[t.reason] = by_reason.get(t.reason, 0) + 1
            key = t.assignee or "未指派"
            by_assignee[key] = by_assignee.get(key, 0) + 1
            if t.first_response_at:
                response_minutes.append(
                    (t.first_response_at - t.created_at).total_seconds() / 60
                )
            if t.resolved_at:
                resolve_minutes.append((t.resolved_at - t.created_at).total_seconds() / 60)
            # 未处理且超过 24 小时 = 超时积压（粗略 SLA，可按业务调整）
            if t.status in {"open", "in_progress"} and (now - t.created_at) > timedelta(hours=24):
                overdue += 1

        return {
            "window_days": days,
            "total": len(tickets),
            "backlog": sum(by_status.get(s, 0) for s in ("open", "in_progress")),
            "unassigned": by_assignee.get("未指派", 0),
            "overdue": overdue,
            "by_status": by_status,
            "by_priority": by_priority,
            "by_reason": by_reason,
            "by_assignee": by_assignee,
            "avg_first_response_minutes": round(sum(response_minutes) / len(response_minutes), 1)
            if response_minutes
            else None,
            "avg_resolve_minutes": round(sum(resolve_minutes) / len(resolve_minutes), 1)
            if resolve_minutes
            else None,
        }

    def summary(self, day: datetime | None = None) -> dict[str, Any]:
        """兼容旧接口：某天的按原因/意图汇总。"""
        tickets = self.list_tickets(day)
        by_reason: dict[str, int] = {}
        by_intent: dict[str, int] = {}
        for t in tickets:
            by_reason[t.reason] = by_reason.get(t.reason, 0) + 1
            by_intent[t.intent] = by_intent.get(t.intent, 0) + 1
        when = day or utcnow()
        return {
            "date": when.strftime("%Y-%m-%d"),
            "total": len(tickets),
            "by_reason": by_reason,
            "by_intent": by_intent,
            "open": sum(1 for t in tickets if t.status == "open"),
        }
