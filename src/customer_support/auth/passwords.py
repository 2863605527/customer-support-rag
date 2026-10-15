"""密码哈希与密码策略。

为什么用 bcrypt 而不是自己 sha256 加盐：
bcrypt 自带**自适应成本因子**（``rounds``），单次校验刻意慢（~100ms），
暴力破解成本随算力上涨；而 sha256 即使加盐也能被 GPU 每秒跑上亿次。
``gensalt(rounds=12)`` 是 OWASP 目前推荐的下限，本项目默认 12。

密码策略三件事：长度、复杂度（字母 + 数字）、拒绝弱口令/与邮箱相关的口令。
错误信息**逐条返回**，让前端能给出可执行的提示，而不是笼统的「密码不合法」。
"""
from __future__ import annotations

import re

import bcrypt

from customer_support.common.exceptions import ValidationError

# bcrypt 成本因子：12 ≈ 每次哈希 ~250ms（本地实测），足够拖慢离线爆破
BCRYPT_ROUNDS = 12

# bcrypt 算法本身只处理前 72 字节；超过必须显式拒绝，绝不能静默截断——
# 截断会让「前 72 字节相同」的两个不同密码互相通过校验。
BCRYPT_MAX_BYTES = 72

# 常见弱口令（小写比对）；生产可换成更大的字典或接 HIBP
_WEAK_PASSWORDS = {
    "password", "password1", "password123", "12345678", "123456789", "1234567890",
    "qwerty123", "qwertyuiop", "admin123", "administrator", "letmein123", "welcome123",
    "iloveyou", "abc123456", "11111111", "00000000", "admin@123", "p@ssw0rd",
    "deepseek123", "tiktok123", "shop123456", "customer123",
}

_REQUIRED_PATTERNS = (
    (re.compile(r"[A-Za-z]"), "至少包含一个字母"),
    (re.compile(r"\d"), "至少包含一个数字"),
)


def hash_password(password: str) -> str:
    """生成 bcrypt 哈希（含盐，可直接落库）。超长密码直接拒绝，不截断。"""
    if len(password.encode("utf-8")) > BCRYPT_MAX_BYTES:
        raise ValidationError(
            f"密码过长（超过 {BCRYPT_MAX_BYTES} 字节），请缩短后重试",
            detail="password exceeds bcrypt limit",
        )
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """校验密码。

    bcrypt 对超长输入会截断到 72 字节；这里显式拒绝超长而非静默截断，
    否则「前 72 字节相同」的两个密码会被判为同一个（安全隐患）。
    哈希格式非法时返回 False 而不是抛错——避免把内部状态泄露给调用方。
    """
    if len(password.encode("utf-8")) > BCRYPT_MAX_BYTES:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def validate_password_strength(password: str, *, email: str = "", min_length: int = 10) -> None:
    """密码策略校验，不通过则抛 ``ValidationError``（400）。"""
    problems: list[str] = []

    if len(password) < min_length:
        problems.append(f"长度至少 {min_length} 位")
    if len(password) > 128:
        problems.append("长度不能超过 128 位")
    if len(password.encode("utf-8")) > BCRYPT_MAX_BYTES:
        problems.append(f"长度不能超过 {BCRYPT_MAX_BYTES} 字节（多字节字符按字节计）")
    if password.strip() != password:
        problems.append("首尾不能有空白字符")

    for pattern, message in _REQUIRED_PATTERNS:
        if not pattern.search(password):
            problems.append(message)

    if len(set(password)) < 4:
        problems.append("字符过于单一（至少 4 个不同字符）")

    lowered = password.lower()
    if lowered in _WEAK_PASSWORDS:
        problems.append("属于常见弱口令，请更换")

    if email:
        local_part = email.split("@", 1)[0].lower()
        if local_part and len(local_part) >= 4 and local_part in lowered:
            problems.append("不能包含邮箱账号名")
        if lowered == email.lower():
            problems.append("不能与邮箱相同")

    if problems:
        raise ValidationError("密码不符合安全要求：" + "；".join(problems), detail="; ".join(problems))


def password_strength_score(password: str) -> int:
    """粗粒度强度评分（0-4），用于前端强度条与弱密码早筛。"""
    score = 0
    if len(password) >= 10:
        score += 1
    if len(password) >= 14:
        score += 1
    if re.search(r"[A-Za-z]", password) and re.search(r"\d", password):
        score += 1
    if re.search(r"[^A-Za-z0-9]", password):
        score += 1
    return min(score, 4)
