"""检索器单测：阈值过滤 + 来源去重（防幻觉第一道防线）。"""
from customer_support.models.knowledge import RetrievalHit
from customer_support.rag.retriever import Retriever
from tests.conftest import FakeRetrievalBackend


def _hit(text: str, score: float, source: str) -> RetrievalHit:
    return RetrievalHit(text=text, score=score, source=source, doc_id=source)


class TestThreshold:
    def test_filters_below_threshold(self):
        backend = FakeRetrievalBackend([_hit("a", 0.9, "d1"), _hit("b", 0.2, "d2")])
        r = Retriever(backend, top_k=4, score_threshold=0.35)
        hits = r.retrieve("q")
        assert [h.text for h in hits] == ["a"]

    def test_all_below_threshold_returns_empty(self):
        backend = FakeRetrievalBackend([_hit("a", 0.1, "d1"), _hit("b", 0.2, "d2")])
        r = Retriever(backend, top_k=4, score_threshold=0.35)
        assert r.retrieve("q") == []

    def test_over_fetch_double_top_k(self):
        backend = FakeRetrievalBackend([])
        r = Retriever(backend, top_k=3, score_threshold=0.0)
        r.retrieve("hello")
        assert backend.calls[-1] == ("hello", 6)


class TestDedup:
    def test_max_per_source(self):
        backend = FakeRetrievalBackend(
            [
                _hit("a1", 0.9, "docA"),
                _hit("a2", 0.8, "docA"),
                _hit("a3", 0.7, "docA"),
                _hit("b1", 0.6, "docB"),
            ]
        )
        r = Retriever(backend, top_k=4, score_threshold=0.0, max_per_source=2)
        hits = r.retrieve("q")
        assert [h.text for h in hits] == ["a1", "a2", "b1"]

    def test_respects_top_k_cap(self):
        backend = FakeRetrievalBackend(
            [_hit(f"x{i}", 0.9 - i * 0.01, f"doc{i}") for i in range(10)]
        )
        r = Retriever(backend, top_k=2, score_threshold=0.0)
        assert len(r.retrieve("q")) == 2


class TestBuildContext:
    def test_empty_when_no_hits(self):
        backend = FakeRetrievalBackend([_hit("a", 0.1, "d1")])
        r = Retriever(backend, top_k=2, score_threshold=0.35)
        assert r.build_context("q") == ""

    def test_context_format_has_source(self):
        backend = FakeRetrievalBackend([_hit("本文", 0.9, "policy.md")])
        r = Retriever(backend, top_k=2, score_threshold=0.35)
        ctx = r.build_context("q")
        assert "出典：policy.md" in ctx
        assert "本文" in ctx


class TestPinnedAnchor:
    """确定性锚定：买家贴了商品链接时，该商品文档必须进参考资料。

    回归的是真实故障——同一个商品文档在锚点查询下相似度只有 0.549，
    排不到第一，模型拿不到资料就拒答转人工。锚定把「该由哪份文档回答」
    从相似度猜测变成确定事实。
    """

    def test_pinned_leads_and_similarity_fills_rest(self):
        backend = FakeRetrievalBackend([_hit("別商品の説明", 0.9, "KB-9999.md")])
        backend.add_document("KB-0001.md", ["素材：綿100%", "サイズ：150cm"])
        r = Retriever(backend, top_k=4, score_threshold=0.35)

        hits = r.retrieve("この商品の素材は？", pin_source="KB-0001.md")

        assert [h.source for h in hits] == ["KB-0001.md", "KB-0001.md", "KB-9999.md"]
        assert all(h.pinned for h in hits[:2])
        assert hits[2].pinned is False

    def test_pinned_source_not_taken_twice_from_similarity(self):
        """相似度也命中同一份文档时不该重复取材（否则挤占上下文预算）。"""
        backend = FakeRetrievalBackend([_hit("重复内容", 0.9, "KB-0001.md")])
        backend.add_document("KB-0001.md", ["素材：綿100%"])
        r = Retriever(backend, top_k=4, score_threshold=0.35)

        hits = r.retrieve("q", pin_source="KB-0001.md")

        assert [h.source for h in hits] == ["KB-0001.md"]

    def test_pinned_document_exempt_from_max_per_source(self):
        backend = FakeRetrievalBackend([])
        backend.add_document("KB-0001.md", ["a", "b", "c"])
        r = Retriever(backend, top_k=4, score_threshold=0.35, max_per_source=2)

        assert len(r.retrieve("q", pin_source="KB-0001.md")) == 3

    def test_pin_miss_degrades_to_similarity(self):
        """远端没有该文档（或后端不支持）时，必须退化为纯相似度检索。"""
        backend = FakeRetrievalBackend([_hit("a", 0.9, "d1")])
        r = Retriever(backend, top_k=4, score_threshold=0.35)

        assert [h.text for h in r.retrieve("q", pin_source="KB-0001.md")] == ["a"]

    def test_skips_backend_when_pin_fills_budget(self):
        backend = FakeRetrievalBackend([_hit("a", 0.9, "d1")])
        backend.add_document("KB-0001.md", ["p1", "p2"])
        r = Retriever(backend, top_k=2, score_threshold=0.35)

        assert [h.text for h in r.retrieve("q", pin_source="KB-0001.md")] == ["p1", "p2"]
        assert backend.calls == [], "锚定已占满预算时不该再打检索后端"

    def test_pin_backend_error_degrades(self):
        backend = FakeRetrievalBackend([_hit("a", 0.9, "d1")])

        def boom(source: str, *, limit: int = 4):
            raise RuntimeError("锚定通道挂了")

        backend.fetch_document = boom  # type: ignore[method-assign]
        r = Retriever(backend, top_k=4, score_threshold=0.35)

        assert [h.text for h in r.retrieve("q", pin_source="KB-0001.md")] == ["a"]
