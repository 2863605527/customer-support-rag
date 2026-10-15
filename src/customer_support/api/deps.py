"""API 依赖注入：把整条客服链路装配成单例。

为什么用「无参 + lru_cache」而不是「带参 lru_cache」：

pydantic 的 ``BaseModel`` 默认不可哈希，``lru_cache`` 一旦收到 Settings 实参就会抛
``TypeError: unhashable type``。这里统一改成无参工厂，配置从 ``get_settings()`` 内部取，
既避免了哈希问题，也保证进程内只建一份连接（httpx 连接池、会话存储都是有状态的）。

装配清单（自下而上）：

1. **存储层**：``ConversationStore`` / ``TicketStore`` / ``FeedbackStore`` / ``AnswerCacheStore``
   共享同一个 SQLite 文件（``CONSOLE_DB_PATH``），各自只操作自己的表；
2. **业务层**：``TicketService``（工单门面）、``AnalyticsService``（指标聚合）；
3. **韧性层**：按配置设置 LLM 熔断阈值；
4. **鉴权**：``UserStore``（独立库 ``output/auth/users.db``）与 ``AuthService``。
"""
from __future__ import annotations

from functools import lru_cache

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from customer_support.agents.intent_agent import IntentAgent
from customer_support.agents.qa_agent import QAAgent
from customer_support.auth.service import AuthService
from customer_support.auth.store import UserStore
from customer_support.common.exceptions import AuthError, ForbiddenError, RateLimitedError
from customer_support.common.llm import create_llm
from customer_support.common.ratelimit import get_limiter
from customer_support.common.resilience import configure_breaker, get_llm_health
from customer_support.config import Settings, get_settings
from customer_support.i18n.translator import Translator
from customer_support.models.user import UserRecord
from customer_support.rag.base import RetrievalBackend
from customer_support.rag.catalog import CatalogIndex
from customer_support.rag.factory import build_retrieval_backend
from customer_support.rag.generator import AnswerGenerator
from customer_support.rag.retriever import Retriever
from customer_support.services.chat_service import ChatService
from customer_support.services.session import QueryRewriter, SessionStore
from customer_support.services.ticket import TicketService
from customer_support.store.analytics import AnalyticsService
from customer_support.store.cache import AnswerCacheStore
from customer_support.store.conversations import ConversationStore
from customer_support.store.feedback import FeedbackStore
from customer_support.store.tickets import TicketStore


@lru_cache
def get_config() -> Settings:
    """应用配置单例。"""
    settings = get_settings()
    settings.ensure_dirs()
    return settings


@lru_cache
def get_retrieval_backend() -> RetrievalBackend:
    """检索后端单例（RAGFlow 或 Milvus，由配置决定）。"""
    return build_retrieval_backend(get_config())


# ==================== 运营存储（共享一个 SQLite 文件） ====================

@lru_cache
def get_conversation_store() -> ConversationStore:
    """会话与消息存储单例。"""
    return ConversationStore(get_config().console.db_path)


@lru_cache
def get_ticket_store() -> TicketStore:
    """工单存储单例。"""
    return TicketStore(get_config().console.db_path)


@lru_cache
def get_feedback_store() -> FeedbackStore:
    """质量反馈存储单例。"""
    return FeedbackStore(get_config().console.db_path)


@lru_cache
def get_answer_cache() -> AnswerCacheStore | None:
    """问答缓存单例（配置关闭时返回 None，链路自动降级为"不缓存"）。"""
    settings = get_config()
    if not settings.console.cache_enabled:
        return None
    return AnswerCacheStore(settings.console.db_path)


@lru_cache
def get_ticket_service() -> TicketService:
    """工单服务单例。"""
    return TicketService(get_ticket_store())


@lru_cache
def get_analytics_service() -> AnalyticsService:
    """运营指标聚合单例。"""
    return AnalyticsService(
        get_conversation_store(),
        get_ticket_store(),
        get_feedback_store(),
        # 缓存可能被关闭：指标里仍需要缓存表读数，这里始终构造一个只读用的实例
        get_answer_cache() or AnswerCacheStore(get_config().console.db_path),
        get_llm_health(),
    )


@lru_cache
def get_session_store() -> SessionStore:
    """会话记忆单例。"""
    agent = get_config().agent
    return SessionStore(ttl_seconds=agent.session_ttl_seconds, max_turns=agent.session_history_turns)


@lru_cache
def get_catalog_index() -> CatalogIndex:
    """商品目录单例（货源链接 → 商品映射；文件缺失时优雅降级）。"""
    return CatalogIndex(get_config().knowledge.catalog_path)


@lru_cache
def get_chat_service() -> ChatService:
    """装配客服服务单例。"""
    settings = get_config()
    llm = create_llm(settings)

    # 熔断阈值来自配置：连续失败 N 次打开、冷却 M 秒后试探
    configure_breaker(
        "llm",
        failure_threshold=settings.llm.circuit_failure_threshold,
        recovery_timeout=settings.llm.circuit_recovery_seconds,
    )

    retriever = Retriever(
        get_retrieval_backend(),
        top_k=settings.retrieval.top_k,
        score_threshold=settings.retrieval.score_threshold,
        max_per_source=settings.retrieval.max_per_source,
    )
    generator = AnswerGenerator(retriever, llm=llm, shop_name=settings.agent.shop_name_ja)

    return ChatService(
        intent_agent=IntentAgent(llm=llm, confidence_threshold=settings.agent.confidence_threshold),
        qa_agent=QAAgent(generator),
        translator=Translator(llm=llm, default_language=settings.agent.default_language),
        default_language=settings.agent.default_language,
        confidence_threshold=settings.agent.confidence_threshold,
        session_store=get_session_store(),
        query_rewriter=QueryRewriter(llm),
        ticket_service=get_ticket_service(),
        catalog_index=get_catalog_index(),
        conversation_store=get_conversation_store(),
        answer_cache=get_answer_cache(),
        cache_ttl_seconds=settings.console.cache_ttl_seconds,
        cost_prompt_per_1k=settings.llm.cost_prompt_per_1k,
        cost_completion_per_1k=settings.llm.cost_completion_per_1k,
    )


def reset_singletons() -> None:
    """清空所有单例缓存（测试与配置热更新用）。"""
    for fn in (
        get_config,
        get_retrieval_backend,
        get_conversation_store,
        get_ticket_store,
        get_feedback_store,
        get_answer_cache,
        get_ticket_service,
        get_analytics_service,
        get_session_store,
        get_catalog_index,
        get_chat_service,
        get_user_store,
        get_auth_service,
    ):
        fn.cache_clear()


# ==================== 鉴权 ====================

# auto_error=False：未带令牌时由我们自己抛 401（带业务错误结构），
# 而不是让 FastAPI 抛它默认的 {"detail": "Not authenticated"}
_bearer_scheme = HTTPBearer(auto_error=False, description="Bearer <access_token>")


@lru_cache
def get_user_store() -> UserStore:
    """用户库单例（SQLite，持久化在 output/auth/users.db）。"""
    return UserStore(get_config().auth.users_db)


@lru_cache
def get_auth_service() -> AuthService:
    """鉴权服务单例。"""
    settings = get_config()
    service = AuthService(get_user_store(), settings.auth)
    service.ensure_seed_admin()
    return service


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    service: AuthService = Depends(get_auth_service),
) -> UserRecord:
    """校验 Bearer 令牌并返回当前用户（受保护接口的依赖）。"""
    if credentials is None or not credentials.credentials:
        raise AuthError("请先登录后再访问", detail="missing bearer token")
    return service.authenticate_access_token(credentials.credentials)


def require_admin(user: UserRecord = Depends(get_current_user)) -> UserRecord:
    """仅管理员可访问。"""
    if user.role != "admin":
        raise ForbiddenError("该操作需要管理员权限")
    return user


# ==================== 限流 ====================

def _client_ip(request: Request) -> str:
    """取客户端真实 IP（**默认不信任** ``X-Forwarded-For``）。

    ``X-Forwarded-For`` 是请求头，任何客户端都能自己写。登录限流是按 IP 建桶的，
    无条件采信这个头等于把「防撞库」交给攻击者——每次请求换一个伪造 IP 就能
    无限试密码（每个 IP 一把新桶，永远不超限）。

    因此默认取 TCP 对端地址；只有当**直连对端**在 ``TRUSTED_PROXIES`` 白名单里
    （说明前面确实是我们自己的 Nginx/网关，由它覆写这个头）才采信 XFF 的第一跳。
    反代部署时**必须**配置 ``TRUSTED_PROXIES``，否则所有访客会被算成同一个 IP。
    """
    peer = request.client.host if request.client else "unknown"
    trusted = get_config().trusted_proxies
    if not trusted:
        return peer
    allow_all = "*" in trusted
    if not (allow_all or peer in trusted):
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return peer


def enforce_rate_limit(request: Request, user: UserRecord, scope: str) -> None:
    """对已认证用户按 ``user:{id}`` 限流（换 IP 也限得住，防代理池）。"""
    settings = get_config()
    if not settings.ratelimit.enabled:
        return
    limiter = get_limiter(
        f"chat:{scope}",
        capacity=float(settings.ratelimit.chat_burst),
        per_minute=float(settings.ratelimit.chat_per_minute),
    )
    allowed, retry_after = limiter.check(f"user:{user.id}")
    if not allowed:
        raise RateLimitedError(
            "请求过于频繁，请稍后再试",
            detail=f"rate limit exceeded for {scope}",
            retry_after_seconds=retry_after,
            payload={"scope": scope},
        )


def chat_rate_limit(
    request: Request,
    user: UserRecord = Depends(get_current_user),
) -> UserRecord:
    """聊天接口限流依赖（同时返回当前用户，省一次鉴权解析）。"""
    enforce_rate_limit(request, user, "chat")
    return user


def login_rate_limit(request: Request) -> None:
    """登录接口按 IP 限流（防脚本撞库，比聊天更严格）。"""
    settings = get_config()
    if not settings.ratelimit.enabled:
        return
    limiter = get_limiter(
        "login",
        capacity=float(settings.ratelimit.login_burst),
        per_minute=float(settings.ratelimit.login_per_minute),
    )
    allowed, retry_after = limiter.check(f"ip:{_client_ip(request)}")
    if not allowed:
        raise RateLimitedError(
            "登录尝试过于频繁，请稍后再试",
            detail="login rate limit exceeded",
            retry_after_seconds=retry_after,
        )
