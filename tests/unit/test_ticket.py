"""工单服务单测：创建、筛选、状态流转、人工回复与 SLA 统计。

覆盖的是「工单能不能被真人接住并走完」这条闭环——只记录不处理等于没有工单。
"""
from datetime import datetime, timedelta, timezone

from customer_support.models.ticket import Ticket, TicketReply
from customer_support.services.ticket import TicketService
from customer_support.store.tickets import TicketStore


def _service(tmp_path) -> TicketService:
    return TicketService(TicketStore(tmp_path / "console.db"))


def _ticket(reason: str, intent: str = "human", **over) -> Ticket:
    payload = dict(
        ticket_id=TicketService.new_ticket_id(),
        question="返金してください",
        language="ja",
        intent=intent,
        confidence=0.9,
        reason=reason,
    )
    payload.update(over)  # 允许覆盖任意字段（如 question / created_at），避免重复关键字参数
    return Ticket(**payload)


class TestTicketLifecycle:
    def test_create_and_list(self, tmp_path):
        svc = _service(tmp_path)
        svc.create(_ticket("high_risk_intent"))
        tickets = svc.list_tickets()
        assert len(tickets) == 1
        assert tickets[0].reason == "high_risk_intent"
        assert tickets[0].status == "open"

    def test_default_priority_follows_reason(self, tmp_path):
        """按转人工原因的默认优先级：欠费/系统故障最急，高风险次之。"""
        svc = _service(tmp_path)
        svc.create(_ticket("service_unavailable"))
        svc.create(_ticket("high_risk_intent"))
        svc.create(_ticket("no_knowledge"))
        by_reason = {t.reason: t.priority for t in svc.list_tickets()}
        assert by_reason["service_unavailable"] == "urgent"
        assert by_reason["high_risk_intent"] == "high"
        assert by_reason["no_knowledge"] == "normal"

    def test_summary_counts(self, tmp_path):
        svc = _service(tmp_path)
        svc.create(_ticket("high_risk_intent", intent="human"))
        svc.create(_ticket("high_risk_intent", intent="human"))
        svc.create(_ticket("no_knowledge", intent="logistics"))
        summary = svc.summary()
        assert summary["total"] == 3
        assert summary["by_reason"]["high_risk_intent"] == 2
        assert summary["by_intent"]["logistics"] == 1
        assert summary["open"] == 3

    def test_list_missing_day_returns_empty(self, tmp_path):
        svc = _service(tmp_path)
        assert svc.list_tickets(datetime(2000, 1, 1, tzinfo=timezone.utc)) == []

    def test_ticket_id_format(self):
        tid = TicketService.new_ticket_id()
        assert tid.startswith("T")
        assert "-" in tid
        assert len(tid.split("-")[1]) == 6


class TestTicketWorkflow:
    def test_status_transition_records_resolved_at(self, tmp_path):
        svc = _service(tmp_path)
        ticket = svc.create(_ticket("high_risk_intent"))
        assert ticket.resolved_at is None

        updated = svc.update(ticket.ticket_id, status="resolved")
        assert updated is not None
        assert updated.status == "resolved"
        assert updated.resolved_at is not None

        # 回退到处理中必须清空解决时间，避免「重新打开了但解决时间还留着」的脏数据
        rolled_back = svc.update(ticket.ticket_id, status="in_progress")
        assert rolled_back is not None
        assert rolled_back.resolved_at is None

    def test_assign_and_priority_update(self, tmp_path):
        svc = _service(tmp_path)
        ticket = svc.create(_ticket("low_confidence"))
        updated = svc.update(ticket.ticket_id, assignee="agent@shop.jp", priority="high")
        assert updated is not None
        assert updated.assignee == "agent@shop.jp"
        assert updated.priority == "high"

    def test_unknown_ticket_returns_none(self, tmp_path):
        svc = _service(tmp_path)
        assert svc.update("T-nonexistent", status="closed") is None
        assert svc.get("T-nonexistent") is None

    def test_public_reply_sets_first_response_and_advances_status(self, tmp_path):
        svc = _service(tmp_path)
        ticket = svc.create(_ticket("no_knowledge"))

        # 内部备注不算「首次响应」——买家没收到消息就不该计入 SLA
        svc.add_reply(ticket.ticket_id, TicketReply(author="a@shop.jp", body="内部：先查库存", internal=True))
        assert svc.get(ticket.ticket_id).first_response_at is None  # type: ignore[union-attr]

        svc.add_reply(ticket.ticket_id, TicketReply(author="a@shop.jp", body="在庫を確認しました"))
        after = svc.get(ticket.ticket_id)
        assert after is not None
        assert after.first_response_at is not None
        assert after.status == "in_progress"
        assert len(after.replies) == 2

    def test_query_filters_and_orders_by_urgency(self, tmp_path):
        svc = _service(tmp_path)
        normal = svc.create(_ticket("no_knowledge"))
        urgent = svc.create(_ticket("service_unavailable"))
        svc.update(normal.ticket_id, status="closed")

        items, total = svc.query(status="open")
        assert total == 1
        assert items[0].ticket_id == urgent.ticket_id

        # search 同时匹配问题与工单号
        found, _ = svc.query(search=urgent.ticket_id[:8])
        assert any(t.ticket_id == urgent.ticket_id for t in found)

    def test_stats_report_backlog_and_sla(self, tmp_path):
        svc = _service(tmp_path)
        ticket = svc.create(_ticket("high_risk_intent", question="返品したい"))
        svc.add_reply(ticket.ticket_id, TicketReply(author="a@shop.jp", body="確認します"))

        stats = svc.stats(days=7)
        assert stats["total"] == 1
        assert stats["backlog"] == 1
        assert stats["unassigned"] == 1
        assert stats["by_reason"]["high_risk_intent"] == 1
        assert stats["avg_first_response_minutes"] is not None

    def test_stats_counts_overdue_backlog(self, tmp_path):
        """超过 24 小时未处理必须计入 overdue——值班最怕的就是被遗忘的单。"""
        svc = _service(tmp_path)
        old = datetime.now(timezone.utc) - timedelta(hours=30)
        svc.create(_ticket("no_knowledge", created_at=old, updated_at=old))
        assert svc.stats(days=7)["overdue"] == 1
