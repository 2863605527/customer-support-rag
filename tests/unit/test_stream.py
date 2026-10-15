"""流式链路单测：生成器增量、拒答替换、ChatService 事件序列。

对应 SSE 流式输出（/chat/stream）的三层：AnswerGenerator.answer_stream →
QAAgent.run_stream → ChatService.handle_stream。
"""
from customer_support.models.knowledge import RetrievalHit
from customer_support.models.message import ChatMessage
from customer_support.rag.generator import FALLBACK_ANSWERS, AnswerGenerator
from customer_support.rag.retriever import Retriever
from customer_support.services.chat_service import ChatService
from customer_support.services.session import SessionStore
from tests.conftest import FakeLLM, FakeRetrievalBackend


def _generator(backend, answer):
    retriever = Retriever(backend, top_k=4, score_threshold=0.35)
    return AnswerGenerator(retriever, FakeLLM(answer), shop_name="くらし寝具")


def _hit(text="到着後30日以内は返品可能です。", score=0.8):
    return RetrievalHit(text=text, score=score, source="policy.md", doc_id="policy.md")


class TestAnswerStream:
    def test_deltas_join_to_final_answer(self):
        gen = _generator(FakeRetrievalBackend([_hit()]), "30日以内であれば返品可能です。")
        events = list(gen.answer_stream("返品は可能ですか？", language="ja"))

        assert events[-1][0] == "final"
        # 检索完成事件在最前，供上层上报「检索知识库 → 命中 N 条」
        assert events[0][0] == "retrieved" and len(events[0][1]) == 1
        deltas = [p for kind, p in events if kind == "delta"]
        assert deltas, "有资料时必须先流出增量"
        final = events[-1][1]
        assert "".join(deltas).strip() == final.answer
        assert final.source == "knowledge"

    def test_no_hits_emits_retrieved_then_final_fallback(self):
        gen = _generator(FakeRetrievalBackend([]), "何でも答えます")
        events = list(gen.answer_stream("今日の天気は？", language="ja"))

        kinds = [kind for kind, _ in events]
        assert kinds == ["retrieved", "final"]
        assert events[0][1] == [], "空命中也要上报 retrieved 事件"
        final = events[1][1]
        assert final.source == "no_answer"
        assert final.answer == FALLBACK_ANSWERS["ja"]

    def test_sentinel_streams_then_final_replaces_with_fallback(self):
        """模型流出哨兵 NO_ANSWER：增量照发，终态给兜底话术（上层据此整段替换）。"""
        gen = _generator(FakeRetrievalBackend([_hit()]), "NO_ANSWER")
        events = list(gen.answer_stream("宇宙人の柄の布団はありますか？", language="ja"))

        final = events[-1][1]
        assert final.source == "no_answer"
        assert final.answer == FALLBACK_ANSWERS["ja"]
        assert len(final.references) == 1, "引用保留供工单复盘"


def _chat_service(llm_payload: str, catalog_index=None) -> ChatService:
    from customer_support.agents.intent_agent import IntentAgent
    from customer_support.agents.qa_agent import QAAgent
    from customer_support.i18n.translator import Translator

    # FakeLLM 按调用顺序取 payload：①语言识别（喂非法输出→回退默认语言）
    # ②意图识别 ③回答生成（stream）
    llm = FakeLLM(
        [
            "not-a-language",
            '{"intent": "logistics", "confidence": 0.9, "reason": "t"}',
            llm_payload,
        ]
    )
    retriever = Retriever(FakeRetrievalBackend([_hit()]), top_k=4, score_threshold=0.35)
    return ChatService(
        intent_agent=IntentAgent(llm=llm, confidence_threshold=0.5),
        qa_agent=QAAgent(AnswerGenerator(retriever, llm=llm)),
        translator=Translator(llm=llm, default_language="ja"),
        default_language="ja",
        session_store=SessionStore(),
        catalog_index=catalog_index,
    )


class TestChatServiceHandleStream:
    def test_event_order_status_delta_done(self):
        service = _chat_service("1〜2週間で届きます。")
        events = list(service.handle_stream(ChatMessage(question="いつ届きますか？")))

        types = [e["type"] for e in events]
        assert types[0] == "status"
        assert types.count("done") == 1 and types[-1] == "done"
        assert "delta" in types

        # 阶段进度完整：检索 → 生成（含命中条数），前端据此渲染思考过程
        stages = [e.get("stage") for e in events if e["type"] == "status"]
        assert "retrieval" in stages and "generate" in stages
        generate = [e for e in events if e["type"] == "status" and e.get("stage") == "generate"][0]
        assert generate["hits"] == 1

        done = events[-1]["result"]
        assert done.source == "knowledge"
        assert done.need_human is False

    def test_high_risk_goes_human_without_deltas(self):
        from customer_support.agents.intent_agent import IntentAgent
        from customer_support.agents.qa_agent import QAAgent
        from customer_support.i18n.translator import Translator

        llm = FakeLLM('{"intent": "human", "confidence": 0.9, "reason": "返金要求"}')
        retriever = Retriever(FakeRetrievalBackend([_hit()]), top_k=4, score_threshold=0.35)
        service = ChatService(
            intent_agent=IntentAgent(llm=llm, confidence_threshold=0.5),
            qa_agent=QAAgent(AnswerGenerator(retriever, llm=llm)),
            translator=Translator(llm=llm, default_language="ja"),
            default_language="ja",
            session_store=SessionStore(),
        )
        events = list(service.handle_stream(ChatMessage(question="返金しろ！")))

        types = [e["type"] for e in events]
        assert "delta" not in types, "转人工路径不应有回答增量"
        done = events[-1]["result"]
        assert done.need_human is True
        assert done.human_reason == "high_risk_intent"

    def test_no_answer_replaces_streamed_content(self):
        service = _chat_service("NO_ANSWER")
        events = list(service.handle_stream(ChatMessage(question="宇宙人の柄はありますか？")))

        replaces = [e for e in events if e["type"] == "replace"]
        assert replaces, "拒答时必须发 replace 事件"
        done = events[-1]["result"]
        assert done.need_human is True
        assert done.answer == replaces[0]["text"] == FALLBACK_ANSWERS["ja"]
