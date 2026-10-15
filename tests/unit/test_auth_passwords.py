"""密码策略与哈希的单元测试（不依赖任何外部服务）。"""
from __future__ import annotations

import pytest

from customer_support.auth.passwords import (
    hash_password,
    password_strength_score,
    validate_password_strength,
    verify_password,
)
from customer_support.common.exceptions import ValidationError


class TestHashing:
    def test_hash_is_salted_and_verifiable(self):
        h1 = hash_password("Str0ng-Passw0rd!")
        h2 = hash_password("Str0ng-Passw0rd!")
        assert h1 != h2, "相同密码两次哈希必须不同（盐随机）"
        assert h1.startswith("$2b$"), "必须是 bcrypt 格式"
        assert verify_password("Str0ng-Passw0rd!", h1)
        assert verify_password("Str0ng-Passw0rd!", h2)

    def test_wrong_password_rejected(self):
        h = hash_password("Str0ng-Passw0rd!")
        assert not verify_password("Str0ng-Passw0rd", h)
        assert not verify_password("", h)

    def test_over_long_password_rejected_not_truncated(self):
        # bcrypt 只取前 72 字节：若不显式拒绝，"A"*80 与 "A"*72 会被判为同一个密码
        over_long = "A" * 80 + "1"
        with pytest.raises(ValidationError):
            hash_password(over_long)
        # 校验路径同样拒绝（而不是截断后通过）
        h = hash_password("A" * 60 + "1")
        assert not verify_password("A" * 60 + "1" + "B" * 20, h)

    def test_invalid_hash_returns_false_without_raising(self):
        assert not verify_password("whatever123", "not-a-bcrypt-hash")


class TestPolicy:
    @pytest.mark.parametrize(
        "password",
        [
            "Short1",  # 太短
            "alllowercaseletters",  # 无数字
            "1234567890123",  # 无字母
            "password",  # 弱口令
            "aaaaaaaaaa1",  # 字符过于单一
            "admin@kurashi-bedding.jp",  # 等于邮箱
            "admin-secret-2026",  # 含邮箱账号名（local part = admin）
            " Passw0rdLong ",  # 首尾空白
            "Pässwörd-" * 12,  # 超过 bcrypt 72 字节限制
        ],
    )
    def test_rejects_weak_passwords(self, password: str):
        with pytest.raises(ValidationError):
            validate_password_strength(password, email="admin@kurashi-bedding.jp")

    @pytest.mark.parametrize(
        "password",
        ["Str0ng-Passw0rd!", "Kurashi2026Support", "tiktok-shop-9x7Q"],
    )
    def test_accepts_strong_passwords(self, password: str):
        validate_password_strength(password, email="agent@kurashi-bedding.jp")

    def test_error_message_lists_all_problems(self):
        with pytest.raises(ValidationError) as exc:
            validate_password_strength("abc", email="abc@example.com")
        message = exc.value.message
        assert "长度至少" in message
        assert "数字" in message

    def test_score_grows_with_entropy(self):
        assert password_strength_score("abc") < password_strength_score("Abcdefgh1!")
        assert password_strength_score("Abcdefgh1!") <= 4
