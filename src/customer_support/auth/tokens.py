"""JWT 令牌签发与校验。

设计取舍：

- **access / refresh 分离**：access 短期（默认 60 分钟）无状态、随请求带；refresh 长期
  （默认 14 天）但**落库可撤销**（登出、改密、检测到盗用时可立即失效）。
- 令牌里带 ``typ`` 声明，**access 与 refresh 不可混用**——只带 ``sub`` 的话，
  攻击者可以把 refresh token 当 access token 用，绕过短期限制。
- 带 ``jti``（唯一 id）便于对单个刷新令牌做撤销与轮换检测。
- 过期时间由 ``exp`` 声明承载，由 PyJWT 校验；同时把 ``exp`` 返回给调用方做前端倒计时。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import jwt

from customer_support.common.exceptions import AuthError

ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _encode(payload: dict, *, secret: str, algorithm: str) -> str:
    return jwt.encode(payload, secret, algorithm=algorithm)


def create_access_token(
    *,
    user_id: str,
    email: str,
    role: str,
    secret: str,
    algorithm: str = "HS256",
    ttl_minutes: int = 60,
) -> tuple[str, int]:
    """签发访问令牌，返回 ``(token, expires_in_seconds)``。"""
    now = _now()
    expires_at = now + timedelta(minutes=ttl_minutes)
    token = _encode(
        {
            "sub": user_id,
            "email": email,
            "role": role,
            "typ": ACCESS_TOKEN_TYPE,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "jti": uuid.uuid4().hex,
        },
        secret=secret,
        algorithm=algorithm,
    )
    return token, int((expires_at - now).total_seconds())


def create_refresh_token(
    *,
    user_id: str,
    secret: str,
    algorithm: str = "HS256",
    ttl_days: int = 14,
) -> tuple[str, str, datetime]:
    """签发刷新令牌，返回 ``(token, jti, expires_at)``，调用方需把 jti 落库以便撤销。"""
    now = _now()
    expires_at = now + timedelta(days=ttl_days)
    jti = uuid.uuid4().hex
    token = _encode(
        {
            "sub": user_id,
            "typ": REFRESH_TOKEN_TYPE,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "jti": jti,
        },
        secret=secret,
        algorithm=algorithm,
    )
    return token, jti, expires_at


def decode_token(token: str, *, secret: str, algorithm: str = "HS256", expected_type: str) -> dict:
    """解码并校验令牌；类型不符、过期、签名错误一律抛 ``AuthError``(401)。"""
    try:
        payload = jwt.decode(token, secret, algorithms=[algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("登录状态已过期，请重新登录", detail="token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("登录凭证无效", detail=f"invalid token: {exc}") from exc

    if payload.get("typ") != expected_type:
        raise AuthError("登录凭证类型不匹配", detail=f"expected {expected_type}, got {payload.get('typ')}")
    if not payload.get("sub"):
        raise AuthError("登录凭证缺少主体信息", detail="missing sub")
    return payload


def token_ttl_seconds(*, ttl_minutes: int = 60) -> int:
    return ttl_minutes * 60
