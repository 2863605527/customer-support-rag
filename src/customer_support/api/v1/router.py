"""v1 路由聚合。

鉴权边界（重要）：
- ``/auth/*`` —— 公开（注册/登录/刷新/验证码），登出、me、管理接口在内部单独要求令牌；
- ``/chat``、``/ops/*`` —— **需要登录**（在路由聚合处统一加 ``Depends(get_current_user)``，
  避免逐个端点漏加）；
- ``/healthz``、``/readyz`` —— 保持公开，K8s/Docker 探针无法携带令牌。
"""
from fastapi import APIRouter, Depends

from customer_support.api.deps import get_current_user
from customer_support.api.v1.auth import router as auth_router
from customer_support.api.v1.chat import router as chat_router
from customer_support.api.v1.ops import router as ops_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(chat_router, tags=["chat"], dependencies=[Depends(get_current_user)])
api_router.include_router(ops_router, tags=["ops"], dependencies=[Depends(get_current_user)])
