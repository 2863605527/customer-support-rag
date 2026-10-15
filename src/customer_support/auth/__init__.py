"""鉴权模块：账号、密码策略、令牌与登录审计。"""
from customer_support.auth.captcha import create_captcha, render_svg, verify_captcha
from customer_support.auth.passwords import (
    hash_password,
    password_strength_score,
    validate_password_strength,
    verify_password,
)
from customer_support.auth.service import AuthService
from customer_support.auth.store import UserStore
from customer_support.auth.tokens import (
    ACCESS_TOKEN_TYPE,
    REFRESH_TOKEN_TYPE,
    create_access_token,
    create_refresh_token,
    decode_token,
)

__all__ = [
    "ACCESS_TOKEN_TYPE",
    "REFRESH_TOKEN_TYPE",
    "AuthService",
    "UserStore",
    "create_access_token",
    "create_captcha",
    "create_refresh_token",
    "decode_token",
    "hash_password",
    "password_strength_score",
    "render_svg",
    "validate_password_strength",
    "verify_captcha",
    "verify_password",
]
