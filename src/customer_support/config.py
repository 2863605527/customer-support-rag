"""应用配置：pydantic-settings 类型安全配置管理。

配置分层：
- ``LLMSettings``      大模型与向量化模型（OpenAI 兼容协议，可指向 DeepSeek / SiliconFlow 等）
- ``RAGFlowSettings``  RAGFlow 检索后端连接参数（默认后端）
- ``MilvusSettings``   Milvus 向量库连接参数（备选后端）
- ``RetrievalSettings`` 检索策略：选哪个后端、top_k、相似度阈值
- ``KnowledgeSettings`` 知识库文档目录与切片参数
- ``AgentSettings``    客服业务规则：店铺名、默认语言、转人工阈值、会话记忆

环境变量前缀即类名去掉 Settings 后的大写形式（如 ``RAGFLOW_BASE_URL``），
无前缀的类直接使用字段名大写（如 ``KNOWLEDGE_BASE_DIR``）。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]


def _split_csv(v: object) -> object:
    """把 "a,b,c" 形式的环境变量解析成 list。

    配套 ``NoDecode`` 使用：pydantic-settings 默认会把 ``list`` 字段的环境变量
    当成 JSON 解析（要求 ``["a","b"]`` 这种写法），而运维习惯写 ``a,b,c``——
    不关掉默认解码，``RAGFLOW_DATASET_IDS=tiktok_bedding_kb`` 会直接抛
    ``SettingsError: error parsing value``。``NoDecode`` 让原始字符串原样交给
    下面的 ``field_validator`` 处理。
    """
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return v


StrList = Annotated[list[str], NoDecode]


class LLMSettings(BaseSettings):
    """大模型配置（OpenAI 兼容协议）。"""

    model_config = SettingsConfigDict(env_prefix="LLM_", env_file=".env", extra="ignore")

    api_key: str = Field("", description="LLM API Key（必填，留空则启动时明确报错）")
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    temperature: float = 0.2
    max_tokens: int = 800
    timeout: float = 30.0
    max_retries: int = 2
    # 成本核算单价（元 / 千 token）：不同供应商差异巨大，必须可配；
    # 留 0 表示不估算成本（此时仪表盘只显示 token 量，不显示金额）。
    cost_prompt_per_1k: float = 0.0
    cost_completion_per_1k: float = 0.0
    # 熔断：连续失败多少次打开、冷却多久（秒）
    circuit_failure_threshold: int = 5
    circuit_recovery_seconds: float = 60.0
    # 向量化模型：仅 Milvus 后端需要（RAGFlow 自带向量化）
    embedding_base_url: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536


class RAGFlowSettings(BaseSettings):
    """RAGFlow 检索后端配置。"""

    model_config = SettingsConfigDict(env_prefix="RAGFLOW_", env_file=".env", extra="ignore")

    base_url: str = "http://localhost"
    api_key: str = ""
    # 知识库可写名称或 dataset id；多个用逗号分隔
    dataset_ids: StrList = Field(default_factory=lambda: ["tiktok_bedding_kb"])
    # 传给 RAGFlow 的粗筛阈值与向量权重（本项目会在本地再做一次精确阈值过滤）
    similarity_threshold: float = 0.15
    # 0 = 纯关键词、1 = 纯向量。0.7 为本项目知识库上的实测最优：
    # 0.3 时改写类问题的相关分仅 0.23~0.49、与无关问题严重重叠；0.7 时相关分升到 0.52+
    vector_similarity_weight: float = 0.7
    page_size: int = 10
    timeout: float = 30.0
    max_retries: int = 3
    # 是否启用 RAGFlow 侧重排（需在 RAGFlow 中已配置 rerank 模型）
    use_rerank: bool = False

    @field_validator("dataset_ids", mode="before")
    @classmethod
    def _parse_dataset_ids(cls, v: object) -> object:
        return _split_csv(v)


class MilvusSettings(BaseSettings):
    """Milvus 向量库配置（备选检索后端）。"""

    model_config = SettingsConfigDict(env_prefix="MILVUS_", env_file=".env", extra="ignore")

    host: str = "localhost"
    port: int = 19530
    collection: str = "shop_knowledge"
    # 本地向量化维度（必须与 llm.embedding_model 一致）
    embedding_dim: int = 1536


class RetrievalSettings(BaseSettings):
    """检索策略配置。"""

    model_config = SettingsConfigDict(env_prefix="RETRIEVAL_", env_file=".env", extra="ignore")

    backend: Literal["ragflow", "milvus"] = "ragflow"
    top_k: int = 4
    # 相似度阈值：低于该值的切片视为「资料不足」，触发兜底转人工（防幻觉第一道防线）。
    # 这个值**与平台/embedding 模型强相关**，不是通用常量：本项目在 26 条标注问题上
    # 用 scripts/calibrate_threshold.py 标定出 0.53（召回 94%、误检 10%、Youden J=0.84）。
    # 换模型或知识库大改后请重新标定，不要沿用。
    score_threshold: float = 0.53
    # 同一份文档最多采用几条切片（来源去重，保证上下文覆盖度）
    max_per_source: int = 2


class KnowledgeSettings(BaseSettings):
    """知识库文档与切片配置。"""

    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

    knowledge_base_dir: Path = ROOT_DIR / "data" / "knowledge_base"
    supported_extensions: StrList = [".txt", ".md", ".pdf"]
    chunk_size: int = 500
    chunk_overlap: int = 50
    # 商品目录（数据流水线产物）：用于把买家消息里的货源链接映射成商品，
    # 注入检索词。文件不存在时链接识别优雅降级为不可用。
    catalog_path: Path = ROOT_DIR / "data" / "raw" / "zhaojiafang" / "catalog.json"

    @field_validator("supported_extensions", mode="before")
    @classmethod
    def _parse_ext(cls, v: object) -> object:
        return _split_csv(v)


class ConsoleSettings(BaseSettings):
    """运营控制台配置：会话 / 工单 / 反馈 / 缓存的存放与策略。"""

    model_config = SettingsConfigDict(env_prefix="CONSOLE_", env_file=".env", extra="ignore")

    # 运营数据统一落一张 SQLite（会话、消息、工单、反馈、缓存）
    db_path: Path = ROOT_DIR / "output" / "console" / "console.db"
    # 精确问答缓存：同问题（同语言、无上下文）直接复用结果，省 LLM 成本
    cache_enabled: bool = True
    cache_ttl_seconds: int = 3600
    # 会话历史页单页大小
    page_size: int = 20


class RateLimitSettings(BaseSettings):
    """接口限流（令牌桶：burst 为突发容量，per_minute 为长期速率）。"""

    model_config = SettingsConfigDict(env_prefix="RATELIMIT_", env_file=".env", extra="ignore")

    enabled: bool = True
    chat_burst: int = 10
    chat_per_minute: int = 30
    login_burst: int = 5
    login_per_minute: int = 10


class AgentSettings(BaseSettings):
    """客服业务规则。"""

    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

    shop_name: str = "くらし寝具（Kurashi Bedding）"
    shop_name_ja: str = "くらし寝具"
    # 默认客服语言：日语（TikTok 日本站）
    default_language: str = "ja"
    supported_languages: StrList = ["ja", "en", "es", "fr", "de", "ko", "zh"]
    # 高风险意图（强制转人工）
    human_intents: StrList = ["human", "refund", "complaint", "dispute"]
    # 意图置信度阈值：低于此值一律转人工（安全优先）
    confidence_threshold: float = 0.5
    # 单会话保留的最近轮数（用于多轮指代消解）
    session_history_turns: int = 4
    # 会话记忆保留时长（秒）
    session_ttl_seconds: int = 3600

    @field_validator("supported_languages", "human_intents", mode="before")
    @classmethod
    def _parse_list(cls, v: object) -> object:
        return _split_csv(v)


class AuthSettings(BaseSettings):
    """鉴权与账号安全配置。

    密码与令牌策略全部可配：生产环境必须覆盖 ``AUTH_SECRET_KEY``（默认值仅供本地开发，
    启动时会打警告）；``AUTH_USERS_DB`` 默认落在 ``output/`` 下，不进版本库。
    """

    model_config = SettingsConfigDict(env_prefix="AUTH_", env_file=".env", extra="ignore")

    # JWT 签名密钥：生产必须覆盖，否则任何拿到默认值的人都能伪造令牌
    secret_key: str = "dev-insecure-secret-change-me-in-production"
    algorithm: str = "HS256"
    # 访问令牌有效期（分钟）与刷新令牌有效期（天）
    access_ttl_minutes: int = 60
    refresh_ttl_days: int = 14
    # 连续失败多少次锁定账号、锁定时长
    max_failed_attempts: int = 5
    lockout_minutes: int = 15
    # 失败多少次后开始要求图形验证码（防脚本撞库）
    captcha_after_failures: int = 3
    # 图形验证码签名有效期（秒）
    captcha_ttl_seconds: int = 300
    # 密码策略
    password_min_length: int = 10
    # 是否开放自助注册（企业内网场景可关闭，只由管理员开号）
    allow_registration: bool = True
    # 允许注册的邮箱域名（空 = 不限制），逗号分隔，如 "kurashi-bedding.jp,example.com"
    allowed_email_domains: StrList = []
    # 首次启动且用户表为空时自动创建的管理员
    seed_admin_email: str = "admin@kurashi-bedding.jp"
    seed_admin_password: str = ""
    users_db: Path = ROOT_DIR / "output" / "auth" / "users.db"

    @field_validator("allowed_email_domains", mode="before")
    @classmethod
    def _parse_domains(cls, v: object) -> object:
        return _split_csv(v)


class Settings(BaseSettings):
    """应用总配置。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "customer-support-rag"
    version: str = "0.8.0"
    # 运行环境：production 下会启用更严格的生产校验（弱密钥直接拒绝启动）
    environment: str = "development"
    log_level: str = "INFO"
    # JSON 结构化日志（生产建议开启；本地调试可关）
    log_json: bool = True
    # 允许跨域访问 API 的来源（前端 dev server / 静态站点域名），逗号分隔；* 表示全部
    cors_origins: StrList = ["*"]
    # 可信反向代理 IP 白名单（逗号分隔）。
    # ⚠️ 只有来自这些地址的请求，其 X-Forwarded-For 才会被采信——因为这个头是
    # 客户端可伪造的，而无条件采信会让「按 IP 限流」形同虚设（伪造 IP 绕过登录限流）。
    # 空 = 不信任任何代理（直连部署的正确默认值）；用 "*" 表示无条件信任（仅限
    # 上游已强制覆写该头的环境，风险自负）。用 Nginx/网关反代时必须填代理自身 IP。
    trusted_proxies: StrList = []
    # /metrics 访问令牌：配置后必须带 X-Metrics-Token 头。
    # ⚠️ 留空表示**完全不鉴权**（任何来源都能读到聚合指标，虽然不含买家内容）；
    # 生产环境请务必设置，``production_warnings()`` 会在启动时提醒。
    metrics_token: str = ""
    output_dir: Path = ROOT_DIR / "output"
    log_dir: Path = ROOT_DIR / "logs"

    @field_validator("cors_origins", "trusted_proxies", mode="before")
    @classmethod
    def _parse_cors_origins(cls, v: object) -> object:
        return _split_csv(v)

    llm: LLMSettings = Field(default_factory=LLMSettings)
    ragflow: RAGFlowSettings = Field(default_factory=RAGFlowSettings)
    milvus: MilvusSettings = Field(default_factory=MilvusSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    knowledge: KnowledgeSettings = Field(default_factory=KnowledgeSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    console: ConsoleSettings = Field(default_factory=ConsoleSettings)
    ratelimit: RateLimitSettings = Field(default_factory=RateLimitSettings)

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"非法日志级别: {v}")
        return v

    def ensure_dirs(self) -> None:
        for d in (
            self.output_dir,
            self.log_dir,
            self.knowledge.knowledge_base_dir,
            self.auth.users_db.parent,
            self.console.db_path.parent,
        ):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 生产环境校验
    # ------------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in {"production", "prod"}

    def production_issues(self) -> list[str]:
        """返回生产环境下**必须修**的问题（会让服务拒绝启动）。"""
        issues: list[str] = []
        if self.auth.secret_key == "dev-insecure-secret-change-me-in-production":
            issues.append(
                "AUTH_SECRET_KEY 仍是默认值——任何人都能用它伪造令牌，请改为随机 32+ 字符"
            )
        elif len(self.auth.secret_key) < 32:
            issues.append("AUTH_SECRET_KEY 长度不足 32 字符，强度不够")
        if not self.auth.seed_admin_password:
            issues.append(
                "AUTH_SEED_ADMIN_PASSWORD 为空——首次启动会生成随机密码并写入日志，生产环境请显式配置"
            )
        if self.cors_origins == ["*"]:
            issues.append("CORS_ORIGINS 为 *（允许任意站点跨域调用），生产环境请收紧为具体域名")
        return issues

    def production_warnings(self) -> list[str]:
        """生产环境下的建议项（只告警不拦截）。"""
        warnings: list[str] = []
        if not self.llm.api_key:
            warnings.append("未配置 LLM_API_KEY，问答链路会全部降级转人工")
        if not self.ragflow.api_key:
            warnings.append("未配置 RAGFLOW_API_KEY，检索后端不可用")
        if not self.log_json:
            warnings.append("LOG_JSON=false，生产环境建议开启结构化日志以便采集")
        if not self.metrics_token:
            warnings.append("未设置 METRICS_TOKEN，/metrics 对本机以外的来源开放")
        if not self.console.cache_enabled:
            warnings.append("问答缓存已关闭，重复提问会重复消耗 LLM 额度")
        if "*" in self.trusted_proxies:
            warnings.append(
                "TRUSTED_PROXIES=* 会无条件采信 X-Forwarded-For，"
                "伪造该头即可绕过按 IP 的登录限流；请改为具体代理地址"
            )
        return warnings


@lru_cache
def get_settings() -> Settings:
    return Settings()
