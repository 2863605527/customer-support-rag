"""FastAPI 应用工厂。

工程要点：
- **探针分离**：``/healthz`` 是存活探针（只看进程），``/readyz`` 是就绪探针
  （会真的去问检索后端 + 检查模型凭据）。K8s/Docker 里用错探针的典型后果是
  「依赖挂了但容器一直重启」，两者必须分开。
- **链路追踪**：中间件为每个请求生成 trace_id，写进 ``contextvars``，
  之后所有日志自动带这个 ID，响应头也回传 ``X-Trace-Id``，前端报障时可直接对上日志。
- **启动不硬依赖**：应用启动时不强制要求模型凭据可用（否则本地跑前端就起不来），
  真正的依赖问题由 ``/readyz`` 暴露，请求进来时才装配服务。
"""
from __future__ import annotations

import secrets
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from customer_support.api.v1.router import api_router
from customer_support.common.exceptions import AppError, ConfigError
from customer_support.common.logging import get_logger, set_trace_id, setup_logging
from customer_support.common.resilience import get_llm_health
from customer_support.config import ROOT_DIR, get_settings

logger = get_logger(__name__)

# 安全响应头：Vue SPA 单域名部署下的最小可用集合。
# Swagger UI（/docs、/redoc）依赖 jsdelivr 的脚本与样式，因此 script-src / style-src
# 必须放行该 CDN，否则「为了安全把文档页打不开」是得不偿失的取舍。
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    ),
}


def _resolve_static(root: Path, full_path: str) -> Path | None:
    """把请求路径解析成 ``root`` 内的真实文件；越界或不存在返回 ``None``。

    ⚠️ **必须「解析后再比对前缀」，不能直接 ``root / full_path`` 再 ``is_file()``**：
    Starlette 的 ``{full_path:path}`` 会匹配到服务端**已解码**的 ``../../.env``
    这类路径，而 ``Path.__truediv__`` 不做归一化——
    ``(dist / "../../.env").is_file()`` 为真，直接返回文件就是**任意文件读取**
    （``.env`` 里的 LLM/RAGFlow Key、``output/auth/users.db`` 全可被下载）。
    实测 ``curl --path-as-is 'http://host/%2e%2e/%2e%2e/.env'`` 即可复现。

    ``resolve()`` 会跟随符号链接，因此软链接指向库外同样会被拦下。
    """
    if not full_path:
        return None
    base = root.resolve()
    try:
        candidate = (base / full_path).resolve()
    except (OSError, ValueError):  # 含 NUL 字节等非法路径
        return None
    # candidate == base 表示请求的就是目录本身，也应交给 SPA 回退
    if candidate == base or base not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


def create_app() -> FastAPI:
    """应用工厂。"""
    settings = get_settings()
    setup_logging(settings.log_level, json_format=settings.log_json)
    settings.ensure_dirs()

    # ---------------- 启动自检：生产环境弱配置直接拒绝启动 ----------------
    # 「能起来但配置不安全」比「起不来」更危险——起不来看得见，配置不安全看不见。
    if settings.is_production:
        issues = settings.production_issues()
        if issues:
            raise ConfigError(
                "生产环境配置校验未通过，已拒绝启动",
                detail="；".join(issues),
            )
    for warning in settings.production_warnings():
        logger.warning("配置提醒: %s", warning)

    app = FastAPI(
        title="customer-support-rag API",
        version=settings.version,
        description=(
            "跨境店铺多语言智能客服 RAG 问答系统 —— 面向 TikTok 日本站寝具店铺。\n\n"
            "检索后端：RAGFlow 知识库（可切换 Milvus）。"
        ),
    )

    # ---------------- 中间件：CORS（前端独立部署时跨域调用 API） ----------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["*"],
        expose_headers=["X-Trace-Id", "Retry-After"],
    )

    # ---------------- 中间件：安全响应头 ----------------
    @app.middleware("http")
    async def security_headers_middleware(request: Request, call_next):
        response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        if settings.is_production:
            # 只在生产加 HSTS：本地 http 开发环境加了会把浏览器锁到 https，反而添乱
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    # ---------------- 中间件：trace_id + 访问日志 ----------------
    @app.middleware("http")
    async def trace_middleware(request: Request, call_next):
        trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex[:12]
        set_trace_id(trace_id)
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        response.headers["X-Trace-Id"] = trace_id
        if request.url.path not in {"/healthz", "/readyz", "/metrics"}:
            logger.info(
                "%s %s -> %d (%dms)",
                request.method, request.url.path, response.status_code, elapsed_ms,
            )
        return response

    # ---------------- 探针 ----------------
    @app.get("/healthz", tags=["ops"], summary="存活探针")
    def healthz() -> dict:
        """进程存活即返回 ok，不触碰任何外部依赖。"""
        return {"status": "ok", "version": settings.version}

    @app.get("/readyz", tags=["ops"], summary="就绪探针")
    def readyz() -> JSONResponse:
        """检查配置、检索后端与 LLM 可用性。

        ``llm.ok=False``（例如供应商欠费）会让就绪探针返回 503——因为此时问答
        已全部降级为转人工，对外承诺的「智能客服」实际不成立。容器重启策略挂在
        ``/healthz``（存活）上，因此这不会引发重启风暴，但监控会准确告警。
        """
        checks: dict[str, object] = {}
        ok = True

        checks["llm_configured"] = bool(settings.llm.api_key)
        if not checks["llm_configured"]:
            ok = False
        llm_health = get_llm_health().snapshot()
        checks["llm"] = llm_health
        if settings.llm.api_key and not llm_health["ok"]:
            ok = False

        try:
            from customer_support.api.deps import get_retrieval_backend

            backend = get_retrieval_backend()
            describe = backend.describe()
            health = backend.health() if hasattr(backend, "health") else {"ok": True}
            retrieval_ok = bool(health.get("ok", False))
            # ok 提升到顶层：消费方（前端侧栏）无需了解 health 子结构
            checks["retrieval"] = {**describe, "health": health, "count": backend.count(), "ok": retrieval_ok}
            if not retrieval_ok:
                ok = False
        except Exception as exc:  # noqa: BLE001 - 就绪检查本身不能抛错
            checks["retrieval"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            ok = False

        return JSONResponse(status_code=200 if ok else 503, content={"ready": ok, "checks": checks})

    @app.get("/metrics", tags=["ops"], summary="Prometheus 指标（聚合值，无买家隐私）", include_in_schema=True)
    def metrics(request: Request) -> PlainTextResponse:
        """Prometheus 文本格式指标。

        配置了 ``METRICS_TOKEN`` 时必须带 ``X-Metrics-Token`` 头；未配置则开放
        （启动时会打警告）。指标只含聚合计数与比率，不含任何买家内容。
        """
        if settings.metrics_token and not secrets.compare_digest(
            request.headers.get("x-metrics-token", ""), settings.metrics_token
        ):
            raise HTTPException(status_code=401, detail="invalid metrics token")
        from customer_support.api.deps import get_analytics_service

        try:
            text = get_analytics_service().metrics_text()
        except Exception as exc:  # noqa: BLE001 - 指标端点不应 500
            logger.warning("生成 metrics 失败: %s", exc)
            text = ""
        return PlainTextResponse(text, media_type="text/plain; version=0.0.4; charset=utf-8")

    # ---------------- 业务路由 ----------------
    app.include_router(api_router, prefix="/api/v1")

    # ---------------- 前端静态托管（单域名模式） ----------------
    # frontend/dist 存在时由后端直接托管页面：打开 http://127.0.0.1:<port>/ 即是工作台，
    # 前后端同源，无需 CORS / 反向代理，部署形态最简单。
    frontend_dist = ROOT_DIR / "frontend" / "dist"
    if frontend_dist.is_dir() and any(frontend_dist.iterdir()):
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        # 1) 带哈希的构建产物走静态目录（长缓存友好）
        assets_dir = frontend_dist / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        index_file = frontend_dist / "index.html"

        def _no_cache_index() -> FileResponse:
            """index.html 必须禁强缓存：它引用带哈希的资源名，缓存旧 index 会导致
            浏览器整页用旧版前端（新功能"看起来没生效"）。带哈希的 assets 可长缓存。"""
            resp = FileResponse(index_file)
            resp.headers["Cache-Control"] = "no-cache"
            return resp

        # 2) SPA 回退：前端用 history 路由，/login、/register 在服务端没有对应文件，
        #    必须统一回 index.html，否则用户刷新页面或直接分享深链就是 404。
        @app.get("/{full_path:path}", include_in_schema=False)
        def spa_fallback(full_path: str) -> FileResponse:
            reserved = ("api/", "docs", "redoc", "openapi.json", "healthz", "readyz")
            if full_path.startswith(reserved):
                # 未匹配的 API 路径仍应返回 JSON 404，而不是把 HTML 丢给调用方
                raise HTTPException(status_code=404, detail="Not Found")
            candidate = _resolve_static(frontend_dist, full_path)
            if candidate is not None:
                return FileResponse(candidate)
            return _no_cache_index()

    else:

        @app.get("/", include_in_schema=False)
        def root_hint() -> dict:
            """根路径提示：没有前端构建产物时，告诉使用者去哪里。"""
            return {
                "service": settings.app_name,
                "docs": "/docs",
                "frontend": (
                    "未发现 frontend/dist。网页工作台两种方式："
                    "① 生产：cd frontend && npm install && npm run build 后重启本服务；"
                    "② 开发：cd frontend && npm run dev（Vite 代理到本服务）"
                ),
            }

    # ---------------- 异常处理 ----------------
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        logger.error("业务异常 %s: %s (%s)", exc.code.value, exc.message, exc.detail)
        headers: dict[str, str] = {}
        retry_after = getattr(exc, "retry_after_seconds", 0)
        if retry_after:
            # 429 必须带 Retry-After，客户端/网关据此退避；否则只能瞎猜
            headers["Retry-After"] = str(int(retry_after))
        return JSONResponse(
            status_code=exc.http_status,
            content=exc.to_dict(),
            headers=headers or None,
        )

    return app
