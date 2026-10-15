"""运营数据存储层（SQLite）。

把「可运营」需要的数据落到一张本地库里，与鉴权的 ``output/auth/users.db`` 分开：

- ``conversations`` / ``messages``  服务端会话与消息（跨设备、可复盘、可分析）；
- ``tickets`` / ``ticket_replies``  转人工工单的全生命周期（状态流转、指派、人工回复）；
- ``feedback``                      回答质量反馈（👍/👎 + 原因），沉淀为评测集；
- ``answer_cache``                  精确问答缓存（同问题秒回，省 LLM 成本）。

为什么是 SQLite 而不是继续用 JSONL / 进程内字典：

- 工单要**改状态、要筛选、要排序**，JSONL 只能追加不能更新；
- 会话要按时间倒序分页、要 join 统计，内存字典进程重启即丢；
- 表结构与 SQL 均为标准 SQL，将来换 Postgres 只需替换连接层。

并发策略沿用 ``auth/store.py``：短连接 + 进程内写锁 + WAL，避免 "database is locked"。
"""
from __future__ import annotations

from customer_support.store.analytics import AnalyticsService
from customer_support.store.base import BaseStore
from customer_support.store.cache import AnswerCacheStore
from customer_support.store.conversations import ConversationStore
from customer_support.store.feedback import FeedbackStore
from customer_support.store.tickets import TicketStore

__all__ = [
    "AnalyticsService",
    "AnswerCacheStore",
    "BaseStore",
    "ConversationStore",
    "FeedbackStore",
    "TicketStore",
]
