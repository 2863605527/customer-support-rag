# 改进路线图 · 对标 8 类开源竞品后的 17 条可落地项

> 本文要回答的问题是：**与市面上同类产品相比，当前实现还差哪些、怎么补、先补哪部分。**
>
> 与 [`docs/benchmark.md`](benchmark.md) 的分工：那份偏**架构层**能力对比；
> 本文偏 **UI/UX 与具体交互**，并标注每一项应改动的文件与数据来源。
>
> 引用具体产品行为时均在括号中标注来源；标注「未核实」的表示缺少权威依据，
> **不应当作事实使用**。竞品星标量级仅作参考（第三方来源的数字常常不一致）。

---

## 一、对标对象与事实依据

| 项目 | 我们对标它的什么 | 关键事实（来源） |
|---|---|---|
| **Dify** | 引用展示、知识库运维台、混合检索 | 「引用与归属」开启后，回复下方显示**被引用的分段**：原始分段文本、分段序号、匹配度，并可「跳转至知识库」；知识库有「召回测试」可调 TopK/Score 对比召回效果；检索方式含**向量 + 倒排索引（关键词）** 混合检索与 Rerank 重排（Dify 官方文档 / dify-docs 仓库）。官网自述 GitHub 148K+ star |
| **Chatwoot** | 坐席工作台信息架构、Copilot | 四区布局：左侧导航栏 / 中左**会话列表**（tab：Mine·Unassigned·All·Mentions·Unattended；状态：Open·Pending·Snoozed·Resolved）/ 中间会话视图（**Reply / Private Note** 双 tab）/ 右侧**上下文面板**（联系人详情、**Previous conversations**、指派/标签/团队/状态）；右侧 Copilot tab 可起草回复并「Use this」插入、翻译、语气改进、查帮助文章（Chatwoot 官方帮助中心）。star 量级 3 万（第三方来源数字不一致：28k / 34.5k） |
| **Chainlit** | 阶段可视化 | 把 retriever / llm / tool 作为可见 Steps 展示（见 `benchmark.md`） |
| **RAGAS** | 质量可度量 | faithfulness / answer_relevancy / context_precision / context_recall 四指标（见 `benchmark.md`） |
| **Langfuse** | 可观测 | 开源 + 自托管，OTel 协议（见 `benchmark.md`） |
| **Open WebUI / LibreChat** | 主题、导出、Artifacts | 主题切换、对话导出（量级未逐条核实） |
| **AnythingLLM** | 工作区引导、跨语言检索 | 工作区空态引导（未逐条核实） |
| **FastGPT** | 混合检索 + Rerank + RRF | 向量 + BM25 + cross-encoder + RRF（见 `benchmark.md`） |

**结论**：竞品的**平台化能力**（可视化编排、多租户、插件市场）本项目不该做——
它是个"跨境店铺智能客服"，不是通用 AI 应用平台。真正该补的是两块软肋：
**① 检索质量；② 质量可度量**。再加一块我们**已经具备但完全没展示出来**的能力：
**③ 坐了金矿却没挖 —— 后端已产出的结构化数据（引用/阶段/命中数/锚定标记/SLA）前端几乎没渲染。**

---

## 二、UI / UX 与交互（U1–U8）

### U1 引用来源：收起的三层折叠 → inline chip + 点击抽屉 ✅P0
- **主流**：Dify「引用与归属」把被引用分段（原文、序号、匹配度）直接列在回复下方，可跳转知识库。
- **我们现状**：`frontend/src/components/MessageItem.vue` 把引用埋在 `.meta` 折叠面板**内部**的**第二个**折叠 `<a>引用来源（N条）展开▼</a>`（第 128–141 行）——要看一眼出处得展开两次；且后端 `Reference.pinned`（是否商品链接**确定性锚定**命中）已经透传到接口，**前端一个字都没渲染**。
- **怎么落地**：`MessageItem.vue` 该段改为答案下方的 inline chip 行，每个 chip 显示 `出典名 · 分数`，`pinned=true` 时换成「精确定位」标签（这是本项目最强的差异化能力，必须让人看见）；点击 chip → 新增 `components/ReferenceDrawer.vue` 右侧抽屉，展示该条 `snippet` 全文 + `doc_id` + `score` + pinned 说明。**数据全部来自已有 `msg.result.references`，零后端改动。**
- **用户价值**：把"防幻觉"从后端概念变成**可见的信任凭证**；跨境售后争议时能当场对质"这句出自哪份文档"。
- **成本 S ｜ 无外部依赖**

### U2 正文内联引用角标 `[1][2]` ✅P1
- **主流**：Dify / Open WebUI 在正文句末插上标，点击定位来源。
- **怎么落地**：`src/customer_support/rag/generator.py` 的 `QA_PROMPT` 增加一条规则：「引用某条资料时在句末标注 `[n]`，n 为资料序号」；`MessageItem.vue` 渲染时把 `[n]` 替换为可点击上标，点击高亮 U1 的对应 chip。**必须做容错**：只识别落在 `1..len(references)` 的序号，越界/不存在的当普通文本。
- **用户价值**：多段答案（价格 + 尺码 + 素材）能逐句定位依据，比底部一张列表高一个量级。
- **成本 M ｜ 无外部依赖**

### U3 检索阶段显示"命中 N 段" ✅P0
- **主流**：Chainlit 把 retriever 阶段作为可见 Step。
- **我们现状**：`generator.answer_stream` **已经** yield 了 `("retrieved", hits)`，`chat_service.handle_stream` 也把它转成 status 事件，但步骤条上「检索知识库」只有阶段名——**后端早就把数字送出来了，前端没显示**。
- **怎么落地**：`chat_service.handle_stream` 在检索 status 事件补 `detail={"hit_count": len(hits)}`；`MessageItem.vue` 步骤行渲染成「检索知识库 · 命中 3 段」。
- **用户价值**：跨境检索常有 1–3s 等待；显示命中数让人确认系统**真的在查**而不是卡死，直接降低中途放弃率。
- **成本 S ｜ 无外部依赖**

### U4 坐席三栏工作台（会话队列 / 对话 / 上下文）✅P1
- **主流**：Chatwoot 的四区布局（见上表），核心是**会话列表 + 上下文面板**这两块我们完全没有。
- **我们现状**：只有 `WorkspaceView`（聊天）/ `TicketsView`（工单）/ `DashboardView`（指标）——**没有"会话队列"这一屏**，会话与工单是两张割裂的表。
- **怎么落地**：新增 `frontend/src/views/InboxView.vue` 三栏：
  - 左栏 → 已有 `GET /ops/conversations`（按 `handoff_count > 0` 先给"需人工"，加 mine/unassigned/all 本地分面）；
  - 中栏 → 复用 `components/ChatPanel.vue` + 已有 `GET /ops/conversations/{session_id}`；
  - 右栏 → `buyer_id`、语言、历史转人工次数、关联工单（`GET /ops/tickets?search=<session_id>`，`search` 已经支持按号/问题模糊匹配）。
  **后端接口已全部就绪，这是纯前端工作。**
- **用户价值**：从"能聊天"变成"能值班"——完整闭环 AI 转人工 → 队列 → 坐席接手 → 回复 → 关单。
- **成本 L ｜ 无外部依赖**

### U5 工单 SLA 倒计时条 + 优先级色标 ✅P1
- **主流**：Chatwoot 会话状态与时间线；SLA 计时是客服系统标配。
- **我们现状**：`store/tickets.py` 已经算出 `overdue`（未处理超 24h）与 `avg_first_response_minutes`，但 `TicketsView.vue` 只把它们当文字统计，**列表里看不出"哪单快超时"**。
- **怎么落地**：`TicketsView.vue` 每行加 SLA 条：剩余 = `created_at + 24h - now`，>8h 绿 / 2–8h 黄 / <2h 或已超红；优先级用色标（`urgent` 红、`high` 橙，复用 `main.css` 的 `.badge` 体系）。前端按已有字段计算即可。
- **用户价值**：值班主管一眼看出今天必须动哪几单——这才是工单系统存在的意义。
- **成本 M ｜ 无外部依赖**

### U6 知识库运维界面（文档清单 + 检索测试台）✅P1
- **主流**：Dify 知识库的「召回测试」（输入问题看召回分段与分数，可临时改参数对比）+ 分段管理。
- **我们现状**：`api/v1/ops.py` 已经提供 `GET /ops/knowledge/documents`、`POST /ops/knowledge/search`（返回每条命中分数 + 是否过线上阈值）、`POST /ops/knowledge/reingest`，**但前端没有任何入口**，只能 curl 或 Swagger。
- **怎么落地**：新增 `frontend/src/views/KnowledgeView.vue`：上半"文档清单"（名称/大小/分块数/解析状态，`run != DONE` 标黄）；下半"检索测试台"（输入框 + 阈值滑块 → 调 `/ops/knowledge/search`，命中卡片显示 `score` 与是否过 `RETRIEVAL_SCORE_THRESHOLD`）；右上"重新入库"按钮（调 reingest，提示"已接受，后台执行"）。
- **用户价值**：运营能自己判断"该补文档还是该调阈值"，不必等开发——这是「运营控制台」定位的最后一块拼图。
- **成本 M ｜ 无外部依赖**

### U7 暗色主题（顺手消灭硬编码色）✅P2
- **主流**：Open WebUI / LibreChat 支持主题切换。
- **我们现状**：`frontend/src/styles/main.css` 是**单一浅色 token 集**（`:root` 的 `--c-bg/--c-panel/--c-text/...`），且 `MessageItem.vue` 的评论框与标签有**硬编码浅色**（`background: #f8f9fb` / `#fff`），`main.css` 的 `.badge.warn/err/ok/dim` 也是硬编码浅底——直接切暗色会花。
- **怎么落地**：① 先把硬编码色收敛为 token（`--c-surface`、`--c-chip-bg`、`--c-badge-warn-bg` 等）；② 新增 `[data-theme="dark"]` 覆盖同名 token；③ `layouts/AppLayout.vue` 顶栏加切换按钮，写 `document.documentElement.dataset.theme` + `localStorage` 持久化。
- **用户价值**：夜间值班可读性；副产品是**样式体系化**，以后换品牌色只改一处。
- **成本 S ｜ 无外部依赖**

### U8 空态引导 + 高频问题快捷入口 ✅P2
- **主流**：AnythingLLM 工作区空态引导；Chatwoot pre-chat 表单（未逐条核实）。
- **我们现状**：`ChatPanel.vue` 空态只有一句欢迎语。
- **怎么落地**：空态加三组快捷问题卡（返品 / 送料 / サイズ，各 2–3 条，点击直接走 `useChat` 发送），文案从 `data/knowledge_base/policies/` 的真实政策主题里取；再加一条「可以直接贴商品链接提问」的提示——**这条尤其重要：确定性锚定是本项目最强能力，但没人知道可以这么用**。
- **用户价值**：首次进入零困惑、提高自助解决率；同时把两个旗舰能力（锚定 + 转人工）变成可被发现的入口。
- **成本 S ｜ 无外部依赖**

---

## 三、功能与检索质量（F1–F9）

### F1 混合检索：向量 + 关键词，RRF 融合 ✅P0
- **主流**：Dify 混合检索 = 向量 + 倒排索引（关键词）再重排；FastGPT 走 vector+BM25+RRF（来源见上表 / `benchmark.md`）。
- **我们现状**：`rag/retriever.py` 只做「召回 → 阈值过滤 → 来源去重」，完全依赖向量相似度。**商品番号**（`KB-0001` / 货源 `11748832`）这种精确匹配，在跨语言向量空间里天然容易被稀释。
- **怎么落地**：给 `RetrievalBackend` 增加**可选**方法 `search_keywords(query, top_k)`——沿用 `fetch_document` 的既有约定（**非抽象 + 默认返回 `[]`**，不支持的后端自动退化，绝不因增强能力失败而报错）；`Retriever.retrieve()` 内用 RRF（`score = Σ 1/(k + rank)`）融合两路结果，再走阈值过滤与 `max_per_source` 去重。RAGFlow 侧已有 `RAGFLOW_VECTOR_SIMILARITY_WEIGHT`（0=纯关键词 / 1=纯向量）可复用作权重起点。
- **用户价值**：直接拉准确率，尤其是带型号/番号的提问与日⇄中跨语言检索。
- **成本 M ｜ 无外部依赖**

### F2 Rerank 重排序（可插拔）✅P0
- **主流**：Dify 支持开启第三方 Rerank 模型对召回结果重排。
- **我们现状**：配置项 `RAGFLOW_USE_RERANK` **已存在**（默认 false），但 `Retriever` 只有阈值过滤，**没有本地可插拔的重排层**；一旦切到 Milvus 后端就完全没有重排能力。
- **怎么落地**：`Retriever.__init__` 增加可选 `reranker`；新增 `src/customer_support/rag/rerank.py`（`NoopReranker` + `HttpReranker`，配置 `RERANK_PROVIDER/API_KEY/MODEL/TOP_N`）；`rag/factory.py` 按配置装配；**调用失败一律退化为原顺序**（与 `fetch_document` 同一原则）。
- **用户价值**：直接减少"检索到了但排在 Top-K 之外 → 模型看不到 → 拒答转人工"这一类损失，是压低 `no_knowledge` 占比最直接的抓手。
- **成本 M ｜ 外部服务**可选（不配即 Noop）

### F3 黄金集回归评测 + CI 门禁 ✅P0
- **主流**：RAGAS 四指标（faithfulness / answer_relevancy / context_precision / context_recall），可接 CI（见 `benchmark.md`）。
- **我们现状**：`scripts/calibrate_threshold.py` 已能用 26 条标注问题标定阈值，但**没有端到端的答案质量回归**——改 prompt / 换分块 / 调阈值之后只能靠手感。
- **怎么落地**：新增 `scripts/eval_rag.py` + `data/knowledge_base/eval_set.jsonl`（问题 / 期望来源 doc_id / 答案要点），跑 `ChatService.handle()`，产出四类指标：**来源命中率**、拒答率、平均耗时、（可选）LLM-as-judge 打分；支持 `--baseline <上次报告>` 做对比输出，CI 里作为 `make eval` 门禁。
- **用户价值**：把"我觉得变好了"变成"来源命中率 0.79 → 0.92"。**能跑质量回归的 RAG 项目，和只会截图的项目不在一个档次。**
- **成本 M ｜ 无外部依赖**（judge 模型可选）

### F4 知识缺口闭环看板 ✅P1
- **主流**：客服系统普遍把"未解决 / 差评"聚合成待办，驱动知识运营。
- **我们现状**：`reason=no_knowledge` 的工单、`👎` + 原因标签的反馈、`/ops/feedback/dataset` 导出**都已存在**，但没有一屏把它们变成"该补哪些文档"。
- **怎么落地**：`DashboardView.vue` 新增「待补知识 TOP」——按 `question` 聚类 `no_knowledge` 工单 + 展示差评原因分布（数据来自已有 `GET /ops/tickets?reason=no_knowledge` 与 `GET /ops/feedback/stats`）；每条给一个"去建文档"按钮，跳到 U6 的知识库页并预填标题。
- **用户价值**：让知识库**自己长大**——每次拒答变成一个待办，而不是消失在日志里。
- **成本 M ｜ 无外部依赖**

### F5 坐席 Copilot：为工单起草回复 ✅P1
- **主流**：Chatwoot Captain Copilot 在右侧 tab 基于知识库与历史起草回复，「Use this」插入输入框（官方帮助中心）。
- **我们现状**：`ChatService._transfer` 已经把当时的命中来源写进 `Ticket.sources`、把兜底话术写进 `Ticket.asked_answer`——**坐席有上下文，但没有"照着资料写一版"的助力**。
- **怎么落地**：新增 `POST /ops/tickets/{ticket_id}/draft`（`require_admin` 或放宽到 agent）：用 `Ticket.question` + `Ticket.sources` 重新走一次 `AnswerGenerator`，**只返回草稿文本、绝不自动发送**；`TicketsView.vue` 回复框上方加「AI 起草」，结果填进 `textarea` 由人确认后走既有 `POST /ops/tickets/{id}/replies`。
- **用户价值**：退款/投诉这类高风险单，坐席能**有依据地快回**，降错误率与首次响应时长（正好对应已有 SLA 指标）。
- **成本 M ｜ 无外部依赖**

### F6 买家多会话画像 ✅P1
- **主流**：Chatwoot 右侧上下文面板有联系人详情与「Previous conversations」。
- **我们现状**：`ConversationStore` 按 `session_id` 存会话、`buyer_id` 已落库，但界面只能一次看一个会话，**看不到"这个买家之前问过什么"**。
- **怎么落地**：`api/v1/ops.py` 增加 `GET /ops/buyers/{buyer_id}`：聚合该 buyer 的会话数、提问总数、转人工次数、最近 N 条问题、语言偏好、关联工单号（全部来自已有两张表）；在会话/工单详情侧栏渲染。
- **用户价值**：坐席接手即知背景（"他上周已经问过退货了"），减少重复询问与误判。
- **成本 M ｜ 无外部依赖**

### F7 语义缓存（精确匹配 → 近邻匹配）✅P2
- **主流**：把 FAQ 缓存从字符串相等升级为向量近邻，命中率显著提升。
- **我们现状**：`store/cache.py` 用 `sha1(language + 规范化问题)` **精确匹配**——「送料はいくら？」与「送料はいくらですか？」是两条缓存。
- **怎么落地**：`AnswerCacheStore` 增加可选向量列（复用 `LLM_EMBEDDING_*`），`get()` 先精确、未命中再近邻检索（阈值可配）。⚠️ **必须保留既有三条正确性前提**：只缓存 `source=knowledge`、不转人工、且仅首轮无上下文——命中方式变了，这三条不能放松。
- **用户价值**：跨境 FAQ 高度重复，命中率提升 = 省钱 + 降延迟。
- **成本 M ｜ 依赖 embedding 模型**

### F8 结构化输出（JSON mode + Pydantic 校验）✅P2
- **主流**：主流框架要求意图/分类节点返回受约束 JSON，而不是从自由文本里正则抠。
- **我们现状**：`agents/intent_agent.py` 与 `i18n/translator.py` 都靠**解析模型自由文本**拿结构化结果——模型多写一句话就可能解析失败 → 退化为转人工。**这是整条防幻觉链路上唯一还带概率的一环。**
- **怎么落地**：`common/llm.py` 的 `create_llm` 支持 `response_format={"type": "json_object"}`；`IntentAgent` 用 Pydantic 做 schema 校验 + 失败重试一次。⚠️ DeepSeek 对该参数的支持情况**需按实际 key 实测确认**（本项目已有"模型名以 key 实测为准"的教训）。
- **用户价值**：把"意图识别偶发失败"从**概率**变成**确定**。
- **成本 S/M ｜ 依赖供应商能力（需实测）**

### F9 链路追踪接 OpenTelemetry / Langfuse ✅P2
- **主流**：Langfuse 开源自托管、走 OTel（见 `benchmark.md`）。
- **我们现状**：已有 `trace_id`（贯穿日志）、`stage_ms`（分阶段耗时）、token 与成本估算——**数据齐全但没有标准的对外出口**。
- **怎么落地**：`common/logging.py` 的 trace 上下文 + `ChatService` 的 `stage_ms` 变成 OTel span（`chat.handle` 为根 span，`retrieve` / `generate` / `intent` 为子 span）；用 OTLP exporter 指向 Langfuse 或任意兼容后端，**做成可选依赖**（不装即退回现有 JSON 日志）。
- **用户价值**：能按"哪一步慢"回答线上问题（是检索慢还是生成慢），而不是只看到总耗时。
- **成本 M ｜ 外部服务可选**

---

## 四、优先级建议

| 优先级 | 项 | 理由 |
|---|---|---|
| **P0** | **U1** 引用 chip + 抽屉 | 改动小、后端无需调整；让回答依据对买家可见，是"不编造"这条原则唯一能被直接感知的地方 |
| **P0** | **U3** 检索命中数 | 改动小；后端已返回该数据，前端漏了显示 |
| **P0** | **F3** 黄金集回归评测 | 让回答质量可度量，改动是否导致退步有据可依 |
| **P0** | **F1/F2** 混合检索 + Rerank | 直接决定答案正确率，跨语言与商品番号场景尤为明显 |
| P1 | U2 / U4 / U5 / U6 / F4 / F5 / F6 | 从"能用"到"能支撑日常值班"；其中 **U4 三栏工作台**是关键一步 |
| P2 | U7 / U8 / F7 / F8 / F9 | 主题、引导、语义缓存、结构化输出、可观测 |

**建议起点**：U1（引用可见）+ U4（坐席工作台）+ F3（质量回归）。
前两项让回答依据与会话上下文在界面上可见，第三项让质量变化可被持续观测。

---

## 五、与同类方案的差异

1. **防幻觉依赖确定性闭环，而非仅靠提示词约束**：阈值过滤 → `只依据资料` 的提示词 →
   模型答不了时必须输出哨兵 `NO_ANSWER`（`rag/generator.py`）→ 业务层确定性识别并转人工。
   多数实现止步于前两步；第三步保证"答不出"这件事一定被检测到，而不是取决于模型自觉。
2. **商品链接确定性锚定**：买家贴货源链接时，按 sku **直接取回**该商品文档切片并置顶
   （`ChatService._pin_source` → `Retriever.retrieve(pin_source=...)`，`score=1.0, pinned=True`，
   且豁免 `max_per_source`）。已知道是哪份文档时不再依赖相似度排序。
3. **转人工归因六分类，且与异常降级严格区分**：`high_risk_intent / low_confidence / no_knowledge /
   rate_limited / service_unavailable / system_error`。这层口径用于区分两类处置动作：
   补充知识库，还是排查外部依赖。
4. **工程完整度**：熔断 + LLM 健康（含欠费识别）+ 精确缓存 + SSE 思考步骤条 +
   Prometheus `/metrics` + 单容器单域名部署 + JWT / 验证码 / 限流 / 审计的账号体系。
   同类方案中，Chatwoot 侧重客服运营流程，Chainlit 侧重快速原型，Dify 侧重应用平台。
5. **质量闭环可回归**：每条回答的 👍/👎 与原因标签入库（`store/feedback.py`），
   并可导出评测样本（`/ops/feedback/dataset`），反馈数据可直接用于调优与评测。
