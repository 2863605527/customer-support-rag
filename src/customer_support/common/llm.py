"""LLM 与向量化模型访问层（OpenAI 兼容协议）。

项目只依赖「OpenAI 兼容」这一层协议，因此换供应商只需要改 ``.env``：

- DeepSeek   ``LLM_BASE_URL=https://api.deepseek.com/v1``      ``LLM_MODEL=deepseek-chat``
- SiliconFlow ``LLM_BASE_URL=https://api.siliconflow.cn/v1``   ``LLM_MODEL=Qwen/Qwen3-8B``
- OpenAI     ``LLM_BASE_URL=https://api.openai.com/v1``        ``LLM_MODEL=gpt-4o-mini``

RAGFlow 检索后端自带向量化，因此 ``create_embedding_fn`` 只在
``RETRIEVAL_BACKEND=milvus`` 时才需要被调用。
"""
from __future__ import annotations

from collections.abc import Callable

from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from customer_support.common.exceptions import ConfigError
from customer_support.common.logging import get_logger
from customer_support.config import Settings

logger = get_logger(__name__)


def create_llm(settings: Settings) -> ChatOpenAI:
    """创建对话模型客户端。"""
    if not settings.llm.api_key:
        raise ConfigError(
            "未配置 LLM_API_KEY",
            detail="请复制 .env.example 为 .env，并填入 DeepSeek / SiliconFlow 等供应商的 API Key",
        )
    logger.info(
        "初始化 LLM: model=%s base_url=%s timeout=%.0fs retries=%d",
        settings.llm.model, settings.llm.base_url, settings.llm.timeout, settings.llm.max_retries,
    )
    return ChatOpenAI(
        model=settings.llm.model,
        api_key=settings.llm.api_key,
        base_url=settings.llm.base_url,
        temperature=settings.llm.temperature,
        max_tokens=settings.llm.max_tokens,
        timeout=settings.llm.timeout,
        max_retries=settings.llm.max_retries,
    )


def create_embedding_fn(settings: Settings) -> Callable[[str], list[float]]:
    """创建查询向量化函数（仅 Milvus 后端需要）。"""
    if not settings.llm.api_key:
        raise ConfigError(
            "未配置 LLM_API_KEY",
            detail="Milvus 后端需要调用向量化模型，请先在 .env 中配置 LLM_API_KEY",
        )
    emb = OpenAIEmbeddings(
        model=settings.llm.embedding_model,
        # 注意：OpenAIEmbeddings 的字段名是 openai_api_key / openai_api_base
        # （沿用 OpenAI 客户端命名），不是 api_key / base_url
        openai_api_key=settings.llm.api_key,
        openai_api_base=settings.llm.embedding_base_url or settings.llm.base_url,
        check_embedding_ctx_length=False,
    )
    return emb.embed_query
