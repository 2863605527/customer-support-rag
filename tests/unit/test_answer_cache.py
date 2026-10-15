"""问答缓存单测：键规范化、TTL 过期、命中打标与统计。"""
from datetime import datetime, timedelta

from customer_support.models.message import ChatResult
from customer_support.store.base import to_iso, utcnow
from customer_support.store.cache import AnswerCacheStore, cache_key, normalize


def _result(answer: str = "綿100%です。") -> ChatResult:
    return ChatResult(
        question="素材は何ですか？",
        answer=answer,
        intent="product_info",
        source="knowledge",
        language="ja",
        confidence=0.9,
        trace_id="t1",
        latency_ms=1234,
    )


def _expire_all(store: AnswerCacheStore) -> None:
    """把全部缓存条目标记为已过期（避免测试真的 sleep）。"""
    with store._write() as conn:  # noqa: SLF001 - 直连测试库改过期时间最快
        conn.execute("UPDATE answer_cache SET expires_at = ?", (to_iso(utcnow()),))


class TestNormalize:
    def test_collapses_whitespace_and_fullwidth_space(self):
        assert normalize("  送料　は  いくら ？ ") == "送料 は いくら ？"

    def test_same_key_for_equivalent_questions(self):
        assert cache_key("送料は？", "ja") == cache_key(" 送料は？ ", "ja")

    def test_language_participates_in_key(self):
        assert cache_key("hello", "en") != cache_key("hello", "ja")


class TestAnswerCacheStore:
    def test_put_and_get_marks_cached(self, tmp_path):
        store = AnswerCacheStore(tmp_path / "c.db")
        key = cache_key("素材は何ですか？", "ja")
        store.put(key, "素材は何ですか？", "ja", _result(), ttl_seconds=600)

        hit = store.get(key)
        assert hit is not None
        assert hit.cached is True
        assert hit.latency_ms == 0  # 缓存命中不计服务端耗时，避免"美化"平均延迟
        assert hit.answer == "綿100%です。"
        assert store.stats()["hits"] == 1

    def test_missing_key_returns_none(self, tmp_path):
        store = AnswerCacheStore(tmp_path / "c.db")
        assert store.get(cache_key("未缓存的问题", "ja")) is None

    def test_expired_entry_is_dropped(self, tmp_path):
        store = AnswerCacheStore(tmp_path / "c.db")
        key = cache_key("q", "ja")
        store.put(key, "q", "ja", _result(), ttl_seconds=600)
        _expire_all(store)

        assert store.get(key) is None
        assert store.stats()["entries"] == 0

    def test_put_is_idempotent_on_same_key(self, tmp_path):
        store = AnswerCacheStore(tmp_path / "c.db")
        key = cache_key("q", "ja")
        store.put(key, "q", "ja", _result("旧答案"), ttl_seconds=600)
        store.put(key, "q", "ja", _result("新答案"), ttl_seconds=600)

        assert store.stats()["entries"] == 1
        assert store.get(key).answer == "新答案"  # type: ignore[union-attr]

    def test_purge_and_clear(self, tmp_path):
        store = AnswerCacheStore(tmp_path / "c.db")
        store.put(cache_key("a", "ja"), "a", "ja", _result(), ttl_seconds=600)
        store.put(cache_key("b", "ja"), "b", "ja", _result(), ttl_seconds=600)
        _expire_all(store)
        assert store.purge_expired() == 2

        store.put(cache_key("c", "ja"), "c", "ja", _result(), ttl_seconds=600)
        assert store.clear() == 1
        assert store.stats()["entries"] == 0


def test_ttl_window_is_honoured(tmp_path):
    store = AnswerCacheStore(tmp_path / "c.db")
    key = cache_key("q", "ja")
    store.put(key, "q", "ja", _result(), ttl_seconds=600)
    with store._write() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT created_at, expires_at FROM answer_cache WHERE cache_key = ?", (key,)
        ).fetchone()
    ttl = datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["created_at"])
    assert timedelta(seconds=590) < ttl <= timedelta(seconds=600)
