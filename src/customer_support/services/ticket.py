"""工单服务：转人工工单的创建与全生命周期管理。

这是 ``store.TicketStore`` 之上的一层**业务门面**，只做两件存储层不该管的事：

1. **生成工单号**（``T + 日期 + 随机码``，人工口述也不易记错）；
2. **按转人工原因补默认优先级**——欠费/系统故障属于"全店受影响"，必须 urgent；
   高风险纠纷次之；低置信度与无资料属于常规队列。默认优先级可由调用方显式覆盖。

存储本身（建表、筛选、状态流转、SLA 统计）在 ``store/tickets.py``，
换数据库只需替换存储层实现，本门面接口不变。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from customer_support.common.logging import get_logger
from customer_support.models.ticket import (
    DEFAULT_PRIORITY,
    Ticket,
    TicketReply,
)
from customer_support.store.tickets import TicketStore

logger = get_logger(__name__)


class TicketService:
    """转人工工单的业务门面。"""

    def __init__(self, store: TicketStore):
        self.store = store

    @staticmethod
    def new_ticket_id(now: datetime | None = None) -> str:
        """工单号：T + 日期 + 随机短码，人工口述也不会记错。"""
        now = now or datetime.now(timezone.utc)
        return f"T{now:%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"

    def create(self, ticket: Ticket) -> Ticket:
        """落库一条工单。未显式指定优先级时，按转人工原因取默认值。"""
        if ticket.priority == "normal" and ticket.reason in DEFAULT_PRIORITY:
            ticket = ticket.model_copy(update={"priority": DEFAULT_PRIORITY[ticket.reason]})
        return self.store.create(ticket)

    # ---------------- 查询 ----------------
    def get(self, ticket_id: str) -> Ticket | None:
        return self.store.get(ticket_id)

    def query(self, **kwargs: object) -> tuple[list[Ticket], int]:
        return self.store.query(**kwargs)  # type: ignore[arg-type]

    def list_tickets(self, day: datetime | None = None) -> list[Ticket]:
        """兼容旧接口：某天的工单（默认全部）。"""
        return self.store.list_tickets(day)

    def stats(self, *, days: int = 7) -> dict:
        return self.store.stats(days=days)

    def summary(self, day: datetime | None = None) -> dict:
        return self.store.summary(day)

    # ---------------- 变更 ----------------
    def update(self, ticket_id: str, **fields: object) -> Ticket | None:
        return self.store.update(ticket_id, **fields)

    def add_reply(self, ticket_id: str, reply: TicketReply) -> Ticket | None:
        return self.store.add_reply(ticket_id, reply)
