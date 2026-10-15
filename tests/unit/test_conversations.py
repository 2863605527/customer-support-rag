"""会话与消息持久化单测：一轮问答成对落库、会话摘要、指标口径。"""
from customer_support.models.message import ChatResult, Reference
from customer_support.store.conversations import ConversationStore


def _result(*, need_human: bool, source: str = "knowledge", latency: int = 800) -> ChatResult:
    return ChatResult(
        question="素材は何ですか？",
        answer="綿100%です。" if not need_human else "担当者におつなぎします。",
        intent="product_info",
        need_human=need_human,
        human_reason="no_knowledge" if need_human else "",
        language="ja",
        source="human" if need_human else source,
        confidence=0.9,
        references=[Reference(source="products/KB-0001.md", doc_id="d1", score=0.8, snippet="綿")],
        ticket_id="T20260101-ABCDEF" if need_human else None,
        trace_id="trace-1",
        latency_ms=latency,
    )


def _turn(store: ConversationStore, session_id: str, *, need_human: bool = False) -> None:
    store.record_turn(
        session_id=session_id,
        question="素材は何ですか？",
        result=_result(need_human=need_human),
        buyer_id="buyer-1",
    )


class TestRecordAndRead:
    def test_turn_writes_buyer_and_agent_messages(self, tmp_path):
        store = ConversationStore(tmp_path / "c.db")
        _turn(store, "s1")

        detail = store.get_conversation("s1")
        assert detail is not None
        assert detail.message_count == 2
        assert [m.role for m in detail.messages] == ["buyer", "agent"]
        assert detail.messages[1].references[0].source == "products/KB-0001.md"
        assert detail.last_question == "素材は何ですか？"

    def test_second_turn_appends_and_counts_handoff(self, tmp_path):
        store = ConversationStore(tmp_path / "c.db")
        _turn(store, "s1")
        _turn(store, "s1", need_human=True)

        detail = store.get_conversation("s1")
        assert detail is not None
        assert detail.message_count == 4
        assert detail.handoff_count == 1
        assert detail.ticket_ids == ["T20260101-ABCDEF"]

    def test_unknown_session_returns_none(self, tmp_path):
        store = ConversationStore(tmp_path / "c.db")
        assert store.get_conversation("nope") is None

    def test_delete_removes_messages(self, tmp_path):
        store = ConversationStore(tmp_path / "c.db")
        _turn(store, "s1")
        store.delete("s1")
        assert store.get_conversation("s1") is None
        assert store.stats()["messages"] == 0


class TestListAndMetrics:
    def test_list_orders_by_recent_update(self, tmp_path):
        store = ConversationStore(tmp_path / "c.db")
        _turn(store, "s1")
        _turn(store, "s2")
        items, total = store.list_conversations()
        assert total == 2
        assert {i.session_id for i in items} == {"s1", "s2"}

    def test_search_filters_by_session_id(self, tmp_path):
        store = ConversationStore(tmp_path / "c.db")
        _turn(store, "web-aaa")
        _turn(store, "web-bbb")
        items, total = store.list_conversations(search="aaa")
        assert total == 1
        assert items[0].session_id == "web-aaa"

    def test_metrics_containment_excludes_handoffs(self, tmp_path):
        store = ConversationStore(tmp_path / "c.db")
        _turn(store, "s1")
        _turn(store, "s2")
        _turn(store, "s3", need_human=True)

        m = store.metrics(days=7)
        assert m["answers"] == 3
        assert m["handoffs"] == 1
        assert m["containment_rate"] == round(2 / 3, 4)
        assert m["handoff_rate"] == round(1 / 3, 4)
        assert m["by_source"]["knowledge"] == 2
        assert m["by_source"]["human"] == 1
        assert m["by_handoff_reason"]["no_knowledge"] == 1
        assert m["avg_latency_ms"] == 800
        assert len(m["daily"]) >= 1

    def test_metrics_without_data_returns_none_rates(self, tmp_path):
        store = ConversationStore(tmp_path / "c.db")
        m = store.metrics(days=7)
        assert m["answers"] == 0
        assert m["containment_rate"] is None
        assert m["handoff_rate"] is None
