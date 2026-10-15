# customer-support-rag

> 跨境店铺多语言智能客服 RAG 问答系统，面向 TikTok 日本站寝具店铺「くらし寝具」。

基于真实货源数据（`zhaojiafang.com` 采集的 120 个 SKU）+ 店铺政策文档构建 RAG 知识库，
自动回答海外买家的售前/售后/物流/商品咨询；**意图识别 + 检索阈值双闸门**把高风险与
「知识库答不上来」的问题强制转人工，绝不编造。

除问答链路外，还配套**运营控制台**——工单闭环（指派/流转/SLA）、会话与消息服务端持久化、
回答质量反馈与评测集导出、运营仪表盘（自助解决率/转人工归因）、问答缓存、
限流、LLM 熔断降级与配额健康、Prometheus 指标与全链路 trace_id。见 §8。

---

## 1. 它解决什么问题

跨境店铺客服的三个真实痛点：

| 痛点 | 本项目的做法 |
|------|-------------|
| **买家多语言**（日/英/西/韩…），客服不懂 | 语言自动识别（字形优先、Latin 走 LLM），用买家语言回答；知识库跨语言检索 |
| **商品/政策问题重复且量大** | RAG 检索 → 基于知识库生成回答，带**可溯源引用** |
| **退款/投诉/纠纷不能自动承诺；不能瞎答** | 三条转人工规则 + 异常降级，全部转人工并自动建工单 |

---

## 2. 快速开始

> 📖 **第一次接触本项目？先看 [`docs/getting-started.md`](docs/getting-started.md)** —— 从零启动的
> 详细步骤：需要哪些前置（尤其是外部 RAGFlow 服务）、每一步做什么、如何验证跑通、常见错误怎么排查。
> 下面这份是速查版。

```bash
# 1. 虚拟环境 + 安装（含开发依赖）
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -e ".[dev]"

# 2. 配置密钥
copy .env.example .env
#   LLM_API_KEY=sk-...            ← DeepSeek（默认）或任意 OpenAI 兼容供应商
#   RAGFLOW_API_KEY=ragflow-...   ← RAGFlow 后台「API」页签

# 3. 准备数据（已包含真实采集结果，可跳过）
python scripts/harvest_zhaojiafang.py --target 120   # 采集真实货源（120 SKU）
node scripts/harvest_goods_detail.mjs                # 采集商品详情（材质/规格，幂等增量）
python scripts/build_kb_docs.py                      # 生成日语商品知识库文档

# 4. 把知识库文档灌入 RAGFlow
customer-support ingest

# 5. 命令行问答（自动识别语言）
customer-support ask "商品はいつ届きますか？"
customer-support ask "Do you accept returns?" --language en

# 6. 启动 API
customer-support serve --port 8002
#    Swagger: http://127.0.0.1:8002/docs

# 6'. Docker 部署（推荐：多阶段构建，前端编译进镜像，单容器单域名）
docker compose up -d --build
#    打开 http://127.0.0.1:8002/ 即登录页
#    ⚠️ 容器内连 RAGFlow 必须用 9380（API 直连端口）：
#       RAGFLOW_BASE_URL=http://host.docker.internal:9380   （compose 已默认注入）
#       经 80 端口走 host.docker.internal 会 502。

# 7. （可选）前端开发模式（改前端代码热更新；生产直接由后端托管 dist）
cd frontend
npm install
npm run dev
#    打开 http://localhost:5173 —— 聊天界面 + 引用溯源 + 转人工/工单徽章
```

> 检索后端默认 **RAGFlow**（文档解析/分块/向量化都由平台承担）；
> 也保留 **Milvus** 可切换：`RETRIEVAL_BACKEND=milvus`（见 §6）。

---

## 3. 端到端处理流程

```
                         ┌──────────────────────────────────────┐
 买家咨询（多语言） ──────▶│ ChatService.handle()                 │
                         │  trace_id 贯穿全链路                  │
                         └───────────────┬──────────────────────┘
                                         │
                    ① 语言识别（字形 → LLM 兜底，失败回退日语）
                                         │
                    ② 会话记忆 + 指代消解（QueryRewriter，失败即用原问题）
                                         │
                    ③ 意图识别 IntentAgent（严格 JSON，解析失败兜底转人工）
                                         │
              ┌──────────────────────────┼──────────────────────────┐
              │ 高风险 human              │ 置信度 < 阈值             │
              ▼                          ▼                          │
   ┌──────────────────────┐   ┌──────────────────────┐              │
   │ 转人工 high_risk     │   │ 转人工 low_confidence│              │
   └──────────┬───────────┘   └──────────┬───────────┘              │
              │                          │                          │
              │                          │        ④ 正常路径 RAG      │
              │                          │      Retriever 阈值过滤     │
              │                          │            │              │
              │                          │   ┌────────┴────────┐     │
              │                          │   │ 有资料           │ 无资料│
              │                          │   ▼                 ▼     │
              │                          │ 生成回答+引用     转人工 no_knowledge
              └──────────────────────────┴────────┬─────────────────┘
                                                  ▼
                              ⑤ 转人工统一出口：话术 + 写工单（SQLite）+ 记会话
```

**任何未预期异常**（模型超时、检索服务挂掉）都会在 `ChatService.handle()` 里被兜住，
降级为 `system_error` 转人工 —— **买家永远不会收到 500**。

> 归入 ④ 的还有一步**商品链接识别**：买家贴了货源链接 / SKU 时，按 `KB-XXXX.md`
> **确定性锚定**该商品文档（不指望相似度排得上来），详见 §6.2。

---

## 4. 转人工的四条规则（业务安全边界）

| reason | 触发条件 | 为什么 |
|--------|---------|--------|
| `high_risk_intent` | 意图 = `human`（退款/投诉/纠纷/暴言/法律） | 客服不能自动承诺，必须人工 |
| `low_confidence` | 意图置信度 < `CONFIDENCE_THRESHOLD`（默认 0.5） | 宁转人工不猜 |
| `no_knowledge` | 检索切片全部低于 `RETRIEVAL_SCORE_THRESHOLD`（默认 0.53，见 §6.1） | **防幻觉第一道防线**：没资料就说不确定 |
| `system_error` | 链路中任何未捕获异常 | 依赖挂了也不能让买家看到错误页 |

每次转人工都会创建一个工单，落到 **SQLite**（`output/console/console.db`，与会话/反馈/缓存共库），
这是一个**可流转、可指派、可回复**的实体，不是一行日志：

- 状态流转 `open → in_progress → resolved → closed`（回退到处理中会清空解决时间，不留脏数据）
- 默认优先级按原因推导：`service_unavailable` → urgent、`high_risk_intent` → high、`no_knowledge` → normal
- 人工回复 / 内部备注（内部备注**不计入**首次响应 SLA）
- SLA 时间戳：`first_response_at` / `resolved_at`，看板给出平均首次响应与平均解决时长

命令行仍可快速复盘（走同一份存储）：

```bash
$ customer-support tickets
{
  "total": 12, "open": 12,
  "by_reason": {"high_risk_intent": 3, "no_knowledge": 8, "low_confidence": 1},
  "by_intent": {"human": 3, "logistics": 5, "aftersale": 3, "presale": 1}
}
```

> 网页里是「**工单中心**」页面：筛选 / 指派 / 状态流转 / 回复在同一页面完成（状态流转与回复需管理员）。
> 见 §8。

---

## 5. 知识库与真实数据

### 5.1 数据流水线（可回溯）

```
zhaojiafang.com 公开 API
   └─ scripts/harvest_zhaojiafang.py
        └─ data/raw/zhaojiafang/harvest_all.json      # 1665 候选 → 1170 去重
             └─ products_selected.json                # 按品类配额选出 120 SKU
                  └─ scripts/build_kb_docs.py
                       └─ data/knowledge_base/products/KB-0001..0120.md  (日语商品文档)
                       └─ data/raw/zhaojiafang/catalog.json             (结构化目录)
                            └─ customer-support ingest → RAGFlow
```

### 5.2 中文货源名 → 日语商品文档

货源名是关键词堆砌写法（如 `新款绣花防尘1.8m加密帐纱可升降导轨蚊帐加厚铝合金支架 1.5m（5英尺）床 冰花`），
逐词机翻会语序错乱、术语不准。本项目采用**结构化重组**（`knowledge/localization.py`）：

1. 按最长匹配「消费」素材词 / 品类词 / 规格词（`全棉双层纱` 不会被 `全棉` 重复翻译）；
2. 尺寸、件数、花型分别抽成独立字段；
3. 面向买家的标题只由**已识别的日语属性**拼装，中文原词只留在「社内参考 / 検索キーワード」段。

例：`KB-0001.md`（铝合金蚊帐）

```markdown
# アルミ合金 蚊帳 ダブル（幅150cm）／冰花
商品番号：**KB-0001**　ジャンル：蚊帳・寝具アクセサリ

## 価格
- 販売価格：**17,000円**（税込）
...
## 社内参考情報
- 原題（中国語）：新款铝合金可升降导轨蚊帐 1.5m（5英尺）床 冰花
- 仕入先：海虹蚊帐
- 貨源番号：11748832
- 仕入価格（参考）：240.00元 → 国内段コスト 276.00元 → 販売価格 17,000円
```

### 5.3 定价模型（`knowledge/pricing.py`）

国内批发价（CNY）→ 日本站零售价（JPY 税込）的**显式公式**，不是随机数：

```
零售价 = 批发价 × 1.15(国内段成本) × 20.5(汇率) × 3.0(跨境加价)  → 取整到 100 円 → 不低于 480 円
```

参数集中在模块顶部，改一处全库生效；`cost_breakdown()` 输出定价明细，写入文档的
「社内参考情報」段，客服核对时不需再打开外部页面。

### 5.4 文档目录结构

```
data/knowledge_base/
├── products/KB-0001..0120.md  # 120 个 SKU 的日语商品文档（脚本生成）
├── policies/                  # 10 份政策文档（人工撰写 9 份 + 脚本生成 1 份）
│   ├── after_sales_policy.md         # 退换货政策（30 天）
│   ├── shipping_policy.md            # 物流时效与运费
│   ├── payment_and_tax.md            # 支付方式与关税
│   ├── quality_warranty.md           # 质检与质保
│   ├── faq.md                        # 高频问题
│   ├── care_and_washing.md           # 洗涤保养（按材质）
│   ├── bedding_materials.md          # 材质种类与安全性
│   ├── customs_and_parcel_issues.md  # 通关与配送异常处理
│   ├── order_changes.md              # 下单后变更与取消
│   └── material_guide.md             # 全店材质构成（脚本生成）
└── store/                     # 3 份店铺文档
    ├── store_profile.md       # 店铺介绍与转人工场景
    ├── size_guide.md          # 寝具尺寸对照表
    └── bedding_selection.md   # 季节/用途别选购指南
```

---

## 6. 检索后端：RAGFlow（默认）/ Milvus（可切换）

```
Retriever ──依赖──▶ RetrievalBackend（文本进、切片出）
                       ├── RagflowBackend   默认：调 /api/v1/retrieval，向量化在平台侧
                       └── VectorStoreBackend
                              └── MilvusStore   自建向量库（pymilvus，需 pip install -e ".[milvus]"）
```

两层抽象的用意：上层 `Retriever` 只关心「给问题文本、拿回切片」，不关心底层是平台还是自建库。
切换只需改配置：

```bash
RETRIEVAL_BACKEND=ragflow   # 默认
RETRIEVAL_BACKEND=milvus    # 备选；再执行 customer-support build-kb
```

两点关键设计：

- **名称与 ID 都支持**：`RAGFLOW_DATASET_IDS=tiktok_bedding_kb`，首次调用惰性解析成真实 dataset id，
  换环境不用手抄 ID；
- **二次阈值过滤**：RAGFlow 侧用宽松阈值粗筛（0.15），本项目在 `Retriever` 里按
  `RETRIEVAL_SCORE_THRESHOLD` 精筛一次 —— 保证「资料不足 → 转人工」不被平台默认值影响。

### 6.1 阈值要标定，不能拍脑袋

不同平台 / embedding 模型给出的 `similarity` 量纲差别很大。若沿用「0.8 才算相关」的直觉，
正常问题会被全部判成「资料不足」，线上表现为「永远在转人工」。

本项目用一份标注集把两个参数**量了出来**（`data/eval/retrieval_eval.json`：16 条相关问题 / 10 条无关问题）：

| `RAGFLOW_VECTOR_SIMILARITY_WEIGHT` | 相关问题的最高分 | 无关问题的最高分 | 可分性 |
|---|---|---|---|
| 0.3（关键词主导） | 0.23 ~ 0.49 | ≤ 0.31 | ❌ 区间重叠严重 |
| **0.7（向量主导）** | **0.52 ~ 0.67** | **≤ 0.54** | ✅ 基本分离 |

阈值扫描（weight=0.7）：

| 阈值 | 召回 | 误检 | Youden J |
|---|---|---|---|
| 0.52 | 16/16 (100%) | 3/10 | 0.70 |
| **0.53** | **15/16 (94%)** | **1/10** | **0.84** ← 采用 |
| 0.54 | 13/16 (81%) | 0/10 | 0.81（更保守的备选） |

```bash
# 重新标定（换 embedding 模型 / 知识库大改后务必重跑）
make calibrate
python scripts/calibrate_threshold.py --verbose
# CI 门禁：召回率低于阈值即失败
python scripts/calibrate_threshold.py --check --min-recall 0.9
```

脚本会打印每个问题的逐条得分、阈值扫描表与建议值，并单列「未召回期望来源」的问题
（这类是检索能力问题，调阈值救不了，需要补文档或改分块）。

### 6.2 商品链接问答：确定性锚定，不让相似度碰运气

买家经常直接贴一个货源链接再问「これの素材は？」「120cm のベッドでも使える？」——
这类问题的关键词本身没有检索价值（「这个商品」），能锁定商品的锚点只有 URL 里的货源编号。

`CatalogIndex` 把 URL / SKU 映射到商品后，**不是**只把商品名拼进检索词就完事：

| 手段 | 作用 | 是否保证命中 |
|---|---|---|
| 检索词注入（`augment_query`） | 让该商品文档**更容易**排上来 | ❌ 不保证 |
| **确定性锚定**（`fetch_document`） | 按 `KB-XXXX.md` 把该商品文档的切片直接取回并置顶 | ✅ 保证 |

为什么必须锚定：**实测目标商品文档的相似度只有 0.549、排不到第一**——跨语言查询叠加
同系列近义标题（「アルミ合金 蚊帳 ダブル」vs 其他蚊帳）会让同款不同号互相抢位。结果就是
模型拿不到**该商品**的资料，误判「资料不足」而拒答转人工（真实踩坑，不是理论顾虑）。

实现要点：

- `RetrievalBackend.fetch_document(source)` 是**非抽象**方法，默认返回空列表——不支持该
  能力的后端（如自建 Milvus）自动退化为纯相似度检索，**纯增强，不会让原本能答的问题变差**；
- `Retriever.retrieve(..., pin_source=...)` 把锚定切片置顶并**豁免 `max_per_source`**
  （同一商品文档的切片有意全给），相似度只补足剩余名额、且不再从该来源取材；
- 锚定切片 `score=1.0` + `pinned=true`，与真实相似度在 API 引用与工单里可区分；
- 锚定失败（远端无此文档 / 请求异常）只记日志并退化，**绝不因为取不到锚定文档就让买家收到失败**；
- 文档名匹配带「去 `(1)` 副本后缀」兜底，历史遗留副本不会让锚定悄悄失效。

> 锚定依赖**文档名规范**，因此 `scripts/reconcile_ragflow.py` 除了删副本，还会把幸存者
> **改名回 `KB-XXXX.md`**——RAGFlow 对同名上传会自动加 `(1)` 后缀，名字不对锚定就失效。

---

## 7. API

> **全部业务接口都需要登录**：先调 `/api/v1/auth/login` 拿 `access_token`，
> 之后每个请求带 `Authorization: Bearer <token>`。仅 `/healthz`、`/readyz` 与 `/auth/*` 公开。

### 7.1 鉴权接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/auth/captcha` | 图形验证码（失败达阈值后登录必填） |
| POST | `/api/v1/auth/register` | 注册（密码需满足策略） |
| POST | `/api/v1/auth/login` | 登录，返回 access + refresh 令牌 |
| POST | `/api/v1/auth/refresh` | 刷新令牌（**刷新即轮换**，旧的立即作废） |
| POST | `/api/v1/auth/logout` | 登出（撤销 refresh 令牌） |
| GET | `/api/v1/auth/me` | 当前用户 |
| POST | `/api/v1/auth/change-password` | 改密（撤销该用户全部刷新令牌） |
| GET | `/api/v1/auth/users` | 用户列表（**管理员**） |
| GET | `/api/v1/auth/audit-logs` | 登录审计（**管理员**） |

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8002/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@kurashi-bedding.jp","password":"YOUR_PASSWORD"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

curl -X POST http://127.0.0.1:8002/api/v1/chat \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"question":"商品はいつ届きますか？","session_id":"s1"}'
```

### 7.2 业务接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/chat` | 处理一条买家咨询（整段返回） |
| POST | `/api/v1/chat/stream` | **SSE 流式**：`status`（思考阶段）→ `delta`（逐字）→ `replace`（兜底替换）→ `done`（终态） |
| POST | `/api/v1/sessions/{id}/reset` | 清空会话记忆 |
| POST | `/api/v1/feedback` | 提交回答评价（👍/👎 + 原因），买家侧唯一写入口 |
| GET | `/api/v1/feedback/reasons` | 反馈原因标签（供前端一键选择） |
| GET | `/healthz` | 存活探针（只看进程，免鉴权） |
| GET | `/readyz` | 就绪探针（真问检索后端 + **LLM 可用性/欠费**，免鉴权） |
| GET | `/metrics` | Prometheus 文本指标（可配 `METRICS_TOKEN` 保护） |

响应关键字段：

| 字段 | 含义 |
|------|------|
| `answer` | 客服回答（转人工时是转人工话术） |
| `source` | `knowledge` / `no_answer` / `human` |
| `need_human` / `human_reason` | 是否转人工 / 转人工原因 |
| `references[]` | 命中的知识库来源（source / doc_id / score / snippet / `pinned`；`pinned=true` 表示由商品链接**确定性锚定**，非相似度命中） |
| `ticket_id` | 转人工工单号 |
| `trace_id` / `latency_ms` | 链路追踪号（与日志对应）/ 端到端耗时 |
| `cached` | 是否命中问答缓存（命中时不消耗模型额度） |
| `stage_ms` | 分阶段耗时：`language` / `rewrite` / `intent` / `retrieval_generate` |
| `prompt_tokens` / `completion_tokens` / `estimated_cost` | token 用量与成本估算 |

流式接口的事件序列（前端据此渲染 DeepSeek 式「思考过程」）：

```
status:detect_language → status:rewrite → status:intent
  → status:retrieval(可带 matched_product) → status:generate(带 hits=N)
  → delta × N（逐字）→ done(result)      # 资料不足时以 replace 兜底整段替换
```

---

## 8. 运营控制台

「能问答」只是及格线；这一层回答的是**跑起来之后怎么运营**——单子有没有人接、
回答质量好不好、模型挂了怎么办、成本花在哪。对应前端顶栏的「工作台 / 工单中心 / 运营仪表盘」。

### 8.1 工单全生命周期（`store/tickets.py` + `views/TicketsView.vue`）

状态流转、优先级、指派、人工回复、SLA（首次响应 / 解决时长）全部持久化。
**AI 拿不准的问题不会消失，而是变成一张有主、有 SLA、能被清零的单。**

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/ops/tickets` | 列表（按状态/优先级/原因/处理人/关键词筛选 + 分页） |
| GET | `/ops/tickets/{id}` | 详情（含全部回复） |
| PATCH | `/ops/tickets/{id}` | 更新状态 / 优先级 / 指派（**管理员**） |
| POST | `/ops/tickets/{id}/replies` | 人工回复 / 内部备注（**管理员**） |
| GET | `/ops/tickets/stats` | 看板：积压 / 超 24h / 未指派 / 平均响应与解决时长 |
| GET | `/ops/tickets/summary` | 按原因 / 意图汇总 |

### 8.2 会话与消息服务端持久化（`store/conversations.py`）

一问一答**成对原子落库**（避免只写了问题没写回答的中间态），并维护会话摘要
（消息数、转人工次数）。用途：跨设备复盘、换浏览器也能看到同一批历史、
以及所有运营指标的**数据底座**。前端侧栏「历史会话」点击即可切回任意一通对话。

> 与浏览器 localStorage 的区别：localStorage 只是**当前浏览器的渲染快照**（防止刷新丢消息），
> 服务端记录才是权威数据源。合规上，管理员可 `DELETE /ops/conversations/{id}` 按请求删除买家数据。

### 8.3 回答质量反馈闭环（`store/feedback.py`）

每条 AI 回答下方有 👍/👎。**差评必须选原因**（否则无法转化为改进项），
原因分布直接指向下一步该改什么：

- 「缺该商品资料 / 信息不全」→ 补知识库文档
- 「答非所问」→ 改 prompt 或调检索阈值
- 「语言不对」→ 查语言识别

管理员可 `GET /ops/feedback/dataset` 导出 **JSONL 回归评测集**，改完 prompt/阈值后跑对比。

### 8.4 运营仪表盘（`store/analytics.py` + `views/DashboardView.vue`）

核心指标是 **自助解决率（containment rate）= 未转人工的回答 / 回答总数**——它直接对应
「AI 替人挡掉了多少工作量」。其余指标用来回答「为什么、下一步改哪」：

- 提问/回答/转人工/会话数、平均延迟（**缓存命中不计入**，否则会掩盖真实耗时）
- 按天趋势、意图分布、回答来源分布、**转人工原因分布**
- 工单积压、差评原因 TOP、LLM 累计调用/失败、各熔断器状态

### 8.5 精确问答缓存（`store/cache.py`）

**同问题（同语言、且无会话上下文）** 直接复用历史回答，带 `cached` 标记。

- 只缓存「有知识库依据且未转人工」的回答——兜底话术不该被复用；
- 只在首轮生效：多轮追问依赖上文，吃缓存必然答非所问（有专门测试固化这条）；
- 命中时按固定粒度分块"重放"，前端仍有逐步出现的观感，但**不消耗任何模型额度**。

### 8.6 熔断降级与配额健康（`common/resilience.py`）

设计起因是一个真实故障：**DeepSeek 账户欠费（402 Insufficient Balance）时，
表现为「所有问题都转人工」**，不翻日志看不出是欠费。

- `CircuitBreaker`：连续失败 N 次打开 → 后续请求**快速失败**（不再每次去撞墙等超时）；
  冷却后进入半开态放一个探针，成功则闭合；
- `LLMHealthMonitor`：分类记录错误（`quota` / `auth` / `rate_limit` / `timeout` / `connection`），
  欠费时 `/readyz` 返回 503 且 `checks.llm.quota_exhausted=true`，前端顶栏直接显示「LLM 欠费」徽章；
- 转人工原因区分 `service_unavailable`（依赖挂了）与 `no_knowledge`（知识库没料）——
  这个区分决定了值班该去**修依赖**还是**补文档**。

### 8.7 接口限流（`common/ratelimit.py`）

令牌桶（突发容量 + 长期速率），聊天**按登录用户 ID** 限流（换 IP 也限得住，防代理池），
登录按来源 IP 限流（防脚本撞库）；超限返回 `429` + `Retry-After`。

### 8.8 结构化可观测性

- 每个请求一个 `trace_id`，经 `contextvars` 贯穿日志与响应头 `X-Trace-Id`，
  回答结果里也带 `trace_id`——**买家报障时能直接对到那一条日志**；
- 回答完成后统一记录：来源、引用数、总耗时、分阶段耗时、token 用量、成本估算；
- `/metrics` 输出 Prometheus 文本（会话数、回答数、转人工率、延迟、缓存命中…），
  只含聚合值，不含任何买家内容。

### 8.9 知识库运维 API（免命令行）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/ops/knowledge/documents` | 远端文档清单（解析状态 / 分块数）——观察入库进度 |
| POST | `/ops/knowledge/search` | **在线检索测试**：调 top_k / 阈值看命中，用于调参 |
| POST | `/ops/knowledge/reingest` | 触发重新入库（**202 立即返回**，后台线程执行） |

> 重新入库之所以是 202 + 后台执行：上传与解析可能耗时数分钟，塞进请求-响应周期会导致
> 网关超时 / 前端转圈 / 重试重复触发；正确做法是接受任务 + 后台跑 + 用文档清单接口看进度。

### 8.10 安全响应头与生产配置校验

- 安全响应头中间件：`X-Content-Type-Options` / `X-Frame-Options` / `Referrer-Policy` /
  `Permissions-Policy` / `CSP`（生产额外加 HSTS）；
- `ENVIRONMENT=production` 时**弱配置直接拒绝启动**：默认 JWT 密钥、开放 CORS、
  空管理员密码——「起不来看得见，配置不安全看不见」，所以宁可起不来。

---

## 9. 鉴权与账号安全

账号数据落在 SQLite（`output/auth/users.db`，真实持久化，已在 `.gitignore` 内），
首次启动自动创建管理员 `AUTH_SEED_ADMIN_EMAIL`（密码取 `AUTH_SEED_ADMIN_PASSWORD`，
留空则随机生成并打印到服务端日志的 WARNING 行）。

| 安全机制 | 实现 |
|---|---|
| 密码存储 | bcrypt（cost=12，随机盐），拒绝 >72 字节而非静默截断 |
| 密码策略 | ≥10 位 + 字母数字 + ≥4 种字符 + 拒弱口令 / 拒邮箱账号名；错误信息逐条可执行 |
| 登录失败保护 | 连续 5 次锁 15 分钟（`429` + `Retry-After`），失败满 3 次起强制图形验证码 |
| 图形验证码 | 自绘 SVG；**答案只存服务端**（不回传、通过即作废、限 5 次、5 分钟过期） |
| 用户枚举防护 | 账号不存在与密码错误返回**同一句**提示，且都做一次哈希以对齐耗时 |
| 令牌 | access 60min（无状态）+ refresh 14d（**落库可撤销**、刷新即轮换、类型不可混用） |
| 账号回收 | `is_active=false` 拒绝登录并撤销全部令牌；改密同样全端下线 |
| 审计 | 注册 / 登录成功失败 / 锁定 / 登出 / 刷新 / 改密 全部落库，含 IP 与 UA |
| 权限 | `agent` 只能用自己的工作台；用户列表与审计仅 `admin` 可读（未知角色一律 403） |

> 已知取舍（工程权衡）：
> - 令牌存 localStorage，若需抗 XSS 窃取应改 HttpOnly Cookie + CSRF。
> - 限流已有（§8.7，聊天按用户、登录按 IP），但是**单进程内存**实现——多副本部署时
>   每个副本各限一份，要全局精确限流需把令牌桶换成 Redis（接口已抽象在 `get_limiter`）。
> - 图形验证码的答案存在**服务端进程内表**（多副本需换 Redis）。这一点是刻意选的：
>   早先版本为了"无状态"把答案 base64 写进 `captcha_id`，而 base64 不是加密——
>   任何客户端 decode 一下就读到了答案，"防撞库"完全失效（0.6.0 已修，
>   见 `auth/captcha.py` 的 docstring）。**安全优先于无状态。**

```bash
# 运维开号 / 改密 / 停用（无需走注册接口）
customer-support users create --email agent@kurashi-bedding.jp --password 'Str0ng-Passw0rd!' --name 小张
customer-support users list
customer-support users set-password --email agent@kurashi-bedding.jp --password 'N3w-Passw0rd!'
customer-support users set-active --email agent@kurashi-bedding.jp --inactive
```

---

## 10. 目录结构

```
customer-support-rag/
├── pyproject.toml / Makefile / .env.example
├── docker-compose.yml / Dockerfile
├── scripts/
│   ├── harvest_zhaojiafang.py       # 采集真实货源列表（--target 指定 SKU 数）
│   ├── harvest_goods_detail.mjs     # 采集商品详情（材质/规格；H5 接口签名 + AES 解密）
│   ├── build_kb_docs.py             # 生成日语商品知识库文档（含详情富化）
│   ├── reconcile_ragflow.py         # RAGFlow 文档收敛：删副本 + 改名回规范名
│   ├── calibrate_threshold.py       # 检索阈值标定
│   └── verify_stream_link.py        # 端到端验证（流式 + 商品链接问答）
├── src/customer_support/
│   ├── config.py                    # pydantic-settings 分层配置 + 生产校验
│   ├── main.py                      # CLI: ingest / build-kb / ask / serve / tickets / users
│   ├── models/                      # knowledge / message / intent / ticket / conversation
│   │                                #   / feedback / console / user
│   ├── common/                      # logging(trace_id) / exceptions / llm
│   │   ├── resilience.py            #   熔断器 + LLM 健康监控（欠费可见）
│   │   └── ratelimit.py             #   令牌桶限流
│   ├── store/                       # 运营存储（同一 SQLite，各自建表）
│   │   ├── base.py                  #   连接/事务/时间序列化共用基类
│   │   ├── conversations.py         #   会话与消息（成对落库）
│   │   ├── tickets.py               #   工单全生命周期
│   │   ├── feedback.py              #   回答质量反馈 + 评测集导出
│   │   ├── cache.py                 #   精确问答缓存（TTL）
│   │   └── analytics.py             #   运营指标聚合 + /metrics 文本
│   ├── knowledge/                   # loader / splitter / builder
│   │   ├── ragflow_ingest.py        #   → RAGFlow 入库（幂等、可审计、可收敛）
│   │   ├── localization.py          #   中文→日语术语表与结构化重组
│   │   ├── pricing.py               #   跨境定价模型
│   │   └── catalog.py               #   货源记录 → 日语商品文档
│   ├── rag/                         # base(抽象) / ragflow_backend / milvus_store
│   │   ├── retriever.py             #   确定性锚定 + 阈值过滤 + 来源去重
│   │   ├── generator.py             #   防幻觉 prompt + 兜底话术 + 流式
│   │   ├── catalog.py               #   货源链接 → 商品（锚定文档名依据）
│   │   └── factory.py               #   按配置装配后端
│   ├── agents/                      # intent_agent / qa_agent
│   ├── i18n/                        # translator（字形优先 + LLM 兜底）
│   ├── services/                    # chat_service / session / ticket
│   └── api/                         # FastAPI v1（chat/auth/ops）+ deps 装配
├── frontend/                        # Vue 3 + Vite + TS
│   ├── src/layouts/AppLayout.vue    #   顶栏导航 + 用户菜单（外壳）
│   ├── src/views/                   #   Workspace / Tickets / Dashboard / Login / Register
│   ├── src/components/              #   ChatPanel / MessageItem / SidePanel / Composer
│   └── src/composables/             #   useChat / useAuth / useHealth
├── tests/                           # unit / integration
├── data/
│   ├── knowledge_base/              # 知识库文档（products/policies/store）
│   └── raw/zhaojiafang/             # 采集原始数据（列表 + 详情 + catalog.json）
├── docs/                            # getting-started / architecture / development / tiktok-integration / audit
└── output/ logs/                    # 运营库（console.db）/ 用户库 / 日志（运行期生成）
```

---

## 11. 开发与质量

```bash
make lint        # ruff check src tests scripts
make format      # ruff format + --fix
make type        # mypy src
make test        # 单元测试（不依赖外部服务）
make test-int    # 集成测试（假后端，覆盖四条转人工分支）
make calibrate   # 检索阈值标定（用标注集量出 RETRIEVAL_SCORE_THRESHOLD）
make check       # lint + type + test 全绿
```

- 单元测试覆盖：术语本地化、定价、目录渲染、意图、检索阈值/去重、
  **商品链接确定性锚定（置顶 / `max_per_source` 豁免 / 退化路径）**、会话记忆、
  工单生命周期与 SLA、语言识别、RAGFlow 检索后端与入库客户端（`httpx.MockTransport`）、
  **熔断/健康监控、问答缓存、会话落库、反馈、限流**。
- 集成测试覆盖：`ChatService` 的正常路径 + 五条转人工分支（含依赖熔断降级）+
  多轮改写 + 会话落库，以及 API 层探针、控制台接口（工单/会话/反馈/指标/安全头/metrics）。
- 所有外部依赖（RAGFlow / Milvus / LLM API）在测试中都用替身，
  `make test` 在离线环境也能全绿。

> ⚠️ **测试隔离**：熔断器是**进程级单例**，`configure_breaker` 会改写它的阈值并常驻。
> 因此 `reset_resilience()` 的实现是**清空注册表**而非只重置状态——否则某个测试临时设的
> `failure_threshold=1` 会泄漏给后续测试（症状是「单独跑全绿、全量跑崩一片」）。
> 新写涉及熔断/限流的测试时，务必在 `finally` 里调 `reset_resilience()`。

---

## 12. 已知限制

- 当前按单机单副本形态运行：会话记忆（`SessionStore`）与熔断/限流都是**进程内**实现，
  多副本部署需换成 Redis（接口均已抽象，替换实现即可）。
- 回答只基于知识库；**资料不足会明确告知并转人工，不猜**。
- 退款/投诉/纠纷等高风险问题**强制转人工**，不自动处理、不自动承诺。
- 采集到的是货源真实批发价；零售价是基于 `pricing.py` 规则的**派生值**，
  文档里两者分开标注，不混为一谈。
- 商品详情通过对方 H5 接口采集（签名 + AES 解密），属于临时取数方式；
  正式使用应取得授权或签署数据合作协议，并遵守对方 robots 与使用条款。
