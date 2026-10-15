"""质量反馈单测：幂等提交、原因过滤、好评率与评测集导出。"""
from customer_support.models.feedback import FeedbackSubmission
from customer_support.store.feedback import FeedbackStore


def _submit(store: FeedbackStore, trace_id: str, rating: str, **over) -> None:
    payload = FeedbackSubmission(
        trace_id=trace_id,
        rating=rating,
        reasons=over.pop("reasons", []),
        comment=over.pop("comment", ""),
        question="素材は何ですか？",
        answer="綿100%です。",
        source=over.pop("source", "knowledge"),
        session_id="s1",
    )
    store.submit(payload)


class TestSubmit:
    def test_roundtrip(self, tmp_path):
        store = FeedbackStore(tmp_path / "f.db")
        _submit(store, "t1", "up", reasons=["accurate"])
        record = store.get("t1")
        assert record is not None
        assert record.rating == "up"
        assert record.reasons == ["accurate"]

    def test_same_trace_overwrites(self, tmp_path):
        """同一条回答改主意：只保留最后一次评价，不产生重复样本。"""
        store = FeedbackStore(tmp_path / "f.db")
        _submit(store, "t1", "down", reasons=["irrelevant"])
        _submit(store, "t1", "up", reasons=["accurate"])

        stats = store.stats()
        assert stats.total == 1
        assert stats.up == 1
        assert stats.down == 0

    def test_unknown_reason_tags_are_dropped(self, tmp_path):
        store = FeedbackStore(tmp_path / "f.db")
        _submit(store, "t1", "down", reasons=["irrelevant", "totally-made-up", "irrelevant"])
        record = store.get("t1")
        assert record is not None
        assert record.reasons == ["irrelevant"]

    def test_comment_is_trimmed_and_bounded(self, tmp_path):
        store = FeedbackStore(tmp_path / "f.db")
        _submit(store, "t1", "down", reasons=["other"], comment="  多   空格  ")
        record = store.get("t1")
        assert record is not None
        assert record.comment == "多 空格"


class TestStatsAndExport:
    def test_positive_rate_and_reason_cluster(self, tmp_path):
        store = FeedbackStore(tmp_path / "f.db")
        _submit(store, "t1", "up", reasons=["accurate"])
        _submit(store, "t2", "down", reasons=["missing_product"])
        _submit(store, "t3", "down", reasons=["missing_product", "incomplete"])

        stats = store.stats()
        assert stats.total == 3
        assert stats.positive_rate == round(1 / 3, 4)
        assert stats.by_reason["missing_product"] == 2
        assert stats.by_source["knowledge"]["total"] == 3
        assert len(stats.recent_down) == 2

    def test_export_dataset_marks_expected_ok(self, tmp_path):
        store = FeedbackStore(tmp_path / "f.db")
        _submit(store, "t1", "up")
        _submit(store, "t2", "down", reasons=["wrong_info"])

        dataset = store.export_dataset()
        assert len(dataset) == 2
        by_trace = {d["trace_id"]: d for d in dataset}
        assert by_trace["t1"]["expected_ok"] is True
        assert by_trace["t2"]["expected_ok"] is False
        assert by_trace["t2"]["reasons"] == ["wrong_info"]

    def test_empty_stats(self, tmp_path):
        store = FeedbackStore(tmp_path / "f.db")
        stats = store.stats()
        assert stats.total == 0
        assert stats.positive_rate is None
