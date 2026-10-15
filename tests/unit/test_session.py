"""会话记忆与查询改写单测。

重点验证「改写失败不能拖垮主流程」这条降级原则。
"""
from customer_support.services.session import QueryRewriter, SessionStore, Turn
from tests.conftest import FakeLLM


class TestSessionStore:
    def test_append_and_get(self):
        store = SessionStore(ttl_seconds=60, max_turns=4)
        store.append("s1", Turn(question="q1", answer="a1"))
        turns = store.get("s1")
        assert [t.question for t in turns] == ["q1"]

    def test_no_session_id_is_noop(self):
        store = SessionStore()
        store.append(None, Turn(question="q"))
        assert store.get(None) == []

    def test_max_turns_trims_oldest(self):
        store = SessionStore(ttl_seconds=60, max_turns=2)
        for i in range(5):
            store.append("s1", Turn(question=f"q{i}"))
        assert [t.question for t in store.get("s1")] == ["q3", "q4"]

    def test_expired_session_dropped(self):
        store = SessionStore(ttl_seconds=-1, max_turns=4)
        store.append("s1", Turn(question="q1"))
        assert store.get("s1") == []

    def test_clear(self):
        store = SessionStore()
        store.append("s1", Turn(question="q1"))
        store.clear("s1")
        assert store.get("s1") == []
        store.append("s1", Turn(question="q1"))
        store.clear()
        assert store.stats()["sessions"] == 0

    def test_stats(self):
        store = SessionStore()
        store.append("s1", Turn(question="q1"))
        store.append("s2", Turn(question="q2"))
        assert store.stats() == {"sessions": 2, "turns": 2}


class TestQueryRewriter:
    def test_no_history_returns_original(self):
        rw = QueryRewriter(FakeLLM("改写后"))
        assert rw.rewrite("これ洗える？", []) == "これ洗える？"

    def test_rewrites_with_history(self):
        rw = QueryRewriter(FakeLLM("全棉挂布団カバーは洗濯機で洗えますか？"))
        history = [Turn(question="全棉挂布団カバーは何の素材ですか？", answer="綿100%です。")]
        assert "洗濯機" in rw.rewrite("これ洗える？", history)

    def test_fail_safe_on_llm_error(self):
        class Boom:
            def invoke(self, prompt):
                raise RuntimeError("模型超时")

        rw = QueryRewriter(Boom())
        history = [Turn(question="q", answer="a")]
        assert rw.rewrite("これ洗える？", history) == "これ洗える？"

    def test_disabled_returns_original(self):
        rw = QueryRewriter(FakeLLM("改写后"), enabled=False)
        history = [Turn(question="q", answer="a")]
        assert rw.rewrite("これ洗える？", history) == "これ洗える？"

    def test_over_long_rewrite_falls_back(self):
        rw = QueryRewriter(FakeLLM("あ" * 500))
        history = [Turn(question="q", answer="a")]
        assert rw.rewrite("短い質問", history) == "短い質問"


class TestTurnDefaults:
    def test_defaults(self):
        t = Turn(question="q")
        assert t.intent == "unknown"
        assert t.need_human is False
        assert t.ts > 0
