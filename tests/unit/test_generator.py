"""回答生成器单测：资料不足走兜底、有资料则带可溯源引用。"""
from customer_support.rag.generator import FALLBACK_ANSWERS, AnswerGenerator
from customer_support.rag.retriever import Retriever
from tests.conftest import FakeLLM, FakeRetrievalBackend


def _generator(backend, answer="ご案内します。", language_hits=True):
    retriever = Retriever(backend, top_k=4, score_threshold=0.35)
    return AnswerGenerator(retriever, FakeLLM(answer), shop_name="くらし寝具")


class TestNoAnswer:
    def test_empty_hits_returns_fallback_and_no_answer(self):
        gen = _generator(FakeRetrievalBackend([]))
        result = gen.answer("在庫はありますか？", language="ja")
        assert result.source == "no_answer"
        assert result.answer == FALLBACK_ANSWERS["ja"]
        assert result.references == []

    def test_all_below_threshold_returns_fallback(self):
        from customer_support.models.knowledge import RetrievalHit

        backend = FakeRetrievalBackend(
            [RetrievalHit(text="x", score=0.1, source="d", doc_id="d")]
        )
        gen = _generator(backend)
        assert gen.answer("q", language="en").source == "no_answer"

    def test_fallback_language_variants(self):
        gen = _generator(FakeRetrievalBackend([]))
        assert gen.fallback("en") == FALLBACK_ANSWERS["en"]
        assert gen.fallback("xx") == FALLBACK_ANSWERS["default"]


class TestWithKnowledge:
    def test_knowledge_source_and_references(self):
        from customer_support.models.knowledge import RetrievalHit

        backend = FakeRetrievalBackend(
            [
                RetrievalHit(text="到着後30日以内は返品可能です。", score=0.8, source="after_sales_policy.md", doc_id="policies/after_sales_policy.md"),
            ]
        )
        gen = _generator(backend, answer="30日以内であれば返品可能です。")
        result = gen.answer("返品は可能ですか？", language="ja")
        assert result.source == "knowledge"
        assert result.answer == "30日以内であれば返品可能です。"
        assert len(result.references) == 1
        assert result.references[0].source == "after_sales_policy.md"
        assert result.references[0].score == 0.8
        assert "返品可能" in result.references[0].snippet

    def test_prompt_contains_shop_name_and_context(self):
        from customer_support.models.knowledge import RetrievalHit

        backend = FakeRetrievalBackend(
            [RetrievalHit(text="コンテキスト本文", score=0.9, source="d.md", doc_id="d")]
        )
        llm = FakeLLM("回答")
        gen = AnswerGenerator(Retriever(backend, top_k=4, score_threshold=0.35), llm)
        gen.answer("質問文", language="ja")
        prompt = llm.calls[-1]
        assert "くらし寝具" in prompt
        assert "コンテキスト本文" in prompt
        assert "質問文" in prompt


class TestModelRefusal:
    """检索有命中、但模型判定「资料里没有答案」时，也必须判为资料不足。

    回归用例：旧实现只在「检索为空」时才判 ``no_answer``，模型按 prompt 输出兜底
    话术后系统仍标记 ``knowledge`` 且不转人工，导致「话术说要转人工、实际没转」。
    """

    @staticmethod
    def _backend():
        from customer_support.models.knowledge import RetrievalHit

        return FakeRetrievalBackend(
            [RetrievalHit(text="到着後30日以内は返品可能です。", score=0.9, source="p.md", doc_id="p")]
        )

    def _gen(self, payload: str):
        return AnswerGenerator(
            Retriever(self._backend(), top_k=4, score_threshold=0.35), FakeLLM(payload)
        )

    def test_sentinel_marks_no_answer(self):
        result = self._gen("NO_ANSWER").answer("御社の上場日は？", language="ja")
        assert result.source == "no_answer"
        assert result.answer == FALLBACK_ANSWERS["ja"]
        # 仍保留检索到的来源，供工单复盘
        assert len(result.references) == 1

    def test_sentinel_with_trailing_text_still_detected(self):
        result = self._gen("NO_ANSWER。この件はご案内できません。").answer("q", language="ja")
        assert result.source == "no_answer"

    def test_verbatim_fallback_still_detected(self):
        result = self._gen(FALLBACK_ANSWERS["en"]).answer("Do you list on Nasdaq?", language="en")
        assert result.source == "no_answer"

    def test_empty_answer_is_no_answer(self):
        assert self._gen("").answer("q", language="ja").source == "no_answer"

    def test_normal_answer_not_flagged(self):
        result = self._gen("30日以内であれば返品可能です。").answer("返品できますか？", language="ja")
        assert result.source == "knowledge"
        assert result.answer == "30日以内であれば返品可能です。"
