"""运营控制台 API 集成测试：工单闭环、会话复盘、反馈、指标、安全头与 /metrics。

依赖用 ``dependency_overrides`` 注入已登录用户（管理端接口需要 admin），
数据落在临时 SQLite，不碰开发者本机的 output/console。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from customer_support.api.app import create_app
from customer_support.api.deps import (
    get_chat_service,
    get_conversation_store,
    get_current_user,
    get_feedback_store,
    reset_singletons,
)
from customer_support.config import get_settings
from customer_support.models.feedback import FeedbackSubmission
from customer_support.models.message import ChatResult
from customer_support.models.ticket import Ticket
from customer_support.models.user import UserRecord
from customer_support.services.ticket import TicketService
from customer_support.store.feedback import FeedbackStore
from customer_support.store.tickets import TicketStore

pytestmark = pytest.mark.integration


class _StubChatService:
    """替身：跳过 LLM/RAGFlow，但**保留真实 ChatService 的落库副作用**。

    真实服务在产出回答后会把这一轮写进 ``ConversationStore``；本替身同样落库，
    这样控制台接口（会话列表/详情、运营指标）走的仍是生产同一条数据路径，
    而不是在测试里凭空造数据。
    """

    def handle(self, message):
        result = ChatResult(
            question=message.question,
            answer="綿100%の生地です。",
            intent="product_info",
            language=message.language or "ja",
            source="knowledge",
            confidence=0.9,
            session_id=message.session_id,
            trace_id="trace-stub",
            latency_ms=500,
        )
        if message.session_id:
            get_conversation_store().record_turn(
                session_id=message.session_id,
                question=message.question,
                result=result,
                buyer_id=message.buyer_id,
            )
        return result


def _user(role: str = "admin") -> UserRecord:
    return UserRecord(
        id=f"{role}-id",
        email=f"{role}@shop.jp",
        full_name=role,
        role=role,  # type: ignore[arg-type]
        password_hash="x",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_DB_PATH", str(tmp_path / "console.db"))
    monkeypatch.setenv("AUTH_USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setenv("AUTH_SEED_ADMIN_PASSWORD", "Seed-Admin-2026x")
    get_settings.cache_clear()
    reset_singletons()
    yield
    get_settings.cache_clear()
    reset_singletons()


def _client(role: str = "admin") -> TestClient:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: _user(role)
    app.dependency_overrides[get_chat_service] = lambda: _StubChatService()
    return TestClient(app)


@pytest.fixture
def ticket_svc(_isolated):
    return TicketService(TicketStore(get_settings().console.db_path))


def _seed_ticket(svc: TicketService, reason: str = "high_risk_intent") -> str:
    ticket = svc.create(
        Ticket(
            ticket_id=TicketService.new_ticket_id(),
            question="返金してください",
            language="ja",
            intent="human",
            confidence=0.9,
            reason=reason,
            sources=["products/KB-0001.md"],
        )
    )
    return ticket.ticket_id


class TestTicketApi:
    def test_list_and_filter(self, ticket_svc):
        _seed_ticket(ticket_svc, "high_risk_intent")
        _seed_ticket(ticket_svc, "no_knowledge")
        with _client() as c:
            body = c.get("/api/v1/ops/tickets").json()
            assert body["total"] == 2
            assert len(body["items"]) == 2

            filtered = c.get("/api/v1/ops/tickets", params={"reason": "no_knowledge"}).json()
            assert filtered["total"] == 1

    def test_detail_and_update_and_reply(self, ticket_svc):
        ticket_id = _seed_ticket(ticket_svc)
        with _client() as c:
            detail = c.get(f"/api/v1/ops/tickets/{ticket_id}").json()
            assert detail["ticket_id"] == ticket_id
            assert detail["priority"] == "high"  # 由原因推导的默认优先级

            patched = c.patch(
                f"/api/v1/ops/tickets/{ticket_id}",
                json={"status": "in_progress", "assignee": "agent@shop.jp"},
            ).json()
            assert patched["status"] == "in_progress"
            assert patched["assignee"] == "agent@shop.jp"

            replied = c.post(
                f"/api/v1/ops/tickets/{ticket_id}/replies",
                json={"body": "ご返金を承りました", "internal": False},
            ).json()
            assert len(replied["replies"]) == 1
            assert replied["first_response_at"] is not None

    def test_unknown_ticket_returns_404(self):
        with _client() as c:
            r = c.get("/api/v1/ops/tickets/T-nope")
            assert r.status_code == 404
            assert r.json()["error"]["code"] == "NOT_FOUND"

    def test_non_admin_cannot_update(self, ticket_svc):
        ticket_id = _seed_ticket(ticket_svc)
        with _client("agent") as c:
            assert c.get("/api/v1/ops/tickets").status_code == 200
            r = c.patch(f"/api/v1/ops/tickets/{ticket_id}", json={"status": "closed"})
            assert r.status_code == 403

    def test_stats_and_summary(self, ticket_svc):
        _seed_ticket(ticket_svc, "service_unavailable")
        with _client() as c:
            stats = c.get("/api/v1/ops/tickets/stats").json()
            assert stats["total"] == 1
            assert stats["backlog"] == 1
            summary = c.get("/api/v1/ops/tickets/summary").json()
            assert summary["total"] == 1


class TestConversationApi:
    def test_history_records_and_reads_back(self):
        with _client() as c:
            c.post("/api/v1/chat", json={"question": "素材は何ですか？", "session_id": "sess-1"})
            listing = c.get("/api/v1/ops/conversations").json()
            assert listing["total"] == 1
            assert listing["items"][0]["session_id"] == "sess-1"
            assert listing["items"][0]["last_question"] == "素材は何ですか？"

            detail = c.get("/api/v1/ops/conversations/sess-1").json()
            assert [m["role"] for m in detail["messages"]] == ["buyer", "agent"]

    def test_delete_requires_admin(self):
        with _client("agent") as c:
            assert c.delete("/api/v1/ops/conversations/x").status_code == 403


class TestFeedbackApi:
    def test_submit_and_stats(self):
        with _client() as c:
            r = c.post(
                "/api/v1/feedback",
                json={
                    "trace_id": "t-1",
                    "rating": "down",
                    "reasons": ["missing_product"],
                    "comment": "缺少尺寸信息",
                    "question": "サイズは？",
                    "answer": "不明です",
                    "source": "knowledge",
                },
            )
            assert r.status_code == 201
            stats = c.get("/api/v1/ops/feedback/stats").json()
            assert stats["total"] == 1
            assert stats["down"] == 1
            assert stats["by_reason"]["missing_product"] == 1

    def test_down_rating_requires_reason(self):
        with _client() as c:
            r = c.post("/api/v1/feedback", json={"trace_id": "t-2", "rating": "down", "reasons": []})
            assert r.status_code == 400
            assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_rating_rejected(self):
        with _client() as c:
            r = c.post("/api/v1/feedback", json={"trace_id": "t-3", "rating": "meh"})
            assert r.status_code == 422

    def test_dataset_export_admin_only(self):
        store: FeedbackStore = get_feedback_store()
        store.submit(
            FeedbackSubmission(trace_id="t-9", rating="up", reasons=["accurate"], question="q", answer="a")
        )
        with _client() as c:
            body = c.get("/api/v1/ops/feedback/dataset").json()
            assert body["total"] == 1
            assert body["samples"][0]["expected_ok"] is True
        with _client("agent") as c:
            assert c.get("/api/v1/ops/feedback/dataset").status_code == 403


class TestObservability:
    def test_analytics_overview(self):
        with _client() as c:
            c.post("/api/v1/chat", json={"question": "素材は何ですか？", "session_id": "s-a"})
            body = c.get("/api/v1/ops/analytics").json()
            assert body["conversation"]["answers"] == 1
            assert body["conversation"]["containment_rate"] == 1.0
            assert "llm" in body and "breakers" in body

    def test_metrics_prometheus_text(self):
        with _client() as c:
            r = c.get("/metrics")
            assert r.status_code == 200
            assert "cs_conversations" in r.text

    def test_metrics_token_enforced(self, monkeypatch):
        monkeypatch.setenv("METRICS_TOKEN", "secret-metrics")
        get_settings.cache_clear()
        reset_singletons()
        with _client() as c:
            assert c.get("/metrics").status_code == 401
            assert c.get("/metrics", headers={"X-Metrics-Token": "secret-metrics"}).status_code == 200

    def test_security_headers_present(self):
        with _client() as c:
            r = c.get("/healthz")
            assert r.headers["X-Content-Type-Options"] == "nosniff"
            assert r.headers["X-Frame-Options"] == "DENY"
            assert "Content-Security-Policy" in r.headers

    def test_readyz_reports_llm_health_block(self):
        with _client() as c:
            body = c.get("/readyz").json()
            assert "llm" in body["checks"]
            assert "quota_exhausted" in body["checks"]["llm"]
