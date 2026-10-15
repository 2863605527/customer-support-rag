"""结构化日志：JSON 输出 + 全链路 trace_id。

为什么要有 trace_id：

一次买家咨询会穿过「语言识别 → 意图识别 → 检索 → 生成 → 工单」多个环节，
出问题时值班同学最想知道的是「这一通到底走了哪条路、每一步花了多久」。
把 trace_id 放进 ``contextvars``（而不是层层透传参数），日志里每条都自动带上，
既不用改函数签名，也不会在异步/多线程下串号。
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone

# 当前请求/任务的链路 ID；未设置时为空串
_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")


def set_trace_id(trace_id: str) -> None:
    _trace_id_var.set(trace_id)


def get_trace_id() -> str:
    return _trace_id_var.get()


class JsonFormatter(logging.Formatter):
    """把日志渲染成单行 JSON，便于采集到 ELK / Loki。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        trace_id = get_trace_id()
        if trace_id:
            payload["trace_id"] = trace_id
        context = getattr(record, "context", None)
        if context:
            payload["context"] = context
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class PlainFormatter(logging.Formatter):
    """本地调试用的人类可读格式。"""

    def format(self, record: logging.LogRecord) -> str:
        trace_id = get_trace_id()
        prefix = f"[{trace_id}] " if trace_id else ""
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {record.name}: "
        text = prefix + record.getMessage()
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)
        return base + text


def setup_logging(level: str = "INFO", json_format: bool = True) -> None:
    """初始化根 logger（幂等）。"""
    root = logging.getLogger()
    root.setLevel(level.upper())
    if root.handlers:
        for handler in root.handlers:
            handler.setFormatter(JsonFormatter() if json_format else PlainFormatter())
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_format else PlainFormatter())
    root.addHandler(handler)
    # 第三方库降噪：httpx/openai 的 INFO 日志会淹没业务日志
    for noisy in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
