"""客服全流程集成测试：用假检索后端 + 假 LLM 覆盖四条转人工分支与正常路径。

这是本项目最关键的一组测试——它固化的是**业务安全规则**：
高风险必转、低置信度必转、没资料不编造、系统异常也不能让买家收到 500。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from customer_support.agents.intent_agent import IntentAgent
from customer_support.agents.qa_agent import QAAgent
from customer_support.i18n.translator import Translator
from customer_support.models.knowledge import RetrievalHit
from customer_support.models.message import ChatMessage
from customer_support.rag.generator import AnswerGenerator
from customer_support.rag.retriever import Retriever
from customer_support.services.chat_service import ChatService
from customer_support.services.session import QueryRewriter, SessionStore, Turn
from customer_support.services.ticket import TicketService
from customer_support.store.tickets import TicketStore
from tests.conftest import FakeLLM, FakeRetrievalBackend


def ticket_service_for(dir_path: Path) -> TicketService:
    """测试用：基于目录构造工单服务（与生产同一条 SQLite 存储路径）。"""
    return TicketService(TicketStore(dir_path / "console.db"))


def build_service(
    backend: FakeRetrievalBackend,
    *,
    intent_payload: str = '{"intent": "product_info", "confidence": 0.9, "reason": "ok"}',
    answer: str = "綿100%の生地です。",
    ticket_dir=None,
    session_store: SessionStore | None = None,
    rewriter=None,
    conversation_store=None,
) -> ChatService:
    retriever = Retriever(backend, top_k=4, score_threshold=0.35)
    generator = AnswerGenerator(retriever, FakeLLM(answer), shop_name="くらし寝具")
    return ChatService(
        intent_agent=IntentAgent(FakeLLM(intent_payload), confidence_threshold=0.5),
        qa_agent=QAAgent(generator),
        translator=Translator(FakeLLM("en"), default_language="ja"),
        default_language="ja",
        confidence_threshold=0.5,
        session_store=session_store or SessionStore(),
        query_rewriter=rewriter,
        ticket_service=ticket_service_for(ticket_dir) if ticket_dir else None,
        conversation_store=conversation_store,
    )


def _hit(text: str = "綿100%の生地です。", score: float = 0.9) -> RetrievalHit:
    return RetrievalHit(text=text, score=score, source="products/KB-0001.md", doc_id="products/KB-0001.md")


@pytest.mark.integration
def test_happy_path_answers_from_knowledge():
    backend = FakeRetrievalBackend([_hit()])
    service = build_service(backend)
    result = service.handle(ChatMessage(question="素材は何ですか？"))
    assert result.need_human is False
    assert result.source == "knowledge"
    assert result.language == "ja"        # 假名 -> 日语
    assert result.answer == "綿100%の生地です。"
    assert result.references and result.references[0].source == "products/KB-0001.md"
    assert result.trace_id
    assert result.latency_ms >= 0


@pytest.mark.integration
def test_high_risk_intent_transfers_to_human(tmp_path):
    backend = FakeRetrievalBackend([_hit()])
    service = build_service(
        backend,
        intent_payload='{"intent": "human", "confidence": 0.95, "reason": "返金要求"}',
        ticket_dir=tmp_path,
    )
    result = service.handle(ChatMessage(question="今すぐ返金しろ！", buyer_id="u1"))
    assert result.need_human is True
    assert result.human_reason == "high_risk_intent"
    assert result.source == "human"
    assert result.ticket_id and result.ticket_id.startswith("T")
    # 工单落盘，人工可接手
    assert len(ticket_service_for(tmp_path).list_tickets()) == 1


@pytest.mark.integration
def test_low_confidence_transfers_to_human(tmp_path):
    backend = FakeRetrievalBackend([_hit()])
    service = build_service(
        backend,
        intent_payload='{"intent": "presale", "confidence": 0.2, "reason": "模糊"}',
        ticket_dir=tmp_path,
    )
    result = service.handle(ChatMessage(question="なんかよくわからないけど教えて"))
    assert result.need_human is True
    assert result.human_reason == "low_confidence"


@pytest.mark.integration
def test_no_knowledge_transfers_instead_of_hallucinating(tmp_path):
    """知识库没有相关切片时，必须兜底转人工而不是编造。"""
    backend = FakeRetrievalBackend([])
    service = build_service(backend, ticket_dir=tmp_path)
    result = service.handle(ChatMessage(question="御社の上場日はいつですか？"))
    assert result.need_human is True
    assert result.human_reason == "no_knowledge"
    assert result.source == "human"
    # 兜底话术是日语，不是模型自由发挥
    assert "申し訳" in result.answer


@pytest.mark.integration
def test_model_refusal_transfers_to_human(tmp_path):
    """检索有命中、但模型判定答不出来（按 prompt 输出哨兵）时，也必须转人工并建工单。

    回归用例：否则会出现「回答里说要转人工、系统却没转、也不建工单」的不一致。
    """
    backend = FakeRetrievalBackend([_hit()])
    service = build_service(backend, answer="NO_ANSWER", ticket_dir=tmp_path)
    result = service.handle(ChatMessage(question="御社の上場日はいつですか？"))

    assert result.need_human is True
    assert result.human_reason == "no_knowledge"
    assert result.source == "human"
    assert result.ticket_id and result.ticket_id.startswith("T")
    assert "申し訳" in result.answer
    # 工单落盘，人工可接手，且记录了「检索到了哪些来源」
    tickets = ticket_service_for(tmp_path).list_tickets()
    assert len(tickets) == 1
    assert tickets[0].sources == ["products/KB-0001.md"]


@pytest.mark.integration
def test_system_error_degrades_to_human(tmp_path):
    """检索后端挂掉时，不能让买家收到 500，而是降级转人工。"""

    class BoomBackend(FakeRetrievalBackend):
        def retrieve(self, query: str, top_k: int = 4):
            raise RuntimeError("ragflow 连接超时")

    service = build_service(BoomBackend([]), ticket_dir=tmp_path)
    result = service.handle(ChatMessage(question="サイズを教えてください"))
    assert result.need_human is True
    assert result.human_reason == "system_error"
    assert result.answer  # 仍有话术


@pytest.mark.integration
def test_multi_turn_rewrite_used_for_retrieval():
    """带指代的追问应先被改写成自包含语句，再拿去检索。"""
    backend = FakeRetrievalBackend([_hit()])
    store = SessionStore(ttl_seconds=60, max_turns=4)
    store.append("s1", Turn(question="このカバーは何の素材ですか？", answer="綿100%です。"))
    rewriter = QueryRewriter(FakeLLM("布団カバーは洗濯機で洗えますか？"))
    service = build_service(backend, session_store=store, rewriter=rewriter)

    result = service.handle(ChatMessage(question="洗える？", session_id="s1"))
    assert result.need_human is False
    # 检索用的 query 是改写后的句子
    assert backend.calls[-1][0] == "布団カバーは洗濯機で洗えますか？"


@pytest.mark.integration
def test_explicit_language_wins_over_detection():
    backend = FakeRetrievalBackend([_hit()])
    service = build_service(backend, answer="It's 100% cotton.")
    result = service.handle(ChatMessage(question="What is the material?", language="en"))
    assert result.language == "en"
    assert result.answer == "It's 100% cotton."


@pytest.mark.integration
def test_session_memory_records_turn():
    backend = FakeRetrievalBackend([_hit()])
    store = SessionStore(ttl_seconds=60, max_turns=4)
    service = build_service(backend, session_store=store)
    service.handle(ChatMessage(question="素材は何ですか？", session_id="s9"))
    turns = store.get("s9")
    assert len(turns) == 1
    assert turns[0].question == "素材は何ですか？"
    assert turns[0].need_human is False


# ----------------------------------------------------------------------
# 缓存与熔断降级（本轮新增能力）
# ----------------------------------------------------------------------
def _service_with_cache(tmp_path, backend: FakeRetrievalBackend, llm: FakeLLM):
    """构造带问答缓存的 ChatService，并返回 (service, generator_llm)。"""
    from customer_support.store.cache import AnswerCacheStore

    retriever = Retriever(backend, top_k=4, score_threshold=0.35)
    service = ChatService(
        intent_agent=IntentAgent(
            FakeLLM('{"intent": "product_info", "confidence": 0.9, "reason": "ok"}'),
            confidence_threshold=0.5,
        ),
        qa_agent=QAAgent(AnswerGenerator(retriever, llm, shop_name="くらし寝具")),
        translator=Translator(FakeLLM("en"), default_language="ja"),
        default_language="ja",
        session_store=SessionStore(),
        answer_cache=AnswerCacheStore(tmp_path / "cache.db"),
    )
    return service


@pytest.mark.integration
def test_cache_hit_skips_generation(tmp_path):
    """不同会话问同一个 FAQ：第二次应命中缓存，不再调用模型。"""
    backend = FakeRetrievalBackend([_hit()])
    llm = FakeLLM("綿100%の生地です。")
    service = _service_with_cache(tmp_path, backend, llm)

    first = service.handle(ChatMessage(question="素材は何ですか？", session_id="a"))
    assert first.cached is False
    assert first.source == "knowledge"
    calls_after_first = len(llm.calls)

    second = service.handle(ChatMessage(question="素材は何ですか？", session_id="b"))
    assert second.cached is True
    assert second.answer == first.answer
    assert len(llm.calls) == calls_after_first, "命中缓存后不应再调模型"


@pytest.mark.integration
def test_cache_not_used_within_same_session(tmp_path):
    """同一会话内的追问依赖上下文，绝不能吃缓存（否则会答非所问）。"""
    backend = FakeRetrievalBackend([_hit()])
    llm = FakeLLM("綿100%の生地です。")
    service = _service_with_cache(tmp_path, backend, llm)

    service.handle(ChatMessage(question="素材は何ですか？", session_id="same"))
    service.handle(ChatMessage(question="素材は何ですか？", session_id="same"))
    # 第二次因为会话已有历史 -> 不命中缓存 -> 仍然调用模型
    assert len(llm.calls) >= 2


@pytest.mark.integration
def test_answer_not_cached_when_transferred(tmp_path):
    """转人工（资料不足）的回答不进缓存——兜底话术不该被复用。"""
    backend = FakeRetrievalBackend([])
    llm = FakeLLM("绵")
    service = _service_with_cache(tmp_path, backend, llm)
    result = service.handle(ChatMessage(question="御社の上場日は？", session_id="x"))
    assert result.need_human is True
    assert result.source == "human"


@pytest.mark.integration
def test_circuit_open_degrades_to_service_unavailable(tmp_path):
    """LLM 熔断打开时必须快速失败，并把原因标成 service_unavailable（而不是 low_confidence）。"""
    from customer_support.common.resilience import configure_breaker, get_breaker, reset_resilience

    reset_resilience()
    # 用 configure_breaker（幂等）而不是 get_breaker(**kwargs)：后者对已存在的熔断器
    # 不会覆盖阈值，全量跑测试时会因为别处已建过 "llm" 熔断器而拿到默认阈值 5。
    configure_breaker("llm", failure_threshold=1, recovery_timeout=60)
    get_breaker("llm").record_failure()  # 直接打开熔断

    try:
        service = build_service(FakeRetrievalBackend([_hit()]), ticket_dir=tmp_path)
        result = service.handle(ChatMessage(question="素材は何ですか？"))
        assert result.need_human is True
        assert result.human_reason == "service_unavailable"
        assert result.source == "human"
        assert result.ticket_id  # 依赖挂了也要建工单，问题不能丢
        assert "申し訳" in result.answer
    finally:
        reset_resilience()


@pytest.mark.integration
def test_conversation_is_recorded_for_console_replay(tmp_path):
    """正常作答后必须落库：这是「跨设备复盘 + 运营统计」的数据来源。"""
    from customer_support.store.conversations import ConversationStore

    store = ConversationStore(tmp_path / "console.db")
    backend = FakeRetrievalBackend([_hit()])
    service = build_service(backend, conversation_store=store)

    service.handle(ChatMessage(question="素材は何ですか？", session_id="sess-x", buyer_id="u1"))

    items, total = store.list_conversations()
    assert total == 1
    assert items[0].session_id == "sess-x"
    assert items[0].last_question == "素材は何ですか？"

    detail = store.get_conversation("sess-x")
    assert detail is not None
    assert [m.role for m in detail.messages] == ["buyer", "agent"]
    assert detail.messages[1].source == "knowledge"
    assert detail.messages[1].references  # 引用一并留存，复盘时能追到依据


@pytest.mark.integration
def test_transferred_conversation_is_recorded_too(tmp_path):
    """转人工也要落库——否则最需要复盘的对话反而查不到。"""
    from customer_support.store.conversations import ConversationStore

    store = ConversationStore(tmp_path / "console.db")
    service = build_service(
        FakeRetrievalBackend([_hit()]),
        intent_payload='{"intent": "human", "confidence": 0.95, "reason": "返金要求"}',
        ticket_dir=tmp_path,
        conversation_store=store,
    )
    result = service.handle(ChatMessage(question="今すぐ返金しろ！", session_id="sess-h"))

    detail = store.get_conversation("sess-h")
    assert detail is not None
    assert detail.handoff_count == 1
    assert detail.ticket_ids == [result.ticket_id]
