"""令牌与图形验证码的单元测试。"""
from __future__ import annotations

import time

import jwt
import pytest

from customer_support.auth.captcha import create_captcha, render_svg, verify_captcha
from customer_support.auth.tokens import (
    ACCESS_TOKEN_TYPE,
    REFRESH_TOKEN_TYPE,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from customer_support.common.exceptions import AuthError

SECRET = "unit-test-secret"


class TestTokens:
    def test_access_token_roundtrip(self):
        token, expires_in = create_access_token(
            user_id="u1", email="a@b.com", role="agent", secret=SECRET, ttl_minutes=30
        )
        payload = decode_token(token, secret=SECRET, expected_type=ACCESS_TOKEN_TYPE)
        assert payload["sub"] == "u1"
        assert payload["email"] == "a@b.com"
        assert payload["role"] == "agent"
        assert 1700 < expires_in <= 1800

    def test_refresh_token_carries_jti(self):
        token, jti, expires_at = create_refresh_token(user_id="u1", secret=SECRET, ttl_days=7)
        payload = decode_token(token, secret=SECRET, expected_type=REFRESH_TOKEN_TYPE)
        assert payload["jti"] == jti
        assert payload["sub"] == "u1"
        assert expires_at.timestamp() > time.time()

    def test_token_type_cannot_be_cross_used(self):
        """refresh 不能当 access 用——否则长期令牌会绕过短期限制。"""
        refresh, _, _ = create_refresh_token(user_id="u1", secret=SECRET)
        with pytest.raises(AuthError):
            decode_token(refresh, secret=SECRET, expected_type=ACCESS_TOKEN_TYPE)

    def test_tampered_signature_rejected(self):
        token, _ = create_access_token(user_id="u1", email="a@b.com", role="agent", secret=SECRET)
        forged = jwt.encode(
            {**jwt.decode(token, SECRET, algorithms=["HS256"]), "role": "admin"},
            "attacker-secret",
            algorithm="HS256",
        )
        with pytest.raises(AuthError):
            decode_token(forged, secret=SECRET, expected_type=ACCESS_TOKEN_TYPE)

    def test_expired_token_rejected(self):
        expired, _ = create_access_token(
            user_id="u1", email="a@b.com", role="agent", secret=SECRET, ttl_minutes=-1
        )
        with pytest.raises(AuthError) as exc:
            decode_token(expired, secret=SECRET, expected_type=ACCESS_TOKEN_TYPE)
        assert "过期" in exc.value.message


class TestCaptcha:
    def test_correct_answer_passes(self):
        code, captcha_id, svg = create_captcha(secret=SECRET, ttl_seconds=60)
        assert f">{code[0]}<" in svg
        assert verify_captcha(captcha_id, code, secret=SECRET)
        # 校验通过即作废（一次性），所以「大小写不敏感」要另开一张来验
        code2, captcha_id2, _ = create_captcha(secret=SECRET, ttl_seconds=60)
        assert verify_captcha(captcha_id2, code2.lower(), secret=SECRET), "应当大小写不敏感"

    def test_wrong_answer_fails(self):
        code, captcha_id, _ = create_captcha(secret=SECRET, ttl_seconds=60)
        wrong = "ZZZZ" if code != "ZZZZ" else "YYYY"
        assert not verify_captcha(captcha_id, wrong, secret=SECRET)
        assert not verify_captcha(captcha_id, "", secret=SECRET)

    def test_answer_not_derivable_from_id_and_no_replay(self):
        """替换旧用例 ``test_token_signed_with_other_secret_rejected``。

        旧实现的「防伪造」是建立在**答案就写在 captcha_id 里**的前提上的
        （用 HMAC 保护 payload），但 base64 不是加密——攻击者根本不需要伪造签名，
        decode 一下就读到答案了。现在 id 只是不含信息的随机句柄，
        真正要守的两条不变量是：①解不出答案 ②用过即废。完整用例见 test_captcha.py。
        """
        import base64

        code, captcha_id, _ = create_captcha(secret=SECRET, ttl_seconds=60)
        for part in captcha_id.split("."):
            padded = part + "=" * (-len(part) % 4)
            try:
                decoded = base64.urlsafe_b64decode(padded).decode()
            except (ValueError, UnicodeDecodeError):
                continue
            assert code not in decoded, f"captcha_id 泄露了答案: {decoded!r}"

        assert verify_captcha(captcha_id, code, secret=SECRET)
        assert not verify_captcha(captcha_id, code, secret=SECRET), "一次性：不可重放"

    def test_expired_captcha_rejected(self):
        code, captcha_id, _ = create_captcha(secret=SECRET, ttl_seconds=-1)
        assert not verify_captcha(captcha_id, code, secret=SECRET)

    def test_malformed_id_rejected(self):
        assert not verify_captcha("garbage", "ABCD", secret=SECRET)
        assert not verify_captcha("a.b.c", "ABCD", secret=SECRET)

    def test_svg_escapes_and_is_self_contained(self):
        svg = render_svg("<script>")
        assert "<script>" not in svg
        assert svg.startswith("<svg") and svg.endswith("</svg>")
