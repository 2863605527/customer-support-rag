"""RAGFlow 检索后端单测（用 httpx.MockTransport 拦截，不碰真实服务）。"""
import httpx
import pytest

from customer_support.common.exceptions import ConfigError, ExternalAPIError
from customer_support.rag.ragflow_backend import RagflowBackend

DATASETS = [{"id": "ds1", "name": "tiktok_bedding_kb", "document_count": 67}]


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://ragflow")


def _ok(payload):
    return httpx.Response(200, json={"code": 0, "data": payload})


def _backend(handler, **kw) -> RagflowBackend:
    return RagflowBackend(
        "http://ragflow", "ragflow-test-key", ["tiktok_bedding_kb"], client=_client(handler), **kw
    )


class TestResolveDatasetIds:
    def test_resolve_by_name(self):
        bk = _backend(lambda req: _ok(DATASETS))
        assert bk.resolve_dataset_ids() == ["ds1"]

    def test_resolve_by_id(self):
        bk = RagflowBackend(
            "http://ragflow", "k", ["ds1"], client=_client(lambda req: _ok(DATASETS))
        )
        assert bk.resolve_dataset_ids() == ["ds1"]

    def test_missing_dataset_raises_config_error(self):
        bk = RagflowBackend(
            "http://ragflow", "k", ["nope"], client=_client(lambda req: _ok(DATASETS))
        )
        with pytest.raises(ConfigError):
            bk.resolve_dataset_ids()

    def test_cached_after_first_call(self):
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            return _ok(DATASETS)

        bk = _backend(handler)
        bk.resolve_dataset_ids()
        bk.resolve_dataset_ids()
        assert calls["n"] == 1


class TestRetrieve:
    def _handler(self, chunks, retrieval_ok=True):
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/api/v1/datasets":
                return _ok(DATASETS)
            if req.url.path == "/api/v1/retrieval":
                if not retrieval_ok:
                    return httpx.Response(200, json={"code": 102, "message": "boom"})
                return _ok({"chunks": chunks})
            return httpx.Response(404)

        return handler

    def test_normalizes_and_sorts(self):
        chunks = [
            {"content": "低相关", "similarity": 0.2, "document_keyword": "b.md", "document_id": "d2"},
            {"content": "高相关", "similarity": 0.85, "document_keyword": "a.md", "document_id": "d1"},
        ]
        bk = _backend(self._handler(chunks))
        hits = bk.retrieve("返品できますか？", top_k=4)
        assert [h.text for h in hits] == ["高相关", "低相关"]
        assert hits[0].source == "a.md"
        assert hits[0].doc_id == "d1"
        assert hits[0].score == pytest.approx(0.85)

    def test_skips_empty_content(self):
        chunks = [
            {"content": "  ", "similarity": 0.9, "document_keyword": "a.md"},
            {"content": "有效", "similarity": 0.5, "document_keyword": "b.md"},
        ]
        bk = _backend(self._handler(chunks))
        assert [h.text for h in bk.retrieve("q")] == ["有效"]

    def test_business_error_raises(self):
        bk = _backend(self._handler([], retrieval_ok=False))
        with pytest.raises(ExternalAPIError):
            bk.retrieve("q")

    def test_empty_query_returns_empty_without_call(self):
        bk = _backend(self._handler([]))
        assert bk.retrieve("   ") == []

    def test_http_5xx_raises(self):
        def handler(req):
            if req.url.path == "/api/v1/datasets":
                return _ok(DATASETS)
            return httpx.Response(500, text="gateway")

        bk = _backend(handler, max_retries=1)
        with pytest.raises(ExternalAPIError):
            bk.retrieve("q")


class TestCountAndHealth:
    def test_count(self):
        bk = _backend(lambda req: _ok(DATASETS))
        assert bk.count() == 67

    def test_health_ok_and_error(self):
        assert _backend(lambda req: _ok(DATASETS)).health()["ok"] is True

        def boom(req):
            raise httpx.ConnectError("refused")

        bad = _backend(boom, max_retries=1)
        assert bad.health()["ok"] is False

    def test_describe(self):
        d = _backend(lambda req: _ok(DATASETS)).describe()
        assert d["backend"] == "ragflow"
        assert d["datasets"] == ["tiktok_bedding_kb"]


DOCS = [
    {"id": "doc1", "name": "KB-0001.md", "size": 100},
    {"id": "doc2", "name": "KB-0002.md", "size": 200},
]
DOC_CHUNKS = [
    {"id": "c1", "content": "## 素材\n綿100%"},
    {"id": "c2", "content": "## サイズ\n150cm"},
]


class TestFetchDocument:
    """按文档名确定性取回（商品链接锚定）：不走向量相似度。"""

    def _handler(self, docs=DOCS, chunks=DOC_CHUNKS, fail_chunks=False):
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/api/v1/datasets":
                return _ok(DATASETS)
            if req.url.path == "/api/v1/datasets/ds1/documents":
                return _ok({"docs": docs})
            if req.url.path == "/api/v1/datasets/ds1/documents/doc1/chunks":
                if fail_chunks:
                    return httpx.Response(500, text="gateway boom")
                return _ok({"chunks": chunks})
            return httpx.Response(404)

        return handler

    def test_fetches_by_name_and_marks_pinned(self):
        bk = _backend(self._handler(), max_retries=1)
        hits = bk.fetch_document("KB-0001.md", limit=4)

        assert [h.text for h in hits] == ["## 素材\n綿100%", "## サイズ\n150cm"]
        assert all(h.pinned is True for h in hits)
        assert all(h.score == 1.0 for h in hits)
        assert hits[0].source == "KB-0001.md" and hits[0].doc_id == "doc1"

    def test_unknown_name_returns_empty(self):
        bk = _backend(self._handler(), max_retries=1)
        assert bk.fetch_document("KB-9999.md") == []

    def test_empty_source_is_noop(self):
        bk = _backend(self._handler(), max_retries=1)
        assert bk.fetch_document("   ") == []

    def test_matches_copy_suffixed_name(self):
        """历史遗留 "(1)" 副本名也要能锚定上（兜底匹配，别让干净名字查不到）。"""
        docs = [{"id": "doc1", "name": "KB-0001(1).md", "size": 100}]
        bk = _backend(self._handler(docs=docs), max_retries=1)

        hits = bk.fetch_document("KB-0001.md")
        assert hits and hits[0].source == "KB-0001(1).md"

    def test_limit_caps_chunks(self):
        bk = _backend(self._handler(), max_retries=1)
        assert len(bk.fetch_document("KB-0001.md", limit=1)) == 1

    def test_skips_blank_chunks(self):
        chunks = [{"id": "c1", "content": "  \n "}, {"id": "c2", "content": "有効"}]
        bk = _backend(self._handler(chunks=chunks), max_retries=1)
        assert [h.text for h in bk.fetch_document("KB-0001.md")] == ["有効"]

    def test_request_failure_degrades_to_empty(self):
        """锚定是增强能力：远端异常要退化为空，交给相似度检索，绝不能抛出。"""
        bk = _backend(self._handler(fail_chunks=True), max_retries=1)
        assert bk.fetch_document("KB-0001.md") == []

    def test_document_index_is_cached(self):
        calls = {"docs": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/api/v1/datasets":
                return _ok(DATASETS)
            if req.url.path == "/api/v1/datasets/ds1/documents":
                calls["docs"] += 1
                return _ok({"docs": DOCS})
            if req.url.path.endswith("/chunks"):
                return _ok({"chunks": DOC_CHUNKS})
            return httpx.Response(404)

        bk = _backend(handler, max_retries=1)
        bk.fetch_document("KB-0001.md")
        bk.fetch_document("KB-0002.md")
        assert calls["docs"] == 1, "文档索引带 TTL 缓存，不应每问一次就拉一遍"
