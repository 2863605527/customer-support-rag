# 从零启动指南（Getting Started）

面向第一次接触本项目的人。按步骤操作，即可在本地跑起一个**能真实回答海外买家问题、
答不出来会转人工**的多语言客服机器人。

只想看命令 → 直接跳到 [§2](#2-首次启动一步步来windows)。

---

## 0. 这个项目到底是什么

一句话：**一个「跨境店铺智能客服」系统（后端 + Web 前端）**。买家（日本站，以日语为主）问商品 / 物流 /
退换货，它先从店铺知识库（60 个真实商品 + 政策文档）里检索，再用大模型组织成回答；
**查不到就不编**，直接转人工并自动建工单。

它长这样：

```
买家提问
  │
  ├─ ① 识别语言（日语/英语/西语…）
  ├─ ② 会话记忆 + 指代消解（"洗える？" → "这个床罩能洗吗？"）
  ├─ ③ 识别意图 ──┬─ 高风险（退款/投诉/纠纷） ──────────▶ 转人工 + 建工单
  │               └─ 意图不确定（置信度低） ────────────▶ 转人工 + 建工单
  │
  └─ ④ 正常路径：检索知识库
            ├─ 有足够相关资料 ──▶ 生成回答（带可溯源引用）
            └─ 没资料 / 模型答不上来 ──▶ 转人工 + 建工单
```

两种用法：

- **网页前端**：`frontend/` 下的 Vue 3 工作台，聊天界面最直观（见 §2 方式 C）；
- **命令行 `ask`**：问一条，看结果（验证 / 调试用）；
- **HTTP API `serve`**：起一个服务，给前端页面或客服后台接入用。

---

## 1. 启动前，你需要三样东西

| 需要什么 | 说明 | 怎么确认 |
|---|---|---|
| **Python 3.10+** | 项目是 Python 写的 | 终端里 `python --version` |
| **一个 LLM API Key** | OpenAI 兼容协议；本项目默认指向 DeepSeek | 已填在 `.env` 的 `LLM_API_KEY` |
| **一个运行中的 RAGFlow 服务** | ⚠️ **默认检索后端**，是本项目唯一的外部依赖 | 浏览器打开 `http://localhost` 能看到 RAGFlow 页面 |

关于第 3 条，**这是最容易踩的点**：

- RAGFlow 是一个**独立的开源知识库系统**（负责文档解析 / 分块 / 向量化 / 检索），
  **不在本项目代码里**，要单独部署（通常用 Docker）。
- 你现在的机器上已经跑着一个（否则检索会直接失败）；换机器或重装时，需要先把 RAGFlow
  跑起来，并在它后台建好知识库、拿到 API Key。
- **不想装 RAGFlow？** 项目保留了 Milvus 备选路线（见 [§5](#5-换检索后端可选进阶)），
  但需要自己配 embedding 模型、还要重新标定检索阈值，**对新手反而更麻烦，建议就用 RAGFlow**。

---

## 2. 首次启动：一步步来（Windows）

> 所有命令都在**项目根目录**（`customer-support-rag\`）下执行。

### 第 1 步：建虚拟环境并安装

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

- 第 1 行：建一个隔离的 Python 环境 `.venv`（不污染系统 Python）；
- 第 2 行：激活它 —— 成功后命令提示符前面会出现 `(.venv)`；
- 第 3 行：把项目装成「可编辑模式」，并安装开发依赖（测试 / lint 工具）。
  装完后即可使用 `customer-support` 命令。

> 如果之后提示 `customer-support` 找不到：要么第 3 行没装成功，要么虚拟环境没激活。
>
> Linux / macOS 把第 2 行换成 `source .venv/bin/activate`。

### 第 2 步：填配置

```bash
copy .env.example .env
```

然后编辑 `.env`，**至少**填这两项：

| 变量 | 填什么 |
|---|---|
| `LLM_API_KEY` | 大模型 API Key |
| `RAGFLOW_API_KEY` | RAGFlow 后台「API」页签里的 Key |

> `.env` 已被 `.gitignore` 排除，**不会被提交**，可以放心填。
> 你当前这两项都已填好 —— 这一步可以跳过。

### 第 3 步：把知识库文档灌进 RAGFlow

```bash
customer-support ingest
```

- 它把 `data/knowledge_base/` 里的 67 份文档上传到 RAGFlow，并等 deepdoc 解析完成；
- **幂等**：同名文档默认跳过，重复执行不会产生重复文档；
- 你现在已经入库完成（69 份文档 / 149 个分块全部 `DONE`）—— **这一步可以跳过**。
- 以后改了知识库文档，再跑一次这条命令即可（改同名覆盖要加 `--replace`）。

### 第 4 步：启动

**方式 A：先来一条命令行问答（最快验证）**

```bash
customer-support ask "返品はできますか？"
```

**方式 B：启动 HTTP 服务**

```bash
customer-support serve --port 8002
```

然后打开 <http://127.0.0.1:8002/docs> —— 这是自动生成的接口文档（Swagger），
可以直接在网页上点「Try it out」发请求。

**方式 C：启动网页前端（推荐，最直观）**

推荐「后端直营模式」——**只起一个服务**，前端由后端直接托管，同源无跨域问题：

```bash
cd frontend
npm install        # 首次执行一次即可
npm run build      # 构建产物到 frontend/dist
cd ..
customer-support serve --port 8002
```

然后打开 **<http://127.0.0.1:8002/>** —— 就是客服工作台（聊天、引用溯源、
转人工/工单徽章、服务健康状态）；`/docs` 接口文档也在同一端口下。

**首次使用要先登录**（鉴权是必须的，不是摆设）：

| 账号 | 从哪来 |
|---|---|
| 管理员 | 服务端**首次启动时自动创建**，邮箱默认 `admin@kurashi-bedding.jp`；密码取 `.env` 的 `AUTH_SEED_ADMIN_PASSWORD`，**留空则随机生成并打印在服务端日志里**（搜 `随机密码`）。也可以直接在 `.env` 里设一个自己的密码，重启后端即可。 |
| 客服账号 | 打开 `/register` 自助注册（密码需 ≥10 位且含字母和数字），或让管理员用 `customer-support users create ...` 开号。 |

忘了密码就用命令行重置：

```bash
customer-support users list                                        # 看有哪些账号
customer-support users set-password --email admin@kurashi-bedding.jp --password 'Str0ng-Passw0rd!'
```

安全策略（连续失败锁号、图形验证码、令牌轮换、登录审计）详见 `README.md` §8。
生产部署**务必**改掉 `.env` 里的 `AUTH_SECRET_KEY`。

开发模式（改前端代码热更新）：

```bash
cd frontend
npm run dev        # Vite 会自动挑空闲端口，注意用它打印出来的地址
```

> ⚠️ Vite 默认从 5173 起找端口，**被别的项目占用会顺延到 5174、5175…**，
> 一定要开终端打印的那个地址。开发模式经 Vite 代理访问后端，默认目标
> `127.0.0.1:8002`，不同就先 `set BACKEND_ORIGIN=http://127.0.0.1:8002` 再 `npm run dev`。

### 第 5 步：确认真的跑起来了

服务起来后，另开一个终端：

```bash
curl http://127.0.0.1:8002/healthz   # 200 = 进程活着
curl http://127.0.0.1:8002/readyz    # 200 = 依赖都可用；503 = 有东西没配好（会告诉你缺哪个）
```

> `/readyz` 里 `checks.llm_configured` 为 `false`，就是 `LLM_API_KEY` 没填对。

### 方式 D：Docker 部署（推荐，环境一致、一条命令起全栈）

多阶段构建：Node 编译前端 → Python 运行时单域名托管 API + 页面。
镜像不内置密钥，配置全部经 `.env` 注入：

```bash
docker compose up -d --build     # 构建并后台启动
docker compose ps                # STATUS 应为 healthy
docker compose logs -f app       # 跟踪日志（首次启动会打印管理员随机密码，如未在 .env 指定）
```

打开 **<http://127.0.0.1:8002/>** 即登录页。停掉：`docker compose down`。

数据持久化：用户库/审计/工单都在宿主机 `./output`（容器删了数据还在），
日志在 `./logs`，知识库文档在 `./data/knowledge_base`。

备选 Milvus 检索后端在独立 profile 里，不随默认启动；
需要时 `docker compose --profile milvus up -d`。

> ⚠️ 容器内连 RAGFlow 用 `RAGFLOW_BASE_URL`。若 RAGFlow 跑在宿主机上，
> Windows/Mac Docker 里要写 `http://host.docker.internal` 而不是 `localhost`。

---

## 3. 怎么算「验证通过」：四个必试问题

用 `customer-support ask "..."` 依次试，结果应该符合下表：

| 问题 | 预期结果 | 为什么 |
|---|---|---|
| `返品はできますか？` | `来源: knowledge`，日语回答 + 引用 `after_sales_policy.md` | 知识库有标准答案 |
| `送料はいくらですか？` | `来源: knowledge`，答「3,980 円以上免运费，未满 500 円」 | 能引用政策文档里的数字 |
| `Do you ship to Osaka?` | `转人工: True（no_knowledge）` | 知识库没有这条，**宁转人工也不瞎编** |
| `商品が壊れていました。最悪です、返金しろ！` | `转人工: True（high_risk_intent）` + 工单号 | 高风险问题不自动承诺 |

每条转人工都会自动建工单，落到 `output/tickets/tickets-YYYY-MM-DD.jsonl`。
用下面这条看当日汇总（按原因 / 意图）：

```bash
customer-support tickets
```

---

## 4. 日常会用到哪些命令

| 命令 | 作用 |
|---|---|
| `customer-support ask "问题"` | 命令行问一条（最常用） |
| `customer-support ask "问题" --json-output` | 输出完整 JSON（看 `references` / `trace_id`） |
| `customer-support ask "洗える？" --session s1` | 带会话上下文（多轮指代消解） |
| `customer-support serve --port 8002` | 启动 HTTP 服务 |
| `customer-support ingest` | 知识库重新入库（改完文档后） |
| `customer-support tickets` | 查看当日转人工工单汇总 |
| `make check` | 一键跑 lint + 类型检查 + 测试（**改代码后必跑**） |
| `make calibrate` | 重新标定检索阈值（见 [§7](#7-一个必须知道的坑检索阈值是量出来的)） |

---

## 5. 换检索后端（可选，进阶）

默认用 RAGFlow。如果想完全不依赖 RAGFlow，可以切到自建 Milvus：

```bash
docker compose up -d milvus      # 起一个本地 Milvus
# 把 .env 改成：RETRIEVAL_BACKEND=milvus
customer-support build-kb        # 把知识库灌进 Milvus（需要额外配 LLM_EMBEDDING_*）
```

代价：要额外配 embedding 模型，检索质量也得重新标定。**新手先别碰这条。**

---

## 6. 常见问题（对照排查）

| 现象 | 原因 / 解法 |
|---|---|
| `ModuleNotFoundError: customer_support` | 没装依赖，或虚拟环境没激活 → 回到 [§2 第 1 步](#第-1-步建虚拟环境并安装) |
| `ConfigError: 未配置 RAGFLOW_API_KEY` | `.env` 里 `RAGFLOW_API_KEY` 为空 |
| `/readyz` 返回 503 且 `llm_configured: false` | `.env` 里 `LLM_API_KEY` 没填 |
| 检索报连接错误 / 超时 | RAGFlow 没在跑 → 浏览器打开 `http://localhost` 看看 |
| **所有问题都「转人工」** | 检索阈值或向量权重不匹配 → 跑 `make calibrate` 重新标定（见 §7） |
| 报 `Model Not Exist` 之类的模型错误 | `LLM_MODEL` 名字不对 → 用下面一行命令列出当前 key 实际可用的模型名 |
| 运行 `pytest` 报 `--cov` 相关错误 | 用 `pytest --no-cov`，或在干净目录跑（项目默认开启覆盖率统计） |

列出当前 key 实际可用的模型名：

```bash
python -c "from openai import OpenAI; from dotenv import load_dotenv; import os; load_dotenv(); c=OpenAI(base_url=os.getenv('LLM_BASE_URL'), api_key=os.getenv('LLM_API_KEY')); print([m.id for m in c.models.list()])"
```

> 本机当前实测可用：`deepseek-flash`、`deepseek-v4-pro`。
> 网上常见的 `deepseek-chat` / `deepseek-reasoner` 在具体服务端不一定存在，
> **以这条命令的输出为准**填 `LLM_MODEL`。

---

## 7. 一个必须知道的坑：检索阈值是「量出来」的

不同平台 / embedding 模型给出的 `similarity` 量纲差别巨大。本项目**不是**拍脑袋定 `0.8`，
而是用一份 26 题的标注集把阈值**标定**出来：

- 当前值：`RAGFLOW_VECTOR_SIMILARITY_WEIGHT=0.7`、`RETRIEVAL_SCORE_THRESHOLD=0.53`
  → 召回 94%、误检 10%（Youden J = 0.84）；
- 若沿用直觉值（如 0.8），正常问题会被全部判成「资料不足」，线上表现为**「永远在转人工」**。

**换 embedding 模型、或知识库大改后，务必重新标定**：

```bash
make calibrate        # 打印阈值扫描表 + 建议值 + 每个问题的得分明细
```

原理详见 `README.md` §6.1。

---

## 8. 想改代码？先看这几处

| 我想改… | 看哪里 |
|---|---|
| 整体架构、为什么这么分层 | `docs/architecture.md` |
| 开发流程、测试策略、怎么加一门语言 | `docs/development.md` |
| **业务规则**（什么时候转人工） | `src/customer_support/services/chat_service.py` |
| 检索 / 防幻觉 | `src/customer_support/rag/`（`retriever.py` / `generator.py`） |
| 意图识别 | `src/customer_support/agents/intent_agent.py` |
| 知识库文档内容 | `data/knowledge_base/`（`products/` `policies/` `store/`） |
| 定价公式 | `src/customer_support/knowledge/pricing.py` |

改完代码，记得 `make check` 全绿再提交。
