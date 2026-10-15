"""鉴权接口集成测试：注册、登录、锁定、验证码升级、令牌轮换、受保护接口。

关键点：全程使用**临时 SQLite 用户库**（``tmp_path``），既不污染开发者的
``output/auth/users.db``，也保证每个用例从干净状态开始。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from customer_support.api.app import create_app
from customer_support.api.deps import reset_singletons
from customer_support.config import get_settings

pytestmark = pytest.mark.integration

STRONG_PASSWORD = "Str0ng-Passw0rd!"
SEED_ADMIN_PASSWORD = "Seed-Admin-2026x"


@pytest.fixture
def auth_client(tmp_path, monkeypatch):
    """独立用户库 + 收紧的锁定策略（3 次失败锁定、2 次起要验证码）。"""
    monkeypatch.setenv("AUTH_USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setenv("AUTH_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AUTH_SEED_ADMIN_PASSWORD", SEED_ADMIN_PASSWORD)
    monkeypatch.setenv("AUTH_MAX_FAILED_ATTEMPTS", "3")
    monkeypatch.setenv("AUTH_CAPTCHA_AFTER_FAILURES", "2")
    monkeypatch.setenv("AUTH_LOCKOUT_MINUTES", "1")
    monkeypatch.setenv("AUTH_ALLOWED_EMAIL_DOMAINS", "")
    get_settings.cache_clear()
    reset_singletons()

    app = create_app()
    with TestClient(app) as c:
        yield c

    get_settings.cache_clear()
    reset_singletons()


def _register(client: TestClient, email: str = "agent@example.com", password: str = STRONG_PASSWORD):
    return client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": "客服小张"},
    )


def _login(client: TestClient, email: str, password: str, **extra):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password, **extra})


def _login_ok(client: TestClient, email: str, password: str) -> str:
    r = _login(client, email, password)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _attempt(client: TestClient, email: str, password: str):
    """一次登录尝试；若后端要求验证码则自动补上（模拟真人）。

    说明：**验证码错误不计入密码失败次数**（否则看不清验证码的正常用户会被锁号），
    所以测试脚本要能过关验证码，才能走到锁定逻辑。
    """
    r = _login(client, email, password)
    if r.status_code == 401 and r.json()["error"].get("context", {}).get("captcha_required"):
        challenge = client.get("/api/v1/auth/captcha").json()
        r = _login(
            client,
            email,
            password,
            captcha_id=challenge["captcha_id"],
            captcha_code=_solve_captcha(challenge["captcha_id"]),
        )
    return r


class TestRegistration:
    def test_register_returns_public_user_without_secrets(self, auth_client):
        r = _register(auth_client)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["email"] == "agent@example.com"
        assert body["role"] == "agent"
        assert "password_hash" not in body and "password" not in body

    def test_duplicate_email_conflicts(self, auth_client):
        _register(auth_client)
        r = _register(auth_client)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "CONFLICT"

    @pytest.mark.parametrize(
        "payload",
        [
            {"email": "not-an-email", "password": STRONG_PASSWORD},
            {"email": "weak@example.com", "password": "12345678"},
            {"email": "weak@example.com", "password": "password"},
        ],
    )
    def test_invalid_inputs_rejected(self, auth_client, payload):
        r = auth_client.post("/api/v1/auth/register", json=payload)
        assert r.status_code in {400, 422}

    def test_password_policy_message_is_actionable(self, auth_client):
        r = auth_client.post(
            "/api/v1/auth/register", json={"email": "weak@example.com", "password": "abcdefghij"}
        )
        assert r.status_code == 400
        assert "数字" in r.json()["error"]["message"]


class TestLogin:
    def test_login_success_returns_token_pair(self, auth_client):
        _register(auth_client)
        r = _login(auth_client, "agent@example.com", STRONG_PASSWORD)
        assert r.status_code == 200
        body = r.json()
        assert body["token_type"] == "bearer"
        assert body["expires_in"] > 0
        assert body["access_token"] and body["refresh_token"]
        assert body["user"]["email"] == "agent@example.com"

    def test_wrong_password_returns_401_with_remaining_attempts(self, auth_client):
        _register(auth_client)
        r = _login(auth_client, "agent@example.com", "Wr0ng-Passw0rd!")
        assert r.status_code == 401
        context = r.json()["error"]["context"]
        assert context["remaining_attempts"] == 2
        assert context["captcha_required"] is False

    def test_captcha_failure_does_not_count_as_password_failure(self, auth_client):
        """设计取舍：验证码答错不计入锁定计数，避免正常用户被误锁。"""
        _register(auth_client)
        for _ in range(2):
            _login(auth_client, "agent@example.com", "Wr0ng-Passw0rd!")
        # 此时已要求验证码；提交错误验证码
        r = _login(auth_client, "agent@example.com", STRONG_PASSWORD, captcha_id="bad", captcha_code="XXXX")
        assert r.status_code == 401
        assert "验证码" in r.json()["error"]["message"]
        # 失败计数未被验证码失败推高（仍是 2，尚未锁定）
        assert r.json()["error"]["context"]["failed_attempts"] == 2

    def test_unknown_account_uses_same_message_as_wrong_password(self, auth_client):
        """防用户枚举：两种情况文案必须一致。"""
        r_unknown = _login(auth_client, "nobody@example.com", STRONG_PASSWORD)
        _register(auth_client)
        r_wrong = _login(auth_client, "agent@example.com", "Wr0ng-Passw0rd!")
        assert r_unknown.status_code == r_wrong.status_code == 401
        assert r_unknown.json()["error"]["message"] == r_wrong.json()["error"]["message"]

    def test_captcha_required_after_threshold_and_blocks_without_it(self, auth_client):
        _register(auth_client)
        _login(auth_client, "agent@example.com", "Wr0ng-Passw0rd!")  # 1
        r2 = _login(auth_client, "agent@example.com", "Wr0ng-Passw0rd!")  # 2 -> 达阈值
        assert r2.json()["error"]["context"]["captcha_required"] is True

        # 即使密码正确，缺验证码也必须拒绝
        r3 = _login(auth_client, "agent@example.com", STRONG_PASSWORD)
        assert r3.status_code == 401
        assert "验证码" in r3.json()["error"]["message"]

        # 带上正确验证码即可登录
        challenge = auth_client.get("/api/v1/auth/captcha").json()
        code = _solve_captcha(challenge["captcha_id"])
        r4 = _login(
            auth_client,
            "agent@example.com",
            STRONG_PASSWORD,
            captcha_id=challenge["captcha_id"],
            captcha_code=code,
        )
        assert r4.status_code == 200, r4.text

    def test_lockout_after_max_attempts(self, auth_client):
        _register(auth_client)
        for _ in range(3):
            _attempt(auth_client, "agent@example.com", "Wr0ng-Passw0rd!")
        r = _login(auth_client, "agent@example.com", STRONG_PASSWORD)
        assert r.status_code == 429
        assert "锁定" in r.json()["error"]["message"]
        assert r.headers.get("Retry-After")

    def test_seed_admin_can_login(self, auth_client):
        token = _login_ok(auth_client, "admin@kurashi-bedding.jp", SEED_ADMIN_PASSWORD)
        r = auth_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.json()["role"] == "admin"


class TestProtectedRoutes:
    def test_chat_requires_authentication(self, auth_client):
        r = auth_client.post("/api/v1/chat", json={"question": "返品できますか？"})
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "AUTH_ERROR"

    def test_ops_requires_authentication(self, auth_client):
        assert auth_client.get("/api/v1/ops/tickets/summary").status_code == 401

    def test_invalid_token_rejected(self, auth_client):
        r = auth_client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-token"})
        assert r.status_code == 401

    def test_me_returns_current_user(self, auth_client):
        _register(auth_client)
        token = _login_ok(auth_client, "agent@example.com", STRONG_PASSWORD)
        r = auth_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["email"] == "agent@example.com"

    def test_probes_stay_public(self, auth_client):
        """探针必须免鉴权，否则 K8s/Docker 探针全挂。"""
        assert auth_client.get("/healthz").status_code == 200
        assert auth_client.get("/readyz").status_code in {200, 503}


class TestRefreshAndLogout:
    def test_refresh_rotates_and_invalidates_old_token(self, auth_client):
        _register(auth_client)
        tokens = _login(auth_client, "agent@example.com", STRONG_PASSWORD).json()

        r = auth_client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        assert r.status_code == 200
        rotated = r.json()
        assert rotated["refresh_token"] != tokens["refresh_token"]

        # 旧刷新令牌已轮换作废（防重放）
        again = auth_client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        assert again.status_code == 401

    def test_logout_revokes_refresh_token(self, auth_client):
        _register(auth_client)
        tokens = _login(auth_client, "agent@example.com", STRONG_PASSWORD).json()
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}

        assert auth_client.post(
            "/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}, headers=headers
        ).status_code == 200
        assert auth_client.post(
            "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        ).status_code == 401

    def test_access_token_type_cannot_be_used_as_refresh(self, auth_client):
        _register(auth_client)
        tokens = _login(auth_client, "agent@example.com", STRONG_PASSWORD).json()
        r = auth_client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["access_token"]})
        assert r.status_code == 401


class TestAdminEndpoints:
    def test_agent_cannot_list_users(self, auth_client):
        _register(auth_client)
        token = _login_ok(auth_client, "agent@example.com", STRONG_PASSWORD)
        r = auth_client.get("/api/v1/auth/users", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403

    def test_admin_can_list_users_and_audit(self, auth_client):
        _register(auth_client)
        _login(auth_client, "agent@example.com", "Wr0ng-Passw0rd!")
        token = _login_ok(auth_client, "admin@kurashi-bedding.jp", SEED_ADMIN_PASSWORD)
        headers = {"Authorization": f"Bearer {token}"}

        users = auth_client.get("/api/v1/auth/users", headers=headers)
        assert users.status_code == 200
        emails = {u["email"] for u in users.json()}
        assert {"agent@example.com", "admin@kurashi-bedding.jp"} <= emails

        audit = auth_client.get("/api/v1/auth/audit-logs?limit=50", headers=headers)
        assert audit.status_code == 200
        events = {e["event"] for e in audit.json()}
        assert {"register", "login_success", "login_failed"} <= events

    def test_change_password_revokes_existing_refresh_tokens(self, auth_client):
        _register(auth_client)
        tokens = _login(auth_client, "agent@example.com", STRONG_PASSWORD).json()
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}

        r = auth_client.post(
            "/api/v1/auth/change-password",
            json={"old_password": STRONG_PASSWORD, "new_password": "N3w-Passw0rd-2026!"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        # 改密后旧刷新令牌立即失效
        assert auth_client.post(
            "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        ).status_code == 401
        # 新密码可登录
        assert _login(auth_client, "agent@example.com", "N3w-Passw0rd-2026!").status_code == 200


def _solve_captcha(captcha_id: str) -> str:
    """测试辅助：取某张验证码的答案。

    修复前这里是从 ``captcha_id`` 里 base64 解出答案的——而那正是漏洞本身
    （任何客户端都能照做，「防脚本撞库」形同虚设）。现在答案不再离开服务端，
    测试因为与生产同进程，改为直接读服务端的验证码表。
    """
    from customer_support.auth import captcha

    code = captcha.peek_code(captcha_id)
    assert code, f"captcha_id 未知或已过期: {captcha_id}"
    return code
