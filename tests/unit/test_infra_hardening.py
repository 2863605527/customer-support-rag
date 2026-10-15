"""基础设施加固的回归测试（0.7.0 审计产出）。

覆盖六个真实缺陷，每个测试都对应一条「曾经会出错」的具体行为：

1. 同一库文件的多个 store **共享同一把写锁**（原来是实例级锁，等于没互斥）；
2. SQLite 连接开启 ``foreign_keys``（原来只在建表连接上开，业务连接不开）；
3. ``X-Forwarded-For`` **默认不被采信**（原来无条件采信 → 伪造头即可绕过登录限流）；
4. 限流器参数变更后**真的生效**（原来只在首次创建时应用，之后静默忽略）；
5. LLM 健康判定**不会永久卡在不可用**（原来失败计数只能被下一次成功清零）；
6. 知识库变更后**作废答案缓存**（原来旧答案会继续命中到 TTL 到期）。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from customer_support.api import deps
from customer_support.api.v1 import ops
from customer_support.common.ratelimit import get_limiter, reset_limiters
from customer_support.common.resilience import LLMHealthMonitor
from customer_support.models.message import ChatResult
from customer_support.models.ticket import Ticket
from customer_support.store.cache import AnswerCacheStore
from customer_support.store.conversations import ConversationStore
from customer_support.store.feedback import FeedbackStore
from customer_support.store.tickets import TicketStore


# ----------------------------------------------------------------------
# 1. 写锁按库文件共享
# ----------------------------------------------------------------------
def test_stores_on_same_db_share_one_write_lock(tmp_path):
    """四个 store 指向同一文件时，必须共用同一把锁——否则并发写不互斥。"""
    db = tmp_path / "console.db"
    stores = [
        ConversationStore(db),
        TicketStore(db),
        FeedbackStore(db),
        AnswerCacheStore(db),
    ]
    assert len({id(s._write_lock) for s in stores}) == 1


def test_different_db_files_do_not_share_lock(tmp_path):
    """不同库文件各用各的锁（避免无关的库互相阻塞）。"""
    a = TicketStore(tmp_path / "a.db")
    b = TicketStore(tmp_path / "b.db")
    assert a._write_lock is not b._write_lock


def test_concurrent_writes_across_stores_succeed(tmp_path):
    """两个 store 并发写同一库：共享锁下不应冒出 database is locked。

    规模刻意压小：本机（Windows + WAL）每次「开连接→写→关连接」约 0.3s，
    用例体量再大就变成测磁盘而不是测锁。真正的守卫是上面两条**确定性**断言
    （同文件 → 同一把锁对象），这里只做一次真实的交叉写入冒烟。
    """
    db = tmp_path / "console.db"
    tickets = TicketStore(db)
    cache = AnswerCacheStore(db)
    errors: list[str] = []
    rounds = 8

    def write_tickets(prefix: str) -> None:
        try:
            for i in range(rounds):
                tickets.create(Ticket(ticket_id=f"{prefix}{i}", question=f"q{i}"))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"tickets: {type(exc).__name__}: {exc}")

    def write_cache(prefix: str) -> None:
        try:
            for i in range(rounds):
                cache.put(
                    f"{prefix}-{i}", f"q{i}", "ja",
                    ChatResult(question=f"q{i}", answer="a"), ttl_seconds=600,
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"cache: {type(exc).__name__}: {exc}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write_tickets, "A"), pool.submit(write_cache, "C")]
        for f in futures:
            f.result()

    assert errors == []
    assert len(tickets.list_tickets()) == rounds
    assert cache.stats()["entries"] == rounds


# ----------------------------------------------------------------------
# 2. 连接级 PRAGMA foreign_keys
# ----------------------------------------------------------------------
def test_every_connection_enables_foreign_keys(tmp_path):
    """``foreign_keys`` 是连接级开关，业务连接也必须打开。"""
    store = TicketStore(tmp_path / "console.db")
    with store._read() as conn:
        assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
    with store._write() as conn:
        assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1


# ----------------------------------------------------------------------
# 3. X-Forwarded-For 默认不可信
# ----------------------------------------------------------------------
class _Req:
    """最小请求替身：只需 ``client.host`` 与 ``headers.get``。"""

    def __init__(self, host: str, forwarded: str | None = None):
        self.client = SimpleNamespace(host=host)
        self.headers = {"x-forwarded-for": forwarded} if forwarded else {}


def test_xff_is_ignored_by_default(monkeypatch):
    """默认不信任 XFF：伪造头改不了限流所用的客户端 IP。"""
    monkeypatch.setattr(deps, "get_config", lambda: SimpleNamespace(trusted_proxies=[]))
    assert deps._client_ip(_Req("203.0.113.7", forwarded="1.2.3.4")) == "203.0.113.7"


def test_xff_honored_only_from_trusted_proxy(monkeypatch):
    monkeypatch.setattr(
        deps, "get_config", lambda: SimpleNamespace(trusted_proxies=["10.0.0.1"])
    )
    # 来自可信代理：采信 XFF 第一跳（保留真实客户端 IP）
    assert deps._client_ip(_Req("10.0.0.1", forwarded="1.2.3.4, 10.0.0.1")) == "1.2.3.4"
    # 不是可信代理：即便带了 XFF 也忽略，仍用对端地址
    assert deps._client_ip(_Req("203.0.113.7", forwarded="1.2.3.4")) == "203.0.113.7"


def test_xff_wildcard_trusts_all(monkeypatch):
    monkeypatch.setattr(deps, "get_config", lambda: SimpleNamespace(trusted_proxies=["*"]))
    assert deps._client_ip(_Req("203.0.113.7", forwarded="1.2.3.4")) == "1.2.3.4"


# ----------------------------------------------------------------------
# 4. 限流器参数变更生效
# ----------------------------------------------------------------------
def test_limiter_rebuilt_when_config_changes():
    """改了 RATELIMIT_* 就该按新值限流，而不是继续用首次创建时的老参数。"""
    reset_limiters()
    try:
        first = get_limiter("t", capacity=2, per_minute=60)
        assert first.matches(capacity=2, per_minute=60)

        rebuilt = get_limiter("t", capacity=9, per_minute=600)
        assert rebuilt is not first
        assert rebuilt.matches(capacity=9, per_minute=600)

        # 参数没变则复用同一实例（不白白丢掉已有令牌状态）
        assert get_limiter("t", capacity=9, per_minute=600) is rebuilt
    finally:
        reset_limiters()


def test_limiter_enforces_capacity():
    reset_limiters()
    try:
        limiter = get_limiter("burst", capacity=2, per_minute=0)
        assert limiter.check("k")[0] is True
        assert limiter.check("k")[0] is True
        allowed, retry_after = limiter.check("k")
        assert allowed is False and retry_after > 0
    finally:
        reset_limiters()


# ----------------------------------------------------------------------
# 5. LLM 健康判定不会永久卡死
# ----------------------------------------------------------------------
def test_llm_health_flags_fresh_failure():
    monitor = LLMHealthMonitor()
    monitor.record_failure(RuntimeError("402 Insufficient Balance"))
    snapshot = monitor.snapshot()
    assert snapshot["ok"] is False
    assert snapshot["stale"] is False
    assert snapshot["quota_exhausted"] is True


def test_llm_health_recovers_after_stale_window():
    """失败记录过期（此后无任何调用）时，不再断言依赖不可用。

    否则一次偶发超时会让 ``/readyz`` 永久 503，把健康副本一直摘出负载均衡。
    """
    monitor = LLMHealthMonitor(stale_after_seconds=0.0)
    monitor.record_failure(RuntimeError("timeout"))
    snapshot = monitor.snapshot()
    assert snapshot["stale"] is True
    assert snapshot["ok"] is True
    assert monitor.quota_exhausted is False


def test_llm_health_recovers_after_success():
    monitor = LLMHealthMonitor()
    monitor.record_failure(RuntimeError("boom"))
    monitor.record_success()
    snapshot = monitor.snapshot()
    assert snapshot["ok"] is True and snapshot["stale"] is False


# ----------------------------------------------------------------------
# 6. 知识库变更后作废答案缓存
# ----------------------------------------------------------------------
def _report(*, uploaded=(), deleted=(), skipped=(), failed=()):
    return SimpleNamespace(
        uploaded=list(uploaded), deleted=list(deleted),
        skipped=list(skipped), failed=list(failed),
    )


def test_reingest_purges_answer_cache_when_changed(tmp_path, monkeypatch):
    store = AnswerCacheStore(tmp_path / "console.db")
    store.put("k", "送料はいくら？", "ja",
              ChatResult(question="送料はいくら？", answer="500円です"), ttl_seconds=3600)
    assert store.stats()["entries"] == 1

    monkeypatch.setattr(ops, "get_answer_cache", lambda: store)
    purged = ops._invalidate_answer_cache(_report(uploaded=["KB-0001.md"]))

    assert purged == 1
    assert store.stats()["entries"] == 0


def test_reingest_keeps_cache_when_nothing_changed(tmp_path, monkeypatch):
    """纯跳过的一轮不该把热缓存清空（否则白白丢掉缓存收益）。"""
    store = AnswerCacheStore(tmp_path / "console.db")
    store.put("k", "q", "ja", ChatResult(question="q", answer="a"), ttl_seconds=3600)

    monkeypatch.setattr(ops, "get_answer_cache", lambda: store)
    assert ops._invalidate_answer_cache(_report(skipped=["KB-0001.md"])) == 0
    assert store.stats()["entries"] == 1


def test_reingest_tolerates_disabled_cache(monkeypatch):
    """缓存关闭（get_answer_cache() 返回 None）时不能抛错。"""
    monkeypatch.setattr(ops, "get_answer_cache", lambda: None)
    assert ops._invalidate_answer_cache(_report(deleted=["old.md"])) == 0
