# 开发指南

## 1. 环境准备

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows（Linux/macOS: source .venv/bin/activate）
pip install -e ".[dev]"
pre-commit install                # 可选
```

## 2. 配置

复制 `.env.example` 为 `.env`：

| 变量 | 必填 | 说明 |
|------|------|------|
| `LLM_API_KEY` | ✅ | DeepSeek（默认）或任意 OpenAI 兼容供应商的 Key |
| `LLM_BASE_URL` / `LLM_MODEL` | | 默认 `https://api.deepseek.com/v1` / `deepseek-chat` |
| `RAGFLOW_API_KEY` | ✅（RAGFlow 后端时） | RAGFlow 后台「API」页签获取 |
| `RAGFLOW_BASE_URL` | | 默认 `http://localhost` |
| `RAGFLOW_DATASET_IDS` | | 知识库名称或 dataset id，可多个逗号分隔 |
| `RETRIEVAL_BACKEND` | | `ragflow`（默认）或 `milvus` |
| `RETRIEVAL_SCORE_THRESHOLD` | | 低于该相似度视为「资料不足」，默认 0.53（标定值，见 README §6.1） |
| `CONFIDENCE_THRESHOLD` | | 意图置信度阈值，默认 0.5 |

配置分层：`LLMSettings`(LLM_) / `RAGFlowSettings`(RAGFLOW_) / `MilvusSettings`(MILVUS_) /
`RetrievalSettings`(RETRIEVAL_) / `KnowledgeSettings`(无前缀) / `AgentSettings`(无前缀)。

## 3. 数据准备与入库

```bash
# 3.1 采集真实货源（需联网；已有结果可跳过）
python scripts/harvest_zhaojiafang.py --target 60

# 3.2 生成日语商品知识库文档（写入 data/knowledge_base/products/）
python scripts/build_kb_docs.py --clean

# 3.3 灌入 RAGFlow（幂等：同名跳过）
customer-support ingest
customer-support ingest --replace        # 同名重传
customer-support ingest --prune          # 清理本地已删除的远端文档（删前自动备份）
customer-support ingest --no-wait        # 只触发解析，不等待 deepdoc
```

解析状态机：`UNSTART → RUNNING → DONE / FAIL / CANCEL`。`ingest` 会轮询到全部终态，
并以退出码反映成败（`report.ok`），可直接串进 CI。

## 4. 常用命令

```bash
make lint / format / type / test / test-int / check

customer-support ask "商品はいつ届きますか？"
customer-support ask "Do you accept returns?" --language en
customer-support ask "洗える？" --session s1          # 多轮上下文
customer-support serve --port 8002 --reload           # 开发热重载
customer-support tickets                              # 当日转人工复盘
```

## 5. 测试策略

| 目录 | 内容 | 是否需外部依赖 |
|------|------|--------------|
| `tests/unit/` | 术语本地化、定价、目录渲染、意图、检索阈值/去重、会话、工单、语言识别、生成器、RAGFlow 后端（MockTransport） | ❌ 全离线 |
| `tests/integration/` | `ChatService` 正常路径 + 四条转人工分支 + 多轮改写；API 探针与端点 | ❌ 用假后端/假 LLM |

- 假实现集中在 `tests/conftest.py`（`FakeRetrievalBackend` / `FakeLLM` / `FakeVectorStore`）；
- 集成测试用 `pytest.mark.integration` 标记；`make test` 只跑单元，`make test-int` 跑集成；
- **改动转人工规则必须同步更新 `tests/integration/test_chat.py`**。

## 6. 质量门槛

- 提交前 `make check` 全绿（ruff + mypy + pytest）；
- 新逻辑必须带单元测试；外部依赖必须可替身；
- 定价参数、转人工阈值等业务常量不得散落在代码里，统一进 `config.py` 或对应模块顶部。

## 7. 加一个新语言

1. `i18n/translator.py` 的 `LANG_ALIASES` 加别名；
2. `rag/generator.py` 的 `LANGUAGE_HINTS` / `FALLBACK_ANSWERS` 加该语言的话术；
3. `services/chat_service.py` 的 `HUMAN_TRANSFER_ANSWERS` 加转人工话术。
