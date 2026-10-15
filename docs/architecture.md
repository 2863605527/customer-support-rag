# 架构设计

## 1. 分层与依赖方向

| 层 | 职责 | 关键约束 |
|----|------|---------|
| Entry（CLI / API） | 入口、参数校验、结果展示 | 不写业务逻辑 |
| **Services** | 业务编排（`ChatService`）、会话（`SessionStore`）、工单（`TicketService`） | **业务规则唯一集中地** |
| Agents | `IntentAgent` 意图识别 / `QAAgent` RAG 问答 | 只做单步，不判定转人工 |
| Knowledge | 加载 / 切片 / 入库 / 本地化 / 定价 / 目录 | 离线侧，与在线问答解耦 |
| RAG | `RetrievalBackend` 抽象 / `Retriever` / `AnswerGenerator` | 阈值过滤防幻觉 |
| i18n | 语言识别 / 翻译 | 字形优先，LLM 兜底 |
| Common / Models / Config | 日志(trace_id)、异常、LLM、配置、模型 | 被所有层依赖，不反向依赖 |

```
Entry ─▶ Services ─▶ {IntentAgent, QAAgent, Translator, SessionStore, TicketService}
                        │
                        ├─▶ RAG: Retriever ─▶ RetrievalBackend ─┬─▶ RagflowBackend（默认）
                        │                                        └─▶ VectorStoreBackend ─▶ MilvusStore
                        └─▶ RAG: AnswerGenerator ─▶ LLM
横切：Common(日志/异常/LLM) · Models · Config
```

**为什么「转人工」放在 Services 而不是 Agent**：转人工是业务规则，不是模型能力。
如果散落在各 Agent 里，行为会不一致（意图 Agent 转、问答 Agent 不转），也无法统一复盘。
因此 Agent 只产出**事实**（意图、置信度、是否检索到资料），由 `ChatService` 按规则裁决。

## 2. 核心流程

见 README §3 的流程图。要点：

1. `trace_id` 在入口生成，写入 `contextvars`，之后所有日志自动携带；
2. 语言识别 → 会话改写 → 意图识别 → 转人工判定 → RAG → 转人工统一出口；
3. 整个 `_handle_inner` 被 `try/except` 包住，**任何异常降级为 `system_error` 转人工**。

### 「异常降级」与「答案降级」的区别

| | 触发 | reason | 复盘口径 |
|---|------|--------|---------|
| 异常降级 | 模型超时、检索服务 502 | `system_error` | 依赖可用性问题，要告警 |
| 答案降级 | 检索无足够相关资料 | `no_knowledge` | 知识库覆盖问题，要补文档 |

两者都转人工，但**归因完全不同**，因此在模型里是两个字段（`reason` + `source`）。

## 3. 检索抽象的两级设计

```
VectorStore        底层向量库能力：insert / search(vector) / count / drop   （只有自建库需要）
RetrievalBackend   面向业务的文本检索：retrieve(query) -> list[RetrievalHit]  （上层只依赖它）
```

- `RagflowBackend` 直接实现 `RetrievalBackend`——解析/分块/向量化都在平台侧；
- `VectorStoreBackend` 把 `VectorStore` 适配成 `RetrievalBackend`（查询文本 → 向量 → 检索）；
- 归一化模型 `RetrievalHit(text, score, source, doc_id, metadata)` 让上层与后端解耦。

## 4. 防幻觉的三道闸门

1. **检索阈值**（`Retriever`）：低于 `RETRIEVAL_SCORE_THRESHOLD` 的切片直接丢弃，
   过滤后为空 → 判定「资料不足」，不给模型编造的机会；
2. **prompt 约束**（`AnswerGenerator`）：明确「只使用参考资料」，并给出固定兜底话术；
3. **来源去重**（`max_per_source`）：同一文档最多取 N 条，避免长文档霸占上下文。

## 5. 关键决策（ADR）

1. **转人工规则前置且集中**：意图层高风险即转；RAG 资料不足也转；两条路径都保证「不编造」。
2. **RAGFlow 为主、Milvus 可切换**：日常运维不碰向量库端口；抽象层保证不被平台锁定。
3. **回答必须可溯源**：`references[]` 带 source/doc_id/score/snippet，日志与 API 都可见。
4. **入库幂等且可审计**：同名文档默认跳过（`--replace` 覆盖），`--prune` 删前先备份；
   `IngestReport` 结构化输出，CI 可直接断言「期望 N 份 DONE」。
5. **会话记忆进程内 + TTL**：跨境内部工具并发不高，无运维成本；接口已抽象，换 Redis 只换实现。
6. **工单用 JSONL**：可读可 grep、零依赖；接口收敛在 `TicketService`，将来接飞书/自研工单只换实现。
7. **定价是公式不是随机数**：`pricing.py` 集中参数，客服报价与后台可对齐。
8. **数据可回溯**：原始 API 响应 → 选品 → 结构化目录 → 知识库文档，每一步都落盘。

## 6. 可观测性

- **trace_id**：中间件注入 + `X-Trace-Id` 回传，前端报障可直接对上日志；
- **日志**：JSON 结构化（生产）或人类可读（本地），`trace_id` 自动附加；
- **探针分离**：`/healthz` 只看进程（存活），`/readyz` 真去问检索后端 + 检查凭据（就绪）。
  用错探针的典型后果是「依赖挂了但容器一直重启」。

## 7. 扩展点

- 接入真实客服后台：IM 插件 / webhook 调 `/api/v1/chat`；
- 会话记忆换 Redis：实现同名 `SessionStore`；
- 工单接飞书：实现同名 `TicketService`；
- 增加语言：`translator.py` 加一条别名映射，无需改业务逻辑；
- 换模型：改 `LLM_BASE_URL` / `LLM_MODEL`（DeepSeek / SiliconFlow / OpenAI 均可）。
