"""API 层集成测试：探针与 chat/reset 端点（依赖用假服务覆盖，不碰外部依赖）。

注意：``/api/v1/*`` 自引入鉴权后**默认需要登录**。本文件聚焦业务逻辑，
因此用 ``dependency_overrides`` 直接注入一个已登录用户；鉴权本身的正确性
（401/403、锁定、验证码、令牌轮换）由 ``tests/integration/test_auth_api.py`` 覆盖。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from customer_support.api.app import create_app
from customer_support.api.deps import (
    get_chat_service,
    get_current_user,
    get_session_store,
    reset_singletons,
)
from customer_support.config import get_settings
from customer_support.models.message import ChatResult
from customer_support.models.user import UserRecord
from customer_support.services.session import SessionStore, Turn

pytestmark = pytest.mark.integration


class _StubChatService:
    def handle(self, message):
        return ChatResult(
            question=message.question,
            answer="綿100%の生地です。",
            intent="product_info",
            need_human=False,
            language=message.language or "ja",
            source="knowledge",
            confidence=0.9,
            session_id=message.session_id,
            trace_id="testtrace",
        )

    def handle_stream(self, message):
        yield {"type": "status", "stage": "intent"}
        for piece in ("綿100", "%の生地", "です。"):
            yield {"type": "delta", "text": piece}
        yield {"type": "done", "result": self.handle(message)}


def _logged_in_user() -> UserRecord:
    """受保护接口的测试替身：无需真实令牌即可进入业务逻辑。"""
    return UserRecord(
        id="test-user",
        email="tester@example.com",
        full_name="Tester",
        role="agent",
        password_hash="not-used-in-this-test",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture(autouse=True)
def _isolated_user_db(tmp_path, monkeypatch):
    """让所有 API 测试都指向临时用户库，不碰开发者本机的 output/auth/users.db。"""
    monkeypatch.setenv("AUTH_USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setenv("AUTH_SEED_ADMIN_PASSWORD", "Seed-Admin-2026x")
    get_settings.cache_clear()
    reset_singletons()
    yield
    get_settings.cache_clear()
    reset_singletons()


@pytest.fixture
def client():
    app = create_app()
    store = SessionStore(ttl_seconds=60, max_turns=4)
    store.append("keep", Turn(question="q"))
    app.dependency_overrides[get_chat_service] = lambda: _StubChatService()
    app.dependency_overrides[get_session_store] = lambda: store
    app.dependency_overrides[get_current_user] = _logged_in_user
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def offline_client(monkeypatch):
    """无凭据环境下的 client。

    就绪探针检查的是「依赖是否可用」，其行为**不应**取决于开发者本机 `.env`：
    这里显式清空 LLM / RAGFlow 密钥并清空配置单例，既保证测试可重复，
    也避免探针真的打到本机 RAGFlow（RagflowBackend 无 key 时立刻抛 ConfigError，不发网络请求）。
    """
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("RAGFLOW_API_KEY", "")
    get_settings.cache_clear()
    reset_singletons()

    app = create_app()
    with TestClient(app) as c:
        yield c

    get_settings.cache_clear()
    reset_singletons()


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_readyz_reports_not_ready_without_credentials(offline_client):
    """没有 LLM / RAGFlow 凭据时，就绪探针必须返回 503 并说明原因。"""
    r = offline_client.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["ready"] is False
    assert "checks" in body
    assert body["checks"]["llm_configured"] is False


def test_chat_endpoint(client):
    r = client.post("/api/v1/chat", json={"question": "素材は何ですか？", "session_id": "s1"})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "綿100%の生地です。"
    assert body["need_human"] is False
    assert body["source"] == "knowledge"
    assert body["trace_id"]


def test_chat_validates_empty_question(client):
    r = client.post("/api/v1/chat", json={"question": ""})
    assert r.status_code == 422


def test_chat_stream_emits_sse_frames(client):
    """/chat/stream 必须以 SSE 分帧：status → delta* → done（终态含完整 ChatResult）。"""
    r = client.post("/api/v1/chat/stream", json={"question": "素材は何ですか？", "session_id": "s1"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    events: list[tuple[str, dict]] = []
    for frame in r.text.split("\n\n"):
        if not frame.strip():
            continue
        event = ""
        data = ""
        for line in frame.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = line[len("data:"):].strip()
        assert event, "每帧都必须带 event 名"
        events.append((event, json.loads(data)))

    types = [e for e, _ in events]
    assert types[0] == "status"
    assert types[-1] == "done"
    assert types.count("delta") == 3

    deltas = "".join(d["text"] for t, d in events if t == "delta")
    assert deltas == "綿100%の生地です。"
    done_result = events[-1][1]["result"]
    assert done_result["answer"] == "綿100%の生地です。"
    assert done_result["trace_id"]


def test_chat_stream_requires_auth(offline_client):
    """/chat/stream 与 /chat 同受鉴权保护：无令牌必须是业务结构的 401，而非断流。"""
    r = offline_client.post("/api/v1/chat/stream", json={"question": "q"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_ERROR"


def test_trace_id_header_echoed(client):
    r = client.get("/healthz", headers={"X-Trace-Id": "abc123"})
    assert r.headers.get("X-Trace-Id") == "abc123"


def test_reset_session(client):
    r = client.post("/api/v1/sessions/keep/reset")
    assert r.status_code == 200
    assert r.json() == {"session_id": "keep", "cleared": True}


def test_ops_sessions_stats(client):
    r = client.get("/api/v1/ops/sessions/stats")
    assert r.status_code == 200
    assert r.json()["sessions"] == 1


def test_unknown_api_path_returns_json_not_html(client):
    """未匹配的 API 路径必须回 JSON 404——不能把前端 index.html 丢给调用方。"""
    r = client.get("/api/v1/definitely-not-here")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[2] / "frontend" / "dist" / "index.html").exists(),
    reason="前端未构建（frontend/dist 不存在）时无 SPA 回退可测",
)
def test_spa_fallback_serves_index_for_frontend_route(client):
    """前端用 history 路由：/login 在服务端没有文件，必须回退到 index.html，不能 404。"""
    r = client.get("/login")
    assert r.status_code == 200
    assert "<div id=\"app\">" in r.text
