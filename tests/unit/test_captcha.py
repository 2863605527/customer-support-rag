"""验证码单测：答案不回传、一次性、限次、过期。

这些用例守的是一个**真实修过的漏洞**：旧实现是
``captcha_id = base64(过期时间.答案.随机数) + HMAC 签名``，
而 **base64 不是加密**——任何客户端 decode 一下就能直接读出答案，
「防脚本撞库」形同虚设；而且同一个 id 在有效期内可以无限次重复提交。

修复后答案只存在服务端的进程内表里，本文件正是它的回归防线。
"""
from __future__ import annotations

import base64

import pytest

from customer_support.auth import captcha


@pytest.fixture(autouse=True)
def _clean_store():
    captcha.reset_store()
    yield
    captcha.reset_store()


def _wrong_answer(code: str) -> str:
    """构造一个确保与正确答案不同的串（避免测试偶发假通过）。"""
    return "".join("A" if ch != "A" else "B" for ch in code)


class TestAnswerNotLeaked:
    def test_answer_is_not_recoverable_from_captcha_id(self):
        code, captcha_id, _svg = captcha.create_captcha(secret="s", ttl_seconds=300)

        # 旧实现下这一条会失败：拼接串就是 base64(exp.code.nonce)
        for part in captcha_id.split("."):
            padded = part + "=" * (-len(part) % 4)
            try:
                decoded = base64.urlsafe_b64decode(padded).decode()
            except (ValueError, UnicodeDecodeError):
                continue
            assert code not in decoded, f"captcha_id 泄露了答案: {decoded!r}"

    def test_unknown_or_empty_input_rejected(self):
        assert captcha.verify_captcha("nonexistent-id", "ABCD", secret="s") is False
        assert captcha.verify_captcha("", "ABCD", secret="s") is False
        assert captcha.verify_captcha("some-id", "", secret="s") is False


class TestLifecycle:
    def test_success_is_one_time(self):
        code, cid, _ = captcha.create_captcha(secret="s", ttl_seconds=300)
        assert captcha.verify_captcha(cid, code, secret="s") is True
        # 用掉即焚：同一个 id 不能再过（防重放）
        assert captcha.verify_captcha(cid, code, secret="s") is False

    def test_case_insensitive_and_whitespace_tolerant(self):
        code, cid, _ = captcha.create_captcha(secret="s")
        assert captcha.verify_captcha(cid, f"  {code.lower()}  ", secret="s") is True

    def test_id_is_burned_after_max_attempts(self):
        code, cid, _ = captcha.create_captcha(secret="s")
        bad = _wrong_answer(code)
        for _ in range(captcha.MAX_ATTEMPTS):
            assert captcha.verify_captcha(cid, bad, secret="s") is False
        # 超过限次即作废：连正确答案也不再接受（防穷举）
        assert captcha.verify_captcha(cid, code, secret="s") is False

    def test_expired_rejected(self):
        code, cid, _ = captcha.create_captcha(secret="s", ttl_seconds=0)
        assert captcha.verify_captcha(cid, code, secret="s") is False

    def test_peek_code_reads_server_side_store(self):
        code, cid, _ = captcha.create_captcha(secret="s")
        assert captcha.peek_code(cid) == code
        assert captcha.peek_code("unknown") is None

    def test_codes_are_unique_enough(self):
        codes = {captcha.create_captcha(secret="s")[0] for _ in range(40)}
        assert len(codes) > 1, "答案不应固定"
