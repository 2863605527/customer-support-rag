"""客服接口：POST /api/v1/chat（整段）、POST /api/v1/chat/stream（SSE 流式）、会话管理。"""
from __future__ import annotations

import json
from collections.abc import Iterator
from typing import cast

from fastapi import APIRouter, Depends, status
from fastapi.responses import StreamingResponse

from customer_support.api.deps import chat_rate_limit, get_chat_service, get_session_store
from customer_support.common.logging import get_logger
from customer_support.models.message import ChatMessage, ChatResult
from customer_support.models.user import UserRecord
from customer_support.services.chat_service import ChatService
from customer_support.services.session import SessionStore

logger = get_logger(__name__)
router = APIRouter()


@router.post(
    "/chat",
    response_model=ChatResult,
    summary="处理一条买家咨询",
    description=(
        "多语言客服主接口。传入 `session_id` 可启用多轮上下文（买家连续追问时必传）。\n\n"
        "返回结果中的 `need_human=true` 表示已自动转人工并创建工单，"
        "`ticket_id` 为工单号，`human_reason` 说明转人工原因。"
    ),
)
def chat(
    message: ChatMessage,
    user: UserRecord = Depends(chat_rate_limit),
    service: ChatService = Depends(get_chat_service),
) -> ChatResult:
    logger.info("API 收到咨询 (user=%s, session=%s): %s", user.email, message.session_id, message.question[:60])
    return service.handle(message)


def _sse(event: str, data: dict[str, object]) -> str:
    """编码一帧 SSE。数据必须单行 JSON（换行符已由 json.dumps 转义）。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post(
    "/chat/stream",
    summary="处理一条买家咨询（SSE 流式）",
    description=(
        "与 `/chat` 同一条业务链路，但以 Server-Sent Events 逐段返回：\n\n"
        "- `status`：链路阶段（detect_language / rewrite / intent / retrieval / generate），"
        "可携带 `detail`（如命中的商品名、检索命中条数）；\n"
        "- `delta`：回答增量，前端逐字渲染；\n"
        "- `replace`：资料不足时整段替换为兜底话术；\n"
        "- `done`：终态 ChatResult（含引用、工单号、耗时），必发。\n\n"
        "任何异常都会以 `done`（转人工结果）收尾，不会断流不给终态。"
    ),
)
def chat_stream(
    message: ChatMessage,
    user: UserRecord = Depends(chat_rate_limit),
    service: ChatService = Depends(get_chat_service),
) -> StreamingResponse:
    logger.info(
        "API 收到流式咨询 (user=%s, session=%s): %s", user.email, message.session_id, message.question[:60]
    )

    def generate() -> Iterator[str]:
        for event in service.handle_stream(message):
            event_type = str(event["type"])
            payload: dict[str, object] = dict(event)
            if event_type == "done":
                result = cast(ChatResult, event["result"])
                payload["result"] = result.model_dump(mode="json")
            yield _sse(event_type, payload)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 关闭反代缓冲，保证逐帧下发
        },
    )


@router.post(
    "/sessions/{session_id}/reset",
    status_code=status.HTTP_200_OK,
    summary="清空指定会话的记忆",
    description="买家换话题或需要重新开始时调用，避免上一轮的上下文污染检索改写。",
)
def reset_session(
    session_id: str,
    store: SessionStore = Depends(get_session_store),
) -> dict:
    store.clear(session_id)
    logger.info("已清空会话记忆: %s", session_id)
    return {"session_id": session_id, "cleared": True}
