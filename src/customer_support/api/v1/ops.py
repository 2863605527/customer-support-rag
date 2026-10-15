"""运营控制台接口：工单闭环、会话复盘、质量反馈、运营指标、知识库运维。

为什么集中在一个模块：这些接口全部面向**内部使用者**（客服 / 值班主管 / 管理员），
共享同一套鉴权与分页约定；放在一起便于统一加权限（``require_admin``）与审计。
买家侧只有 ``POST /feedback`` 一个写入口。

路由声明顺序有讲究：``/ops/tickets/summary``、``/ops/tickets/stats`` 必须排在
``/ops/tickets/{ticket_id}`` **之前**，否则 ``summary`` 会被当成工单号吃掉。
"""
from __future__ import annotations

import threading
from datetime import date as date_type
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, status

from customer_support.api.deps import (
    get_analytics_service,
    get_answer_cache,
    get_config,
    get_conversation_store,
    get_feedback_store,
    get_retrieval_backend,
    get_session_store,
    get_ticket_service,
    require_admin,
)
from customer_support.common.exceptions import NotFoundError, ValidationError
from customer_support.common.logging import get_logger
from customer_support.common.resilience import all_breaker_snapshots
from customer_support.models.console import (
    ConversationDetail,
    ConversationPage,
    KnowledgeDocument,
    ReingestRequest,
    ReingestResult,
    ReplyRequest,
    SearchProbeHit,
    SearchProbeRequest,
    SearchProbeResult,
    TicketPage,
)
from customer_support.models.feedback import (
    DOWN_REASONS,
    UP_REASONS,
    FeedbackRecord,
    FeedbackStats,
    FeedbackSubmission,
)
from customer_support.models.ticket import Ticket, TicketReply, TicketUpdate
from customer_support.models.user import UserRecord
from customer_support.services.session import SessionStore
from customer_support.services.ticket import TicketService
from customer_support.store.analytics import AnalyticsService
from customer_support.store.conversations import ConversationStore
from customer_support.store.feedback import FeedbackStore

logger = get_logger(__name__)
router = APIRouter()


def _day_to_dt(day: date_type | None) -> datetime | None:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc) if day else None


# ======================================================================
# 工单闭环
# ======================================================================
@router.get("/ops/tickets", response_model=TicketPage, summary="工单列表（可筛选/分页）")
def list_tickets(
    status_: str | None = Query(None, alias="status", description="open/in_progress/resolved/closed"),
    priority: str | None = Query(None, description="low/normal/high/urgent"),
    assignee: str | None = Query(None, description="处理人邮箱"),
    reason: str | None = Query(None, description="转人工原因"),
    day: date_type | None = Query(None, description="创建日期（YYYY-MM-DD）"),
    search: str | None = Query(None, description="按问题或工单号模糊搜索"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    service: TicketService = Depends(get_ticket_service),
) -> TicketPage:
    items, total = service.query(
        status=status_, priority=priority, assignee=assignee, reason=reason,
        day=_day_to_dt(day), search=search, limit=limit, offset=offset,
    )
    return TicketPage(items=items, total=total, limit=limit, offset=offset)


@router.get("/ops/tickets/stats", summary="工单运营看板（积压 / 分布 / SLA 时长）")
def ticket_stats(
    days: int = Query(7, ge=1, le=90),
    service: TicketService = Depends(get_ticket_service),
) -> dict:
    return service.stats(days=days)


@router.get("/ops/tickets/summary", summary="转人工工单汇总（按原因 / 意图）")
def ticket_summary(
    day: date_type | None = Query(None, description="日期（YYYY-MM-DD），默认全部"),
    service: TicketService = Depends(get_ticket_service),
) -> dict:
    return service.summary(_day_to_dt(day))


@router.get("/ops/tickets/{ticket_id}", response_model=Ticket, summary="工单详情（含回复）")
def get_ticket(
    ticket_id: str,
    service: TicketService = Depends(get_ticket_service),
) -> Ticket:
    ticket = service.get(ticket_id)
    if ticket is None:
        raise NotFoundError("工单不存在", detail=f"ticket_id={ticket_id}")
    return ticket


@router.patch("/ops/tickets/{ticket_id}", response_model=Ticket, summary="更新工单（状态/优先级/指派）")
def update_ticket(
    ticket_id: str,
    payload: TicketUpdate,
    user: UserRecord = Depends(require_admin),
    service: TicketService = Depends(get_ticket_service),
) -> Ticket:
    ticket = service.update(
        ticket_id, status=payload.status, priority=payload.priority, assignee=payload.assignee
    )
    if ticket is None:
        raise NotFoundError("工单不存在", detail=f"ticket_id={ticket_id}")
    logger.info(
        "工单 %s 被 %s 更新为 status=%s priority=%s assignee=%s",
        ticket_id, user.email, ticket.status, ticket.priority, ticket.assignee,
    )
    return ticket


@router.post(
    "/ops/tickets/{ticket_id}/replies",
    response_model=Ticket,
    status_code=status.HTTP_201_CREATED,
    summary="新增人工回复 / 内部备注",
)
def add_reply(
    ticket_id: str,
    payload: ReplyRequest,
    user: UserRecord = Depends(require_admin),
    service: TicketService = Depends(get_ticket_service),
) -> Ticket:
    if service.get(ticket_id) is None:
        raise NotFoundError("工单不存在", detail=f"ticket_id={ticket_id}")
    ticket = service.add_reply(
        ticket_id,
        TicketReply(ticket_id=ticket_id, author=user.email, body=payload.body, internal=payload.internal),
    )
    assert ticket is not None
    return ticket


# ======================================================================
# 会话复盘
# ======================================================================
@router.get("/ops/conversations", response_model=ConversationPage, summary="会话列表（服务端历史）")
def list_conversations(
    search: str | None = Query(None, description="按 session_id 模糊搜索"),
    days: int | None = Query(None, ge=1, le=365, description="只看最近 N 天"),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    store: ConversationStore = Depends(get_conversation_store),
) -> ConversationPage:
    items, total = store.list_conversations(limit=limit, offset=offset, search=search, days=days)
    return ConversationPage(items=items, total=total, limit=limit, offset=offset)


@router.get(
    "/ops/conversations/{session_id}",
    response_model=ConversationDetail,
    summary="会话详情（完整消息与引用）",
)
def get_conversation(
    session_id: str,
    store: ConversationStore = Depends(get_conversation_store),
) -> ConversationDetail:
    detail = store.get_conversation(session_id)
    if detail is None:
        raise NotFoundError("会话不存在", detail=f"session_id={session_id}")
    return detail


@router.delete("/ops/conversations/{session_id}", summary="删除会话（合规：按请求删除买家数据）")
def delete_conversation(
    session_id: str,
    _: UserRecord = Depends(require_admin),
    store: ConversationStore = Depends(get_conversation_store),
) -> dict:
    store.delete(session_id)
    logger.info("会话 %s 已被管理员删除", session_id)
    return {"session_id": session_id, "deleted": True}


# ======================================================================
# 质量反馈
# ======================================================================
@router.post(
    "/feedback",
    response_model=FeedbackRecord,
    status_code=status.HTTP_201_CREATED,
    summary="提交回答评价（👍/👎 + 原因）",
)
def submit_feedback(payload: FeedbackSubmission) -> FeedbackRecord:
    if payload.rating == "down" and not payload.reasons:
        # 差评必须带原因：没有原因的差评无法转化为改进项
        raise ValidationError("差评请至少选择一个原因", detail="reasons is required when rating=down")
    return get_feedback_store().submit(payload)


@router.get("/feedback/reasons", summary="反馈原因标签（供前端渲染一键选择）")
def feedback_reasons() -> dict[str, dict[str, str]]:
    return {"up": UP_REASONS, "down": DOWN_REASONS}


@router.get("/ops/feedback/stats", response_model=FeedbackStats, summary="回答质量统计（好评率 / 差评原因）")
def feedback_stats(
    days: int = Query(30, ge=1, le=365),
    store: FeedbackStore = Depends(get_feedback_store),
) -> FeedbackStats:
    return store.stats(days=days)


@router.get("/ops/feedback/dataset", summary="导出回归评测集（JSONL 样本）")
def feedback_dataset(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(2000, ge=1, le=10000),
    _: UserRecord = Depends(require_admin),
    store: FeedbackStore = Depends(get_feedback_store),
) -> dict:
    samples = store.export_dataset(days=days, limit=limit)
    return {"total": len(samples), "samples": samples}


# ======================================================================
# 运营指标
# ======================================================================
@router.get("/ops/analytics", summary="运营总览（自助解决率 / 转人工 / 延迟 / 好评率 / 趋势）")
def analytics_overview(
    days: int = Query(7, ge=1, le=90),
    service: AnalyticsService = Depends(get_analytics_service),
) -> dict:
    return service.overview(days=days)


@router.get("/ops/sessions/stats", summary="会话记忆占用统计")
def session_stats(store: SessionStore = Depends(get_session_store)) -> dict:
    return store.stats()


@router.get("/ops/breakers", summary="熔断器状态（依赖健康）")
def breaker_states() -> list[dict]:
    return all_breaker_snapshots()


# ======================================================================
# 知识库运维
# ======================================================================
def _ragflow_ingestor():
    """构造 RAGFlow 客户端（仅当检索后端是 ragflow 时可用）。

    ⚠️ 调用方**必须**在 finally 里 ``close()``：这里每次都新建一个 httpx.Client，
    不关就会按请求数线性泄漏连接池与文件描述符（运维页多刷几次就能耗光）。
    """
    settings = get_config()
    if settings.retrieval.backend != "ragflow":
        raise ValidationError(
            "当前检索后端不是 RAGFlow，知识库运维接口不可用",
            detail=f"RETRIEVAL_BACKEND={settings.retrieval.backend}",
        )
    if not settings.ragflow.api_key:
        raise ValidationError("未配置 RAGFLOW_API_KEY", detail="ragflow api key missing")
    if not settings.ragflow.dataset_ids:
        raise ValidationError(
            "未配置 RAGFLOW_DATASET_IDS，无法确定要操作哪个知识库",
            detail="dataset_ids is empty",
        )
    from customer_support.knowledge.ragflow_ingest import RagflowIngestor

    return RagflowIngestor(
        settings.ragflow.base_url,
        settings.ragflow.api_key,
        timeout=settings.ragflow.timeout,
        max_retries=settings.ragflow.max_retries,
    )


@router.get(
    "/ops/knowledge/documents",
    response_model=list[KnowledgeDocument],
    summary="远端知识库文档清单（解析状态 / 分块数）",
)
def knowledge_documents(_: UserRecord = Depends(require_admin)) -> list[KnowledgeDocument]:
    settings = get_config()
    ingestor = _ragflow_ingestor()
    try:
        dataset_id = ingestor.get_or_create_dataset(settings.ragflow.dataset_ids[0])
        docs = ingestor.list_documents(dataset_id)
    finally:
        # 见 _ragflow_ingestor 的说明：不关就按请求数泄漏连接与 fd
        ingestor.close()
    return [
        KnowledgeDocument(
            id=str(d.get("id", "")),
            name=str(d.get("name", "")),
            size=int(d.get("size") or 0),
            chunk_count=int(d.get("chunk_count") or 0),
            run=str(d.get("run") or ""),
            status=str(d.get("status") or ""),
        )
        for d in docs
    ]


@router.post(
    "/ops/knowledge/search",
    response_model=SearchProbeResult,
    summary="在线检索测试（观察不同阈值下的命中，用于调参）",
)
def knowledge_search(
    payload: SearchProbeRequest,
    _: UserRecord = Depends(require_admin),
) -> SearchProbeResult:
    settings = get_config()
    from customer_support.rag.retriever import Retriever

    retriever = Retriever(
        get_retrieval_backend(),
        top_k=payload.top_k,
        score_threshold=payload.score_threshold,
        max_per_source=max(payload.top_k, 4),
    )
    hits = retriever.retrieve(payload.query)
    online = settings.retrieval.score_threshold
    return SearchProbeResult(
        query=payload.query,
        hits=[
            SearchProbeHit(
                source=h.source, doc_id=h.doc_id, score=round(h.score, 4), text=h.text[:220]
            )
            for h in hits
        ],
        total=len(hits),
        threshold_passed=sum(1 for h in hits if h.score >= online),
    )


def _invalidate_answer_cache(report: object) -> int:
    """知识库内容变更后作废问答缓存，返回作废条数。

    缓存键是「问题文本 + 语言」的哈希，**不含知识库版本**——不清理的话，改完文档后
    一小时内（``CONSOLE_CACHE_TTL_SECONDS``）老问题仍会命中改之前的旧答案，表现为
    「明明更新了知识库，客服还是答旧的」，最容易被误判成"检索没生效"。

    只在上传/删除确实发生时清理（纯跳过的一轮不动缓存，避免白白把热缓存清空）。
    """
    if not (getattr(report, "uploaded", None) or getattr(report, "deleted", None)):
        return 0
    cache = get_answer_cache()
    if cache is None:
        return 0
    return cache.clear()


@router.post(
    "/ops/knowledge/reingest",
    response_model=ReingestResult,
    status_code=status.HTTP_202_ACCEPTED,
    summary="触发知识库重新入库（后台执行，立即返回）",
)
def knowledge_reingest(
    payload: ReingestRequest,
    _: UserRecord = Depends(require_admin),
) -> ReingestResult:
    """上传与解析可能耗时数分钟，接口**立即返回 202**，实际任务在后台线程执行。

    这样做的理由：把长任务塞进请求-响应周期会导致网关超时、前端转圈、重试重复触发；
    正确做法是接受任务 + 后台执行 + 用文档清单接口观察进度。
    """
    _ragflow_ingestor().close()  # 提前校验后端与密钥；配置不对立刻报错而不是静默失败
    settings = get_config()

    def _run() -> None:
        from customer_support.knowledge.ragflow_ingest import ingest_knowledge_base

        try:
            report = ingest_knowledge_base(
                settings, replace=payload.replace, prune=payload.prune, wait=payload.wait
            )
            logger.info(
                "后台重新入库完成：上传 %d / 跳过 %d / 删除 %d / 失败 %d",
                len(report.uploaded), len(report.skipped), len(report.deleted), len(report.failed),
            )
            # 知识库内容变了，答案缓存必须一起失效（见 _invalidate_answer_cache）
            purged = _invalidate_answer_cache(report)
            if purged:
                logger.info("知识库已变更，作废问答缓存 %d 条", purged)
        except Exception as exc:  # noqa: BLE001 - 后台任务异常只记日志
            logger.exception("后台重新入库失败: %s", exc)

    threading.Thread(target=_run, name="knowledge-reingest", daemon=True).start()
    return ReingestResult(
        accepted=True,
        message="重新入库已在后台开始，请稍后用文档清单接口查看解析进度",
    )
