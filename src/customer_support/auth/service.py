"""鉴权业务服务：注册、登录、令牌轮换、登出、账号管理等安全策略的**唯一实现处**。

安全策略一览（都可配，见 ``AuthSettings``）：

| 策略 | 做法 | 防的是什么 |
|---|---|---|
| 密码存储 | bcrypt(cost=12) 加盐哈希 | 库被拖走也拿不到明文 |
| 密码强度 | 长度 ≥10 + 字母数字 + 拒弱口令/邮箱相关 | 弱口令撞库 |
| 登录失败计数 | 连续 5 次锁定 15 分钟 | 暴力破解 |
| 图形验证码 | 失败满 3 次起强制校验（无状态签名） | 脚本化撞库 |
| 用户枚举防护 | 账号不存在与密码错误返回**同一句**提示 | 探测哪些邮箱已注册 |
| 令牌 | access 60min / refresh 14d 且落库可撤销、刷新即轮换 | 令牌泄露后长期可用 |
| 审计 | 每次注册/登录成功/失败/锁定/登出/刷新都落库 | 事后追溯与合规 |
| 账号状态 | ``is_active`` 停用即拒绝 | 离职账号回收 |
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from customer_support.auth import captcha, passwords, tokens
from customer_support.auth.store import UserStore
from customer_support.common.exceptions import (
    AuthError,
    ConflictError,
    ForbiddenError,
    RateLimitedError,
    ValidationError,
)
from customer_support.common.logging import get_logger
from customer_support.config import AuthSettings
from customer_support.models.user import (
    AuditEntry,
    CaptchaChallenge,
    LoginAuditEvent,
    TokenPair,
    UserPublic,
    UserRecord,
    record_to_public,
)

logger = get_logger(__name__)

# 账号不存在与密码错误共用同一句，避免「邮箱是否已注册」被枚举
_INVALID_CREDENTIALS = "邮箱或密码不正确"

DEFAULT_SEED_ADMIN_PASSWORD_BYTES = 12


class AuthService:
    """账号与令牌的业务编排。"""

    def __init__(self, store: UserStore, settings: AuthSettings):
        self.store = store
        self.settings = settings

    # ---------------- 初始化 ----------------
    def ensure_seed_admin(self) -> UserRecord:
        """首次启动（用户表为空）时创建管理员账号。

        密码来源：``AUTH_SEED_ADMIN_PASSWORD``；未配置则随机生成并**写进日志**，
        同时强制标记为需要尽快修改（日志里明确提示）。
        """
        existing = self.store.get_user_by_email(self.settings.seed_admin_email)
        if existing:
            return existing

        raw_password = self.settings.seed_admin_password
        generated = False
        if not raw_password:
            raw_password = secrets.token_urlsafe(DEFAULT_SEED_ADMIN_PASSWORD_BYTES)
            generated = True

        record = UserRecord(
            id=uuid.uuid4().hex,
            email=self.settings.seed_admin_email.lower(),
            full_name="系统管理员",
            role="admin",
            password_hash=passwords.hash_password(raw_password),
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        try:
            self.store.create_user(record)
        except Exception:  # noqa: BLE001 - 并发启动时另一进程可能已创建
            existing = self.store.get_user_by_email(self.settings.seed_admin_email)
            if existing:
                return existing
            raise

        self.store.add_audit(
            email=record.email,
            user_id=record.id,
            event=LoginAuditEvent.REGISTER,
            detail="seed admin created",
        )
        if generated:
            logger.warning(
                "已创建初始管理员账号 %s，随机密码：%s —— 请登录后立即修改，或用 AUTH_SEED_ADMIN_PASSWORD 指定。",
                record.email,
                raw_password,
            )
        else:
            logger.info("已创建初始管理员账号 %s（密码来自 AUTH_SEED_ADMIN_PASSWORD）", record.email)
        return record

    # ---------------- 验证码 ----------------
    def create_captcha(self) -> CaptchaChallenge:
        _code, captcha_id, svg = captcha.create_captcha(
            secret=self.settings.secret_key, ttl_seconds=self.settings.captcha_ttl_seconds
        )
        return CaptchaChallenge(
            captcha_id=captcha_id, image_svg=svg, expires_in=self.settings.captcha_ttl_seconds
        )

    # ---------------- 注册 ----------------
    def register(
        self,
        *,
        email: str,
        password: str,
        full_name: str = "",
        ip: str = "",
        user_agent: str = "",
    ) -> UserPublic:
        if not self.settings.allow_registration:
            raise ForbiddenError("当前未开放自助注册，请联系管理员开通账号")

        normalized_email = email.strip().lower()
        self._check_email_domain(normalized_email)
        passwords.validate_password_strength(
            password, email=normalized_email, min_length=self.settings.password_min_length
        )

        if self.store.get_user_by_email(normalized_email):
            # 注册接口必然暴露「邮箱是否已存在」，这是自助注册的固有代价；
            # 因此生产建议开启 allowed_email_domains 把范围限制在企业邮箱内。
            raise ConflictError("该邮箱已注册，请直接登录或使用找回密码")

        record = UserRecord(
            id=uuid.uuid4().hex,
            email=normalized_email,
            full_name=full_name.strip()[:64],
            role="agent",
            password_hash=passwords.hash_password(password),
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        try:
            self.store.create_user(record)
        except Exception as exc:  # noqa: BLE001 - UNIQUE 冲突（并发注册同一邮箱）
            raise ConflictError("该邮箱已注册，请直接登录或使用找回密码") from exc

        self.store.add_audit(
            email=normalized_email,
            user_id=record.id,
            event=LoginAuditEvent.REGISTER,
            ip=ip,
            user_agent=user_agent,
            detail="self-service registration",
        )
        logger.info("新用户注册: %s", normalized_email)
        return record_to_public(record)

    def _check_email_domain(self, email: str) -> None:
        allowed = self.settings.allowed_email_domains
        if not allowed:
            return
        domain = email.rsplit("@", 1)[-1].lower()
        if domain not in {d.lower().lstrip("@") for d in allowed}:
            raise ValidationError(
                f"仅允许 {'、'.join(allowed)} 的邮箱注册",
                detail=f"domain {domain} not allowed",
            )

    # ---------------- 登录 ----------------
    def login(
        self,
        *,
        email: str,
        password: str,
        captcha_id: str | None = None,
        captcha_code: str | None = None,
        ip: str = "",
        user_agent: str = "",
    ) -> TokenPair:
        normalized_email = email.strip().lower()
        record = self.store.get_user_by_email(normalized_email)
        now = datetime.now(timezone.utc)

        # 1) 账号不存在：仍然做一次哈希校验（时间侧信道对齐），返回同一句提示
        if record is None:
            passwords.verify_password(password, passwords.hash_password("dummy-timing-equalizer"))
            self.store.add_audit(
                email=normalized_email,
                event=LoginAuditEvent.LOGIN_FAILED,
                ip=ip,
                user_agent=user_agent,
                detail="unknown account",
            )
            raise AuthError(_INVALID_CREDENTIALS, detail="unknown account")

        # 2) 锁定中：直接拒绝（不在锁定期间比对密码，避免「锁定也能试密码」）
        if record.locked_until and record.locked_until > now:
            remaining = int((record.locked_until - now).total_seconds())
            self.store.add_audit(
                email=normalized_email,
                user_id=record.id,
                event=LoginAuditEvent.LOGIN_LOCKED,
                ip=ip,
                user_agent=user_agent,
                detail=f"locked {remaining}s remaining",
            )
            raise RateLimitedError(
                f"账号已被临时锁定，请 {max(1, remaining // 60 + 1)} 分钟后再试",
                retry_after_seconds=remaining,
                payload={"locked_seconds": remaining},
            )

        # 3) 失败次数达到阈值后，强制校验图形验证码
        #    注意：**验证码错误不计入密码失败计数**——否则把验证码看错的正常用户
        #    会先被验证码挡住、再被锁定，属于把安全机制变成拒绝服务；
        #    验证码本身已足够拖住脚本撞库（答案不落库、5 分钟过期、需签名校验）。
        captcha_required = record.failed_attempts >= self.settings.captcha_after_failures
        if captcha_required:
            if not captcha.verify_captcha(
                captcha_id or "", captcha_code or "", secret=self.settings.secret_key
            ):
                self.store.add_audit(
                    email=normalized_email,
                    user_id=record.id,
                    event=LoginAuditEvent.LOGIN_FAILED,
                    ip=ip,
                    user_agent=user_agent,
                    detail="captcha invalid",
                )
                raise AuthError(
                    "图形验证码不正确或已过期",
                    detail="captcha invalid",
                    payload={"captcha_required": True, "failed_attempts": record.failed_attempts},
                )

        # 4) 密码校验
        if not passwords.verify_password(password, record.password_hash):
            failed_attempts = record.failed_attempts + 1
            locked_until = None
            if failed_attempts >= self.settings.max_failed_attempts:
                locked_until = now + timedelta(minutes=self.settings.lockout_minutes)
            self.store.record_login_failure(
                record.id, failed_attempts=failed_attempts, locked_until=locked_until
            )
            self.store.add_audit(
                email=normalized_email,
                user_id=record.id,
                event=LoginAuditEvent.LOGIN_FAILED,
                ip=ip,
                user_agent=user_agent,
                detail=f"bad password, attempts={failed_attempts}",
            )
            if locked_until:
                raise RateLimitedError(
                    f"密码连续错误 {failed_attempts} 次，账号已锁定 {self.settings.lockout_minutes} 分钟",
                    retry_after_seconds=self.settings.lockout_minutes * 60,
                    payload={"locked_seconds": self.settings.lockout_minutes * 60},
                )
            remaining = self.settings.max_failed_attempts - failed_attempts
            raise AuthError(
                _INVALID_CREDENTIALS,
                detail="bad password",
                payload={
                    "failed_attempts": failed_attempts,
                    "remaining_attempts": remaining,
                    "captcha_required": failed_attempts >= self.settings.captcha_after_failures,
                },
            )

        # 5) 账号停用
        if not record.is_active:
            self.store.add_audit(
                email=normalized_email,
                user_id=record.id,
                event=LoginAuditEvent.LOGIN_FAILED,
                ip=ip,
                user_agent=user_agent,
                detail="account disabled",
            )
            raise ForbiddenError("账号已被停用，请联系管理员")

        # 6) 成功：清零计数、签发令牌、落审计
        self.store.record_login_success(record.id, at=now)
        pair = self._issue_tokens(record)
        self.store.add_audit(
            email=normalized_email,
            user_id=record.id,
            event=LoginAuditEvent.LOGIN_SUCCESS,
            ip=ip,
            user_agent=user_agent,
            detail=f"role={record.role}",
        )
        record.last_login_at = now
        record.failed_attempts = 0
        record.locked_until = None
        logger.info("登录成功: %s (%s)", normalized_email, ip or "unknown-ip")
        return pair

    # ---------------- 令牌 ----------------
    def _issue_tokens(self, record: UserRecord) -> TokenPair:
        access_token, expires_in = tokens.create_access_token(
            user_id=record.id,
            email=record.email,
            role=record.role,
            secret=self.settings.secret_key,
            algorithm=self.settings.algorithm,
            ttl_minutes=self.settings.access_ttl_minutes,
        )
        refresh_token, jti, expires_at = tokens.create_refresh_token(
            user_id=record.id,
            secret=self.settings.secret_key,
            algorithm=self.settings.algorithm,
            ttl_days=self.settings.refresh_ttl_days,
        )
        self.store.save_refresh_token(jti=jti, user_id=record.id, expires_at=expires_at)
        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
            user=record_to_public(record),
        )

    def refresh(self, *, refresh_token: str, ip: str = "", user_agent: str = "") -> TokenPair:
        """刷新令牌并**轮换**：旧的立即作废，降低泄露令牌的可用窗口。"""
        payload = tokens.decode_token(
            refresh_token,
            secret=self.settings.secret_key,
            algorithm=self.settings.algorithm,
            expected_type=tokens.REFRESH_TOKEN_TYPE,
        )
        jti = str(payload.get("jti") or "")
        if not self.store.is_refresh_token_valid(jti):
            raise AuthError("刷新令牌已失效，请重新登录", detail="refresh token revoked or unknown")

        record = self.store.get_user_by_id(str(payload["sub"]))
        if record is None or not record.is_active:
            raise AuthError("账号不可用，请重新登录", detail="user missing or disabled")

        self.store.revoke_refresh_token(jti)
        pair = self._issue_tokens(record)
        self.store.add_audit(
            email=record.email,
            user_id=record.id,
            event=LoginAuditEvent.REFRESH,
            ip=ip,
            user_agent=user_agent,
            detail="token rotated",
        )
        return pair

    def logout(self, *, refresh_token: str | None, user: UserRecord, ip: str = "", user_agent: str = "") -> None:
        """登出：撤销该刷新令牌（access 短期，靠过期自然失效）；无 refresh 时撤销该用户全部。"""
        if refresh_token:
            try:
                payload = tokens.decode_token(
                    refresh_token,
                    secret=self.settings.secret_key,
                    algorithm=self.settings.algorithm,
                    expected_type=tokens.REFRESH_TOKEN_TYPE,
                )
                self.store.revoke_refresh_token(str(payload.get("jti") or ""))
            except AuthError:
                # 令牌已失效也要允许登出（幂等），但不能因此报错打断前端清理
                logger.info("登出时刷新令牌已失效: user=%s", user.email)
        else:
            self.store.revoke_all_for_user(user.id)

        self.store.add_audit(
            email=user.email,
            user_id=user.id,
            event=LoginAuditEvent.LOGOUT,
            ip=ip,
            user_agent=user_agent,
            detail="logout",
        )

    def authenticate_access_token(self, token: str) -> UserRecord:
        """校验访问令牌并返回有效用户；用于受保护接口的依赖注入。"""
        payload = tokens.decode_token(
            token,
            secret=self.settings.secret_key,
            algorithm=self.settings.algorithm,
            expected_type=tokens.ACCESS_TOKEN_TYPE,
        )
        record = self.store.get_user_by_id(str(payload["sub"]))
        if record is None:
            raise AuthError("登录状态无效，请重新登录", detail="user not found")
        if not record.is_active:
            raise ForbiddenError("账号已被停用，请联系管理员")
        if record.locked_until and record.locked_until > datetime.now(timezone.utc):
            raise AuthError("账号处于锁定状态，请重新登录", detail="account locked")
        return record

    # ---------------- 账号管理（管理员） ----------------
    def change_password(self, *, user_id: str, old_password: str, new_password: str) -> None:
        record = self.store.get_user_by_id(user_id)
        if record is None:
            raise AuthError("账号不存在", detail="user not found")
        if not passwords.verify_password(old_password, record.password_hash):
            raise AuthError("原密码不正确", detail="old password mismatch")
        passwords.validate_password_strength(
            new_password, email=record.email, min_length=self.settings.password_min_length
        )
        self.store.set_password_hash(user_id, passwords.hash_password(new_password))
        self.store.revoke_all_for_user(user_id)  # 改密后所有设备重新登录
        self.store.add_audit(
            email=record.email, user_id=user_id, event="password_changed", detail="password rotated"
        )

    def list_users(self, *, admin: UserRecord, limit: int = 100) -> list[UserPublic]:
        if admin.role != "admin":
            raise ForbiddenError("仅管理员可查看用户列表")
        return [record_to_public(r) for r in self.store.list_users(limit=limit)]

    def list_audit(self, *, admin: UserRecord, limit: int = 100, email: str | None = None) -> list[AuditEntry]:
        if admin.role != "admin":
            raise ForbiddenError("仅管理员可查看登录审计")
        return self.store.list_audit(limit=limit, email=email)

    # ---------------- CLI / 运维入口（无请求上下文时使用） ----------------
    def create_user(
        self, *, email: str, password: str, full_name: str = "", role: Literal["admin", "agent"] = "agent"
    ) -> UserPublic:
        """运维开号：不受 ``allow_registration`` 限制，可指定角色。"""
        normalized_email = email.strip().lower()
        passwords.validate_password_strength(
            password, email=normalized_email, min_length=self.settings.password_min_length
        )
        if self.store.get_user_by_email(normalized_email):
            raise ConflictError("该邮箱已存在")
        record = UserRecord(
            id=uuid.uuid4().hex,
            email=normalized_email,
            full_name=full_name.strip()[:64],
            role=role,
            password_hash=passwords.hash_password(password),
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        self.store.create_user(record)
        self.store.add_audit(
            email=normalized_email, user_id=record.id, event="register", detail="created via CLI"
        )
        return record_to_public(record)

    def list_all_users(self) -> list[UserRecord]:
        return self.store.list_users(limit=1000)

    def admin_reset_password(self, *, email: str, new_password: str) -> None:
        record = self.store.get_user_by_email(email.strip().lower())
        if record is None:
            raise ValidationError("账号不存在", detail=f"no such user: {email}")
        passwords.validate_password_strength(
            new_password, email=record.email, min_length=self.settings.password_min_length
        )
        self.store.set_password_hash(record.id, passwords.hash_password(new_password))
        self.store.revoke_all_for_user(record.id)
        self.store.add_audit(
            email=record.email, user_id=record.id, event="password_changed", detail="reset by admin/CLI"
        )

    def set_active(self, *, email: str, is_active: bool) -> None:
        record = self.store.get_user_by_email(email.strip().lower())
        if record is None:
            raise ValidationError("账号不存在", detail=f"no such user: {email}")
        self.store.set_active(record.id, is_active=is_active)
        if not is_active:
            self.store.revoke_all_for_user(record.id)  # 停用即踢下线
        self.store.add_audit(
            email=record.email,
            user_id=record.id,
            event="account_enabled" if is_active else "account_disabled",
            detail="via CLI",
        )
