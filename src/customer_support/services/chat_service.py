"""客服编排服务：一次咨询的完整处理流程（业务规则唯一集中地）。

处理流程：

1. 生成 ``trace_id``，整条链路（意图 / 检索 / 生成 / 工单）都用它串起来；
2. 语言：买家显式指定优先，否则 LLM 识别，失败回退店铺默认语言（日本站=日语）；
3. 缓存：**无会话上下文**的重复问题（FAQ 型）直接复用历史回答，省一次完整链路；
4. 会话：取历史 → 把带指代的追问改写成自包含检索语句；
5. 商品链接识别：消息里带货源链接/商品番号时，按文档名**确定性锚定**到对应商品文档
   （不指望相似度一定排得上），并把商品名注入检索词顺带召回关联文档；
6. 意图识别（IntentAgent）；
7. 判定转人工（安全优先）：
   - ``high_risk_intent``：命中高风险类别（退款/投诉/纠纷），客服不能自动承诺；
   - ``low_confidence``：意图置信度低于阈值，宁可转人工也不猜；
   - ``no_knowledge``：RAG 没有拿到足够相关的资料，不编造；
   - ``service_unavailable``：LLM/检索熔断打开（依赖已不可用，快速失败）；
   - ``system_error``：其它未预期异常；
8. 正常路径：RAG 生成回答，带可溯源引用；
9. **落库**：会话与消息写入 ``ConversationStore``（跨设备、可复盘、可统计）。

「异常降级」与「答案降级」的区别：模型挂了属于前者，走 ``service_unavailable`` /
``system_error``；知识库没有资料属于后者，走 ``no_knowledge``。两者都转人工，
但原因不同、复盘口径不同——这个区分直接决定了"该去修依赖还是该去补知识库"。
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Iterator

from customer_support.agents.intent_agent import IntentAgent
from customer_support.agents.qa_agent import QAAgent
from customer_support.common.logging import get_logger
from customer_support.common.resilience import CircuitOpenError, is_dependency_unavailable
from customer_support.i18n.translator import Translator
from customer_support.models.intent import IntentResult
from customer_support.models.message import ChatMessage, ChatResult, Reference
from customer_support.models.ticket import Ticket
from customer_support.rag.catalog import CatalogIndex, CatalogProduct
from customer_support.services.session import QueryRewriter, SessionStore, Turn
from customer_support.services.ticket import TicketService
from customer_support.store.cache import AnswerCacheStore, cache_key
from customer_support.store.conversations import ConversationStore

logger = get_logger(__name__)

# 转人工话术（默认日语；按语言扩展）
HUMAN_TRANSFER_ANSWERS: dict[str, str] = {
    "ja": (
        "大変申し訳ございません。こちらの件は担当者が確認する必要がございます。"
        "ただいま担当者におつなぎいたしますので、少々お待ちください。"
    ),
    "en": (
        "I'm sorry, but this needs a member of our support team to help you properly. "
        "I'm connecting you with a human agent right now."
    ),
    "zh": "非常抱歉，这个问题需要人工客服为您确认，我现在帮您转接，请稍等。",
    "default": (
        "I'm sorry, but this needs a member of our support team to help you properly. "
        "I'm connecting you with a human agent right now."
    ),
}

# 依赖不可用时的话术：与「资料不足」区分开——买家能感知到这是系统原因而非"查不到"
UNAVAILABLE_ANSWERS: dict[str, str] = {
    "ja": (
        "申し訳ございません。ただいまシステムが混み合っております。"
        "担当者が確認のうえ、あらためてご案内いたします。少々お待ちください。"
    ),
    "en": (
        "I'm sorry, our assistant is temporarily unavailable. "
        "I'm passing this to a human agent who will follow up with you shortly."
    ),
    "zh": "非常抱歉，智能客服暂时不可用。已为您转接人工客服跟进，请稍等。",
    "default": (
        "I'm sorry, our assistant is temporarily unavailable. "
        "I'm passing this to a human agent who will follow up with you shortly."
    ),
}

# 缓存命中时把答案分块"重放"，让前端仍有逐步出现的观感（无需真的调模型）
_CACHE_CHUNK = 24


class ChatService:
    """客服编排服务。"""

    def __init__(
        self,
        intent_agent: IntentAgent,
        qa_agent: QAAgent,
        translator: Translator,
        *,
        default_language: str = "ja",
        confidence_threshold: float = 0.5,
        session_store: SessionStore | None = None,
        query_rewriter: QueryRewriter | None = None,
        ticket_service: TicketService | None = None,
        catalog_index: CatalogIndex | None = None,
        conversation_store: ConversationStore | None = None,
        answer_cache: AnswerCacheStore | None = None,
        cache_ttl_seconds: int = 3600,
        cost_prompt_per_1k: float = 0.0,
        cost_completion_per_1k: float = 0.0,
    ):
        self.intent_agent = intent_agent
        self.qa_agent = qa_agent
        self.translator = translator
        self.default_language = default_language
        self.confidence_threshold = confidence_threshold
        self.session_store = session_store or SessionStore()
        self.query_rewriter = query_rewriter
        self.ticket_service = ticket_service
        self.catalog_index = catalog_index
        self.conversation_store = conversation_store
        self.answer_cache = answer_cache
        self.cache_ttl_seconds = cache_ttl_seconds
        self.cost_prompt_per_1k = cost_prompt_per_1k
        self.cost_completion_per_1k = cost_completion_per_1k

    # ------------------------------------------------------------------
    # 主流程（整段返回）
    # ------------------------------------------------------------------
    def handle(self, message: ChatMessage) -> ChatResult:
        """处理一条买家咨询，返回结构化结果。"""
        started = time.perf_counter()
        trace_id = uuid.uuid4().hex[:12]
        question = message.question.strip()
        logger.info("[%s] 收到咨询 (session=%s): %s", trace_id, message.session_id, question[:80])

        try:
            return self._handle_inner(message, question, trace_id, started)
        except CircuitOpenError as exc:
            return self._degrade(
                message, question, trace_id, started, reason="service_unavailable", exc=exc
            )
        except Exception as exc:  # noqa: BLE001 - 兜底：任何异常都不能让买家收到失败
            # 「依赖不可用」与「我们自己的代码异常」要分开归因：
            # 前者运维看到 service_unavailable 就该去找上游，后者才需要看代码。
            reason = "service_unavailable" if is_dependency_unavailable(exc) else "system_error"
            return self._degrade(message, question, trace_id, started, reason=reason, exc=exc)

    def _handle_inner(
        self, message: ChatMessage, question: str, trace_id: str, started: float
    ) -> ChatResult:
        stage: dict[str, int] = {}
        language = self._detect_language(message, question, stage)
        history = self.session_store.get(message.session_id)

        cached = self._try_cache(message, question, language, trace_id, stage)
        if cached is not None:
            return cached

        search_query = self._rewrite(question, history, stage)
        product = self._match_product(question)
        search_query = self._augment_query(search_query, product)

        intent = self._classify(search_query, stage)

        # 转人工判定（高风险优先于低置信度，便于复盘归因）
        if intent.is_high_risk:
            return self._transfer(
                message=message, question=question, trace_id=trace_id, started=started,
                language=language, intent=intent.intent.value, confidence=intent.confidence,
                reason="high_risk_intent", sources=[], stage=stage,
            )
        if intent.needs_human(self.confidence_threshold):
            return self._transfer(
                message=message, question=question, trace_id=trace_id, started=started,
                language=language, intent=intent.intent.value, confidence=intent.confidence,
                reason="low_confidence", sources=[], stage=stage,
            )

        # 正常路径：RAG 回答（贴了商品链接时按文档名确定性锚定该商品文档）
        t0 = time.perf_counter()
        qa_result = self.qa_agent.run(
            {
                "question": question,
                "rewritten_question": search_query,
                "language": language,
                "pin_source": self._pin_source(product),
            }
        )
        stage["retrieval_generate"] = _ms(t0)
        references = qa_result.get("references") or []
        source = qa_result.get("source", "unknown")

        if qa_result.get("need_human"):
            return self._transfer(
                message=message, question=question, trace_id=trace_id, started=started,
                language=language, intent=intent.intent.value, confidence=intent.confidence,
                reason="no_knowledge",
                sources=[r.source for r in references],
                answer=qa_result.get("answer") or self._human_answer(language),
                stage=stage,
            )

        usage = qa_result.get("usage") or {}
        result = self._build_result(
            message=message, question=question, trace_id=trace_id, started=started,
            language=language, intent=intent, answer=qa_result.get("answer", ""),
            source=source, references=references, need_human=False, human_reason="",
            stage=stage, prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
        )
        self._after_answer(message, question, result, history=history)
        return result

    # ------------------------------------------------------------------
    # 主流程（SSE 流式）
    # ------------------------------------------------------------------
    def handle_stream(self, message: ChatMessage) -> Iterator[dict[str, object]]:
        """流式版 ``handle``：产出 SSE 事件序列。

        事件（dict，由 API 层编码为 SSE 帧）：
        - ``{"type": "status", "stage": str}``：链路阶段（语言/改写/意图/检索/生成/缓存），
          前端展示为「思考过程」；
        - ``{"type": "delta", "text": str}``：回答增量，逐字渲染；
        - ``{"type": "replace", "text": str}``：资料不足时用兜底话术**整段替换**已流出内容；
        - ``{"type": "done", "result": ChatResult}``：终态结构化结果（必发，最后一条），
          即使中途异常也保证发出（降级为转人工），与 ``handle`` 的兜底承诺一致。
        """
        started = time.perf_counter()
        trace_id = uuid.uuid4().hex[:12]
        question = message.question.strip()
        logger.info("[%s] 收到咨询(流式) (session=%s): %s", trace_id, message.session_id, question[:80])

        stage: dict[str, int] = {}
        try:
            yield {"type": "status", "stage": "detect_language"}
            language = self._detect_language(message, question, stage)
            history = self.session_store.get(message.session_id)

            # 缓存命中：直接"重放"历史回答（不调模型），并给出明确的阶段标识
            if self._cache_eligible(message, history):
                cached = self._lookup_cache(question, language, trace_id)
                if cached is not None:
                    cached = cached.model_copy(update={"session_id": message.session_id})
                    yield {"type": "status", "stage": "cache_hit"}
                    for i in range(0, len(cached.answer), _CACHE_CHUNK):
                        yield {"type": "delta", "text": cached.answer[i : i + _CACHE_CHUNK]}
                    self._after_answer(message, question, cached, history=history)
                    yield {"type": "done", "result": cached}
                    return

            yield {"type": "status", "stage": "rewrite"}
            search_query = self._rewrite(question, history, stage)

            product = self._match_product(question)

            yield {"type": "status", "stage": "intent"}
            intent = self._classify(search_query, stage)

            if intent.is_high_risk or intent.needs_human(self.confidence_threshold):
                reason = "high_risk_intent" if intent.is_high_risk else "low_confidence"
                result = self._transfer(
                    message=message, question=question, trace_id=trace_id, started=started,
                    language=language, intent=intent.intent.value, confidence=intent.confidence,
                    reason=reason, sources=[], stage=stage,
                )
                yield {"type": "done", "result": result}
                return

            # 正常路径：RAG 流式回答
            retrieval_event: dict[str, object] = {"type": "status", "stage": "retrieval"}
            if product is not None:
                retrieval_event["detail"] = {"matched_product": product.name_ja}
            yield retrieval_event
            search_query = self._augment_query(search_query, product)

            t0 = time.perf_counter()
            streamed: list[str] = []
            usage: dict[str, int] = {}
            for kind, payload in self.qa_agent.run_stream(
                {
                    "question": question,
                    "rewritten_question": search_query,
                    "language": language,
                    "pin_source": self._pin_source(product),
                }
            ):
                if kind == "status":  # 检索完成 → 进入生成（含命中条数）
                    yield {"type": "status", **payload}
                    continue
                if kind == "delta":
                    streamed.append(str(payload))
                    yield {"type": "delta", "text": str(payload)}
                    continue

                qa_result = payload  # dict：与 run() 返回结构一致
                references = qa_result.get("references") or []
                source = qa_result.get("source", "unknown")
                usage = qa_result.get("usage") or {}
                stage["retrieval_generate"] = _ms(t0)

                if qa_result.get("need_human"):
                    # 资料不足：已流出的内容（可能是哨兵或残句）整段替换为兜底话术
                    yield {"type": "replace", "text": qa_result.get("answer", "")}
                    result = self._transfer(
                        message=message, question=question, trace_id=trace_id, started=started,
                        language=language, intent=intent.intent.value, confidence=intent.confidence,
                        reason="no_knowledge",
                        sources=[r.source for r in references],
                        answer=qa_result.get("answer") or self._human_answer(language),
                        stage=stage,
                    )
                    yield {"type": "done", "result": result}
                    return

                result = self._build_result(
                    message=message, question=question, trace_id=trace_id, started=started,
                    language=language, intent=intent, answer=qa_result.get("answer", ""),
                    source=source, references=references, need_human=False, human_reason="",
                    stage=stage, prompt_tokens=int(usage.get("prompt_tokens", 0)),
                    completion_tokens=int(usage.get("completion_tokens", 0)),
                )
                self._after_answer(message, question, result, history=history)
                yield {"type": "done", "result": result}
                return

        except CircuitOpenError as exc:
            result = self._degrade(
                message, question, trace_id, started, reason="service_unavailable", exc=exc, stage=stage
            )
            yield {"type": "done", "result": result}
        except Exception as exc:  # noqa: BLE001 - 与 handle 同款兜底：绝不让买家收到失败
            reason = "service_unavailable" if is_dependency_unavailable(exc) else "system_error"
            result = self._degrade(
                message, question, trace_id, started, reason=reason, exc=exc, stage=stage
            )
            yield {"type": "done", "result": result}

    # ------------------------------------------------------------------
    # 分步：语言 / 改写 / 缓存 / 意图
    # ------------------------------------------------------------------
    def _detect_language(self, message: ChatMessage, question: str, stage: dict[str, int]) -> str:
        t0 = time.perf_counter()
        language = message.language or self.translator.detect(question) or self.default_language
        stage["language"] = _ms(t0)
        return language

    def _rewrite(self, question: str, history: list[Turn], stage: dict[str, int]) -> str:
        t0 = time.perf_counter()
        search_query = question
        if history and self.query_rewriter:
            search_query = self.query_rewriter.rewrite(question, history)
        stage["rewrite"] = _ms(t0)
        return search_query

    def _classify(self, search_query: str, stage: dict[str, int]) -> IntentResult:
        t0 = time.perf_counter()
        intent = self.intent_agent.classify(search_query)
        stage["intent"] = _ms(t0)
        return intent

    def _cache_eligible(self, message: ChatMessage, history: list[Turn]) -> bool:
        """只有「无上下文的首轮问题」才允许命中缓存（多轮答案依赖上文，缓存会答错）。"""
        return (
            self.answer_cache is not None
            and message.session_id is not None
            and not history
        )

    def _lookup_cache(self, question: str, language: str, trace_id: str) -> ChatResult | None:
        assert self.answer_cache is not None
        result = self.answer_cache.get(cache_key(question, language))
        if result is None:
            return None
        logger.info("[%s] 命中问答缓存（language=%s）: %s", trace_id, language, question[:60])
        return result.model_copy(update={"trace_id": trace_id, "question": question})

    def _try_cache(
        self,
        message: ChatMessage,
        question: str,
        language: str,
        trace_id: str,
        stage: dict[str, int],
    ) -> ChatResult | None:
        history = self.session_store.get(message.session_id)
        if not self._cache_eligible(message, history):
            return None
        cached = self._lookup_cache(question, language, trace_id)
        if cached is None:
            return None
        cached = cached.model_copy(update={"session_id": message.session_id, "stage_ms": stage})
        self._after_answer(message, question, cached, history=history)
        return cached

    def _store_cache(self, question: str, language: str, result: ChatResult) -> None:
        """只缓存「有知识库依据且未转人工」的回答。"""
        if self.answer_cache is None or result.need_human or result.source != "knowledge":
            return
        self.answer_cache.put(
            cache_key(question, language), question, language, result,
            ttl_seconds=self.cache_ttl_seconds,
        )

    # ------------------------------------------------------------------
    # 结果构造 / 落库
    # ------------------------------------------------------------------
    def _build_result(
        self,
        *,
        message: ChatMessage,
        question: str,
        trace_id: str,
        started: float,
        language: str,
        intent: IntentResult,
        answer: str,
        source: str,
        references: list[Reference],
        need_human: bool,
        human_reason: str,
        stage: dict[str, int],
        ticket_id: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> ChatResult:
        return ChatResult(
            question=question,
            answer=answer,
            intent=intent.intent.value,
            need_human=need_human,
            human_reason=human_reason,
            language=language,
            source=source,
            confidence=intent.confidence,
            references=references,
            ticket_id=ticket_id,
            session_id=message.session_id,
            trace_id=trace_id,
            latency_ms=_ms(started),
            stage_ms=dict(stage),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_cost=self._estimate_cost(prompt_tokens, completion_tokens),
        )

    def _estimate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        if not (self.cost_prompt_per_1k or self.cost_completion_per_1k):
            return 0.0
        return round(
            prompt_tokens / 1000 * self.cost_prompt_per_1k
            + completion_tokens / 1000 * self.cost_completion_per_1k,
            6,
        )

    def _after_answer(
        self,
        message: ChatMessage,
        question: str,
        result: ChatResult,
        *,
        history: list[Turn] | None = None,
    ) -> None:
        """回答产出后的统一后置：会话记忆 + 落库 + 缓存写入 + 观测日志。"""
        self._remember(message.session_id, question, result)
        if self.conversation_store is not None and message.session_id:
            try:
                self.conversation_store.record_turn(
                    session_id=message.session_id,
                    question=question,
                    result=result,
                    buyer_id=message.buyer_id,
                )
            except Exception as exc:  # noqa: BLE001 - 落库失败不能影响买家拿到回答
                logger.warning("[%s] 会话落库失败: %s", result.trace_id, exc)
        # 只有首轮（无上下文）的回答才写缓存，与读取侧口径保持一致
        if not history:
            self._store_cache(question, result.language, result)
        logger.info(
            "[%s] 回答完成 source=%s 引用=%d 耗时=%dms 阶段=%s tokens=%d/%d 成本=%.6f",
            result.trace_id, result.source, len(result.references), result.latency_ms,
            result.stage_ms, result.prompt_tokens, result.completion_tokens,
            result.estimated_cost,
        )

    # ------------------------------------------------------------------
    # 转人工 / 降级
    # ------------------------------------------------------------------
    def _human_answer(self, language: str) -> str:
        return HUMAN_TRANSFER_ANSWERS.get(language, HUMAN_TRANSFER_ANSWERS["default"])

    def _unavailable_answer(self, language: str) -> str:
        return UNAVAILABLE_ANSWERS.get(language, UNAVAILABLE_ANSWERS["default"])

    def _degrade(
        self,
        message: ChatMessage,
        question: str,
        trace_id: str,
        started: float,
        *,
        reason: str,
        exc: BaseException,
        stage: dict[str, int] | None = None,
    ) -> ChatResult:
        """依赖/系统异常的统一降级：记录日志 + 建工单 + 明确话术。"""
        logger.exception("[%s] 处理异常(%s)，降级转人工: %s", trace_id, reason, exc)
        return self._transfer(
            message=message, question=question, trace_id=trace_id, started=started,
            language=message.language or self.default_language,
            intent="unknown", confidence=0.0, reason=reason, sources=[],
            detail=f"{type(exc).__name__}: {exc}", stage=stage or {},
            unavailable=(reason == "service_unavailable"),
        )

    def _transfer(
        self,
        *,
        message: ChatMessage,
        question: str,
        trace_id: str,
        started: float,
        language: str,
        intent: str,
        confidence: float,
        reason: str,
        sources: list[str],
        answer: str | None = None,
        detail: str = "",
        stage: dict[str, int] | None = None,
        unavailable: bool = False,
    ) -> ChatResult:
        """统一走转人工：出话术 + 建工单 + 记会话。"""
        final_answer = answer or (
            self._unavailable_answer(language) if unavailable else self._human_answer(language)
        )
        logger.info(
            "[%s] 转人工 reason=%s intent=%s confidence=%.2f %s",
            trace_id, reason, intent, confidence, detail,
        )

        ticket_id: str | None = None
        if self.ticket_service is not None:
            # 建单必须自吞异常：工单是**内部流程**，写库失败不该让买家收到失败。
            # 这里不兜住的话，异常会穿透 handle()/handle_stream() 的兜底——
            # 因为兜底本身也是走 _transfer，再抛一次就真的变成 500 了
            # （「买家永远不会收到 500」这条承诺，靠这里守住）。
            try:
                ticket = self.ticket_service.create(
                    Ticket(
                        ticket_id=TicketService.new_ticket_id(),
                        question=question,
                        language=language,
                        intent=intent,
                        confidence=confidence,
                        reason=reason,
                        asked_answer=final_answer,
                        buyer_id=message.buyer_id,
                        session_id=message.session_id,
                        trace_id=trace_id,
                        sources=sources,
                    )
                )
                ticket_id = ticket.ticket_id
            except Exception as exc:  # noqa: BLE001 - 建单失败不影响买家拿到话术
                logger.error("[%s] 创建工单失败，仍返回转人工话术: %s", trace_id, exc)

        result = ChatResult(
            question=question,
            answer=final_answer,
            intent=intent,
            need_human=True,
            human_reason=reason,
            language=language,
            source="human",
            confidence=confidence,
            references=[],
            ticket_id=ticket_id,
            session_id=message.session_id,
            trace_id=trace_id,
            latency_ms=_ms(started),
            stage_ms=dict(stage or {}),
        )
        self._remember(message.session_id, question, result)
        if self.conversation_store is not None and message.session_id:
            try:
                self.conversation_store.record_turn(
                    session_id=message.session_id,
                    question=question,
                    result=result,
                    buyer_id=message.buyer_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] 会话落库失败: %s", trace_id, exc)
        return result

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _match_product(self, question: str) -> CatalogProduct | None:
        """识别消息里的货源链接/商品番号（未装配目录或未命中时返回 None）。"""
        if self.catalog_index is None:
            return None
        product = self.catalog_index.match(question)
        if product is not None:
            logger.info("识别到商品链接：%s（%s）", product.sku, product.name_ja)
        return product

    def _augment_query(self, search_query: str, product: CatalogProduct | None) -> str:
        """把命中的商品名注入检索词（放在最前，权重最高）。"""
        if product is None or self.catalog_index is None:
            return search_query
        return f"{self.catalog_index.augment_query(product)} {search_query}"

    @staticmethod
    def _pin_source(product: CatalogProduct | None) -> str | None:
        """商品链接命中时给出要**确定性锚定**的知识库文档名（未命中为 None）。

        检索词注入（``_augment_query``）解决的是「让相似度更容易排到该商品」，
        但不保证排得到；锚定则保证该商品文档一定进参考资料。两者叠加：锚定
        保正确性，注入帮助顺带召回关联文档（如尺寸指南、政策说明）。
        """
        return product.doc_name if product is not None else None

    def _remember(self, session_id: str | None, question: str, result: ChatResult) -> None:
        self.session_store.append(
            session_id,
            Turn(
                question=question,
                answer=result.answer,
                intent=result.intent,
                need_human=result.need_human,
            ),
        )


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
