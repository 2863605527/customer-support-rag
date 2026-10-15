"""RAGFlow 入库客户端单测（httpx.MockTransport，不碰真实服务）。

重点回归：上传后 RAGFlow 会自动开始解析，此时再触发 parse 会返回业务码 102；
这个「状态冲突」必须被容忍，否则整个 ingest 会在最后一步崩掉。
"""
from __future__ import annotations

import json

import httpx
import pytest

from customer_support.common.exceptions import ExternalAPIError, KnowledgeError
from customer_support.knowledge.ragflow_ingest import (
    ALREADY_PARSING_CODE,
    IngestReport,
    RagflowIngestor,
)


def _ingestor(handler, **kw) -> RagflowIngestor:
    return RagflowIngestor(
        "http://ragflow",
        "ragflow-test-key",
        client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://ragflow"),
        **kw,
    )


class TestConstruction:
    def test_missing_api_key_raises(self):
        with pytest.raises(KnowledgeError):
            RagflowIngestor("http://ragflow", "")


class TestParseTolerance:
    def test_code_102_is_tolerated(self):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": ALREADY_PARSING_CODE, "message": "already running"})

        # 不应抛异常
        _ingestor(handler).parse("ds1", ["d1", "d2"])

    def test_other_business_error_still_raises(self):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": 500, "message": "boom"})

        with pytest.raises(ExternalAPIError) as ei:
            _ingestor(handler).parse("ds1", ["d1"])
        assert ei.value.remote_code == 500

    def test_empty_ids_is_noop(self):
        called = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            called["n"] += 1
            return httpx.Response(200, json={"code": 0, "data": {}})

        _ingestor(handler).parse("ds1", [])
        assert called["n"] == 0


class TestDocumentStates:
    def test_remote_code_attached_on_business_error(self):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": 100, "message": "max page size"})

        with pytest.raises(ExternalAPIError) as ei:
            _ingestor(handler).list_documents("ds1")
        assert ei.value.remote_code == 100


class TestIngestReport:
    def test_ok_when_all_done(self):
        r = IngestReport(dataset_id="ds", parse_status={"a.md": "DONE", "b.md": "DONE"})
        assert r.ok is True

    def test_not_ok_when_any_failed_parse(self):
        r = IngestReport(dataset_id="ds", parse_status={"a.md": "DONE", "b.md": "FAIL"})
        assert r.ok is False

    def test_not_ok_when_upload_failed(self):
        r = IngestReport(
            dataset_id="ds",
            parse_status={"a.md": "DONE"},
            failed=[{"batch": ["x.md"], "error": "boom"}],
        )
        assert r.ok is False

    def test_ok_when_nothing_to_do(self):
        assert IngestReport(dataset_id="ds").ok is True

    def test_to_dict_roundtrip(self):
        r = IngestReport(dataset_id="ds", uploaded=["a.md"], skipped=["b.md"])
        d = r.to_dict()
        assert d["dataset_id"] == "ds"
        assert d["uploaded"] == ["a.md"]
        assert d["ok"] is True


class TestChunksAndRename:
    """收敛脚本依赖的两个能力：列分块、把 "(1)" 副本改回规范名。"""

    def test_list_chunks_stops_on_short_page(self):
        def handler(req: httpx.Request) -> httpx.Response:
            page = req.url.params.get("page")
            batch = [{"id": "c1", "content": "x"}] if page == "1" else []
            return httpx.Response(200, json={"code": 0, "data": {"chunks": batch}})

        assert [c["id"] for c in _ingestor(handler).list_chunks("ds1", "d1")] == ["c1"]

    def test_list_chunks_honors_limit(self):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"code": 0, "data": {"chunks": [{"id": "c1"}, {"id": "c2"}]}}
            )

        assert [c["id"] for c in _ingestor(handler).list_chunks("ds1", "d1", limit=1)] == ["c1"]

    def test_rename_document_puts_name(self):
        seen: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["method"] = req.method
            seen["path"] = req.url.path
            seen["body"] = json.loads(req.content)
            return httpx.Response(200, json={"code": 0, "data": {}})

        _ingestor(handler).rename_document("ds1", "d1", "KB-0001.md")
        assert seen["method"] == "PUT"
        assert seen["path"] == "/api/v1/datasets/ds1/documents/d1"
        assert seen["body"] == {"name": "KB-0001.md"}
