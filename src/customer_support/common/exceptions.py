"""统一异常体系（与项目1同一设计，按客服域扩展）。"""
from enum import Enum


class ErrorCode(str, Enum):
    CONFIG_ERROR = "CONFIG_ERROR"
    EXTERNAL_API_ERROR = "EXTERNAL_API_ERROR"
    KNOWLEDGE_ERROR = "KNOWLEDGE_ERROR"
    RAG_ERROR = "RAG_ERROR"
    AGENT_ERROR = "AGENT_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    AUTH_ERROR = "AUTH_ERROR"
    FORBIDDEN = "FORBIDDEN"
    CONFLICT = "CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    NOT_FOUND = "NOT_FOUND"
    UNKNOWN = "UNKNOWN"


class AppError(Exception):
    """应用异常基类。"""

    code: ErrorCode = ErrorCode.UNKNOWN
    http_status: int = 500

    def __init__(self, message: str, *, detail: str | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        return {
            "error": {"code": self.code.value, "message": self.message, "detail": self.detail},
        }


class ConfigError(AppError):
    code = ErrorCode.CONFIG_ERROR
    http_status = 500


class ExternalAPIError(AppError):
    """外部服务（RAGFlow / LLM 等）调用失败。

    ``remote_code`` 保留服务端业务错误码：调用方需要据此区分
    「真失败」与「可容忍的状态冲突」（例如 RAGFlow 的 102 = 文档已在解析中）。
    只靠 message 字符串匹配太脆弱，因此把它结构化下来。
    """

    code = ErrorCode.EXTERNAL_API_ERROR
    http_status = 502

    def __init__(self, message: str, *, detail: str | None = None, remote_code: int | None = None):
        super().__init__(message, detail=detail)
        self.remote_code = remote_code


class KnowledgeError(AppError):
    """知识库构建/加载失败。"""

    code = ErrorCode.KNOWLEDGE_ERROR
    http_status = 500


class RAGError(AppError):
    code = ErrorCode.RAG_ERROR
    http_status = 500


class AgentError(AppError):
    code = ErrorCode.AGENT_ERROR
    http_status = 500


class ValidationError(AppError):
    code = ErrorCode.VALIDATION_ERROR
    http_status = 400


class AuthError(AppError):
    """未认证 / 凭据错误（401）。

    ``payload`` 用于携带前端需要的非敏感上下文（例如「还需图形验证码」「已失败 3 次」），
    避免前端靠错误文案做分支判断。
    """

    code = ErrorCode.AUTH_ERROR
    http_status = 401

    def __init__(
        self,
        message: str,
        *,
        detail: str | None = None,
        payload: dict | None = None,
    ):
        super().__init__(message, detail=detail)
        self.payload = payload or {}

    def to_dict(self) -> dict:
        data = super().to_dict()
        if self.payload:
            data["error"]["context"] = self.payload
        return data


class ForbiddenError(AppError):
    """已认证但无权访问（403）。"""

    code = ErrorCode.FORBIDDEN
    http_status = 403


class ConflictError(AppError):
    """资源冲突，如邮箱已注册（409）。"""

    code = ErrorCode.CONFLICT
    http_status = 409


class ServiceUnavailableError(AppError):
    """依赖暂时不可用（熔断打开 / 主动降级）（503）。

    ``retry_after_seconds`` 会由异常处理器写成 ``Retry-After`` 头，
    客户端据此退避而不是立刻重试（避免把已经过载的依赖打死）。
    """

    code = ErrorCode.SERVICE_UNAVAILABLE
    http_status = 503

    def __init__(
        self,
        message: str,
        *,
        detail: str | None = None,
        retry_after_seconds: int = 0,
    ):
        super().__init__(message, detail=detail)
        self.retry_after_seconds = retry_after_seconds


class NotFoundError(AppError):
    """资源不存在（404）。"""

    code = ErrorCode.NOT_FOUND
    http_status = 404


class RateLimitedError(AppError):
    """触发限流 / 账号锁定（429）。"""

    code = ErrorCode.RATE_LIMITED
    http_status = 429

    def __init__(
        self,
        message: str,
        *,
        detail: str | None = None,
        retry_after_seconds: int = 0,
        payload: dict | None = None,
    ):
        super().__init__(message, detail=detail)
        self.retry_after_seconds = retry_after_seconds
        self.payload = payload or {}

    def to_dict(self) -> dict:
        data = super().to_dict()
        data["error"]["context"] = {
            **self.payload,
            "retry_after_seconds": self.retry_after_seconds,
        }
        return data
