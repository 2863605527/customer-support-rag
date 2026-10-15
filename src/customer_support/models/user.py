"""账号与鉴权相关的数据模型。

设计要点：

- **敏感字段永不出网**：``UserPublic`` 是唯一对外的用户视图，不含 ``password_hash`` /
  ``failed_attempts`` / ``locked_until`` 等内部状态；数据库实体 ``UserRecord`` 只在服务层流转。
- 请求模型的校验规则与后端策略保持单一来源：长度/字符要求写在 ``auth.passwords``，
  Pydantic 这里只做「形状」校验（必填、长度上限），避免两处规则打架。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

Role = Literal["admin", "agent"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UserRecord(BaseModel):
    """数据库中的用户实体（含敏感字段，仅服务层可见）。"""

    id: str
    email: str
    full_name: str = ""
    role: Role = "agent"
    password_hash: str
    is_active: bool = True
    created_at: datetime
    last_login_at: datetime | None = None
    failed_attempts: int = 0
    locked_until: datetime | None = None


class UserPublic(BaseModel):
    """对外的用户视图（绝不含密码哈希与锁定细节）。"""

    id: str
    email: str
    full_name: str = ""
    role: Role = "agent"
    is_active: bool = True
    created_at: datetime
    last_login_at: datetime | None = None


class RegisterRequest(BaseModel):
    """注册请求。"""

    email: EmailStr = Field(..., description="企业邮箱，登录名")
    password: str = Field(..., min_length=1, max_length=128, description="需满足密码策略")
    full_name: str = Field("", max_length=64, description="显示名")


class LoginRequest(BaseModel):
    """登录请求。"""

    email: EmailStr
    password: str = Field(..., min_length=1, max_length=128)
    captcha_id: str | None = Field(None, description="图形验证码 id（失败次数达阈值后必填）")
    captcha_code: str | None = Field(None, description="图形验证码答案")


class TokenPair(BaseModel):
    """令牌对：访问令牌短期，刷新令牌长期且可撤销。"""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(0, description="access_token 有效期（秒）")
    user: UserPublic


class RefreshRequest(BaseModel):
    refresh_token: str


class ChangePasswordRequest(BaseModel):
    """修改密码（需登录并提供原密码）。"""

    old_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=1, max_length=128)


class CaptchaChallenge(BaseModel):
    """图形验证码挑战：SVG 直接内联给前端，无外部请求。"""

    captcha_id: str
    image_svg: str
    expires_in: int


class AuditEntry(BaseModel):
    """登录审计日志条目（安全合规：谁在何时何地登录/失败）。"""

    id: int
    email: str
    user_id: str | None = None
    event: str
    ip: str = ""
    user_agent: str = ""
    detail: str = ""
    created_at: datetime


class LoginAuditEvent(str):
    """审计事件类型常量（避免散落字符串）。"""

    REGISTER = "register"
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILED = "login_failed"
    LOGIN_LOCKED = "login_locked"
    LOGOUT = "logout"
    REFRESH = "refresh"


AUDIT_EVENTS = {
    "register",
    "login_success",
    "login_failed",
    "login_locked",
    "logout",
    "refresh",
}


def record_to_public(record: UserRecord) -> UserPublic:
    """实体 → 对外视图。"""
    return UserPublic(
        id=record.id,
        email=record.email,
        full_name=record.full_name,
        role=record.role,
        is_active=record.is_active,
        created_at=record.created_at,
        last_login_at=record.last_login_at,
    )


def utcnow() -> datetime:
    return _utcnow()
