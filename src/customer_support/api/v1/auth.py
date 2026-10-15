"""鉴权接口：注册、登录、令牌刷新/登出、当前用户、账号与审计（管理员）。

客户端 IP 与 User-Agent 会随审计落库：排查「异地/异常登录」时是唯一线索，
因此统一从 ``Request`` 里取，并优先信任反向代理透传的 ``X-Forwarded-For`` 首个地址。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse

from customer_support.api.deps import get_auth_service, get_current_user, require_admin
from customer_support.auth.service import AuthService
from customer_support.common.logging import get_logger
from customer_support.models.user import (
    AuditEntry,
    CaptchaChallenge,
    ChangePasswordRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserPublic,
    UserRecord,
    record_to_public,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _user_agent(request: Request) -> str:
    return request.headers.get("user-agent", "")


@router.get(
    "/captcha",
    response_model=CaptchaChallenge,
    summary="获取图形验证码",
    description="失败次数达到阈值后登录必须携带验证码；验证码为无状态签名，服务端不存答案。",
)
def captcha(service: AuthService = Depends(get_auth_service)) -> CaptchaChallenge:
    return service.create_captcha()


@router.post(
    "/register",
    response_model=UserPublic,
    status_code=status.HTTP_201_CREATED,
    summary="注册账号",
    description="自助注册；密码需满足策略（长度、字母+数字、非弱口令）。注册后需登录获取令牌。",
)
def register(
    payload: RegisterRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),
) -> UserPublic:
    return service.register(
        email=str(payload.email),
        password=payload.password,
        full_name=payload.full_name,
        ip=_client_ip(request),
        user_agent=_user_agent(request),
    )


@router.post(
    "/login",
    response_model=TokenPair,
    summary="登录并获取令牌",
    description=(
        "返回 access_token（默认 60 分钟）与 refresh_token（默认 14 天，可撤销）。\n\n"
        "失败会返回结构化上下文：`remaining_attempts`（剩余尝试次数）、"
        "`captcha_required`（是否需图形验证码）、`locked_seconds`（锁定剩余秒数）。"
    ),
)
def login(
    payload: LoginRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),
) -> TokenPair:
    return service.login(
        email=str(payload.email),
        password=payload.password,
        captcha_id=payload.captcha_id,
        captcha_code=payload.captcha_code,
        ip=_client_ip(request),
        user_agent=_user_agent(request),
    )


@router.post(
    "/refresh",
    response_model=TokenPair,
    summary="刷新访问令牌",
    description="刷新即轮换：旧 refresh_token 立即作废，返回新的令牌对。",
)
def refresh(
    payload: RefreshRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),
) -> TokenPair:
    return service.refresh(
        refresh_token=payload.refresh_token,
        ip=_client_ip(request),
        user_agent=_user_agent(request),
    )


@router.post(
    "/logout",
    summary="登出",
    description="撤销 refresh_token（未提供时撤销该用户全部刷新令牌）。幂等，可重复调用。",
)
def logout(
    request: Request,
    payload: RefreshRequest | None = None,
    user: UserRecord = Depends(get_current_user),
    service: AuthService = Depends(get_auth_service),
) -> dict:
    service.logout(
        refresh_token=payload.refresh_token if payload else None,
        user=user,
        ip=_client_ip(request),
        user_agent=_user_agent(request),
    )
    return {"logged_out": True, "email": user.email}


@router.get("/me", response_model=UserPublic, summary="当前登录用户")
def me(user: UserRecord = Depends(get_current_user)) -> UserPublic:
    return record_to_public(user)


@router.post(
    "/change-password",
    summary="修改密码",
    description="需登录 + 原密码。改密成功后会**撤销该用户全部刷新令牌**（其他设备强制重新登录）。",
)
def change_password(
    payload: ChangePasswordRequest,
    user: UserRecord = Depends(get_current_user),
    service: AuthService = Depends(get_auth_service),
) -> dict:
    service.change_password(
        user_id=user.id, old_password=payload.old_password, new_password=payload.new_password
    )
    return {"changed": True, "email": user.email}


@router.get(
    "/users",
    response_model=list[UserPublic],
    summary="用户列表（管理员）",
)
def list_users(
    limit: int = Query(100, ge=1, le=500),
    admin: UserRecord = Depends(require_admin),
    service: AuthService = Depends(get_auth_service),
) -> list[UserPublic]:
    return service.list_users(admin=admin, limit=limit)


@router.get(
    "/audit-logs",
    response_model=list[AuditEntry],
    summary="登录审计日志（管理员）",
    description="包含注册、登录成功/失败、锁定、登出、刷新等事件，含 IP 与 UA，用于安全追溯。",
)
def audit_logs(
    limit: int = Query(100, ge=1, le=500),
    email: str | None = Query(None, description="按邮箱过滤"),
    admin: UserRecord = Depends(require_admin),
    service: AuthService = Depends(get_auth_service),
) -> list[AuditEntry] | JSONResponse:
    return service.list_audit(admin=admin, limit=limit, email=email)
