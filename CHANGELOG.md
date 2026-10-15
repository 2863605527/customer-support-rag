# Changelog

本文件记录对外可见的行为变化。版本号跟随 `config.py` 的 `version` 与 `frontend/package.json`。

## [0.8.0] — 2026-10-15

主题：**知识库扩容与售前售后知识补全**。商品从 60 个 SKU 扩到 120 个，补齐此前
缺失的 5 类高频问答文档，并修掉扩品过程暴露的两处数据质量问题。

### 知识库

- **商品 SKU 60 → 120**（`--target 120`）。新增的 60 条来自同一份候选池
  （1665 条 → 去重 1170 条），按原有品类配额规则选取，且全部带上了货源详情
  （材质属性 / 规格展開 / 重量）。
- **新增 5 份共通知识文档**，补的是此前「知识库最薄、但买家问得最多」的部分：
  - `policies/care_and_washing.md` 洗涤保养：按材质的洗法、洗涤标识读法、
    缩水 / 掉色 / 起球的原因与处理、晾晒与收纳、防螨。
  - `policies/bedding_materials.md` 材质与安全：材质特性对比、GB 18401 安全等级
    （A/B/C 類）、甲醛残留、敏感肌与过敏、抗菌防螨加工的**有效期限**、冬季静电。
  - `policies/customs_and_parcel_issues.md` 通关与配送异常：关税计算式与承担方、
    海关扣留通知的处理、追踪停滞、退运与再发运费、破损取证的时限。
  - `policies/order_changes.md` 下单后变更与取消：可操作的时间窗、
    发运前后的地址变更、支付方式为何不能改、订单号与新单号。
  - `store/bedding_selection.md` 季节 / 用途选购：冷暖房并用、结露与发霉、
    垫被与床垫罩的区别、床垫厚度与床笠尺寸、宠物与儿童场景、更换周期。

### 修复（扩品暴露的数据质量）

- **素材指南被占位值污染**：`material_guide.md` 的素材排行第一位是
  「其他/other：57 商品」——那是货源属性里的占位值，不是材质。它排在首位会让
  「你们主要用什么材质」这类泛问拿到误导性依据。现在占位值
  （`其他` / `other` / `无` / `请选择` …）一律不写入文档与统计。
- **材质值漏译与重复计数**：`涤纶` / `棉絮` / `天然乳胶` 等实际出现的值没有映射，
  同义写法（`涤纶` 与 `聚酯纤维`）被拆成两行分别计数。现在按别名归一，
  并支持组合值（`聚酯纤维+10%大豆纤维`）与带配比的写法（`10%大豆纤维`）。
  季节值（`四季通用` → オールシーズン）与安全区分（`B类` → `B類`）同样日语化。
- **`material_value_ja()` 现在可能返回空串**（占位值被过滤），调用方需判空。
  两个调用点（文档渲染、素材统计）均已适配。

### 新增能力

- **`build_catalog(records, existing_skus=...)` 稳定编号**：商品番号改为沿用上一版
  `catalog.json` 已分配的番号，新商品接在最大番号之后。不这么做的话，`--target`
  一变（60 → 120）品类分界就平移，**所有旧番号都会指向别的商品**，
  既有文档、链接锚定与 RAGFlow 索引会整体失效。本次扩品后原有 60 份文档
  SHA256 完全一致，入库只需增量解析新增部分。番号不再回收：某商品本次未入选时
  它的番号留空不复用，否则同一个 `KB-0042` 在不同时间会指向不同商品。

### 修复（运维脚本）

- **`reconcile_ragflow.py` 在解析积压时会整脚本崩溃**：等待循环里调用的
  「触发解析」与「拉取文档列表」在 RAGFlow 忙碌时都会超时，而这两个调用
  原先没有兜底——一次 `ExternalAPIError` 就把整次收敛中断，副本和文件名都停在
  半途。现在它们是等待环节，失败就地吞掉并在下一轮重试。
- **收敛基准与入库基准不一致**：收敛脚本用 `rglob` 自己判定「本地该有哪些文档」，
  会把给人看的 `README.md` 也算进去，而入库用的加载器会跳过它。两处口径不同，
  一旦远端存在同名文件就会被误判为「该保留」。现在收敛改用同一个加载器。

### 入库实操记录

- 扩品后入库需要两步：`customer-support ingest --replace` 上传变更内容，
  再 `scripts/reconcile_ragflow.py --yes` 收敛文件名。RAGFlow 对同名上传会加
  `(1)` 后缀，**收敛前商品链接的确定性锚定会失效**（它按 `KB-XXXX.md` 精确查找）。
- 本次结果：133 份文档 / 411 切片 / 无 `(1)` 后缀残留 / 无未完成解析。

### 测试

- `tests/unit/test_catalog.py` 新增 10 条：稳定编号的复用与追加、扩品不重排、
  非标准番号容错、组合材质值拆分、别名与配比识别、占位值过滤、
  渲染跳过占位属性、季节值翻译。

## [0.7.0] — 2026-10-15

主题：**并发一致性、限流可信度与运维可见性**。全量审计（含并发与长期运行视角）
挖出 1 个安全缺陷 + 5 个健壮性/正确性缺陷，全部修复并补回归测试。
完整报告见 [`docs/audit-2026-10.md`](docs/audit-2026-10.md)。

### 安全修复

- **【中危】登录限流可被伪造请求头绕过**：`_client_ip()` 原先**无条件采信**
  `X-Forwarded-For`。这个头是客户端可写的，而登录限流恰好是**按来源 IP**建桶的——
  攻击者每次换一个假 IP 就能拿到一把新桶，`RATELIMIT_LOGIN_*` 形同不存在
  （防撞库只剩账号锁定与图形验证码两道）。
  现在改为：**默认完全不信任该头**（直连部署的正确默认值）；只有当直连对端在
  新增的 `TRUSTED_PROXIES` 白名单内（即前面确实是我们自己的网关）才采信 XFF 第一跳。
  ⚠️ **反代部署需新增配置 `TRUSTED_PROXIES=<代理 IP>`**，否则所有访客会被算作同一 IP。

### 修复（正确性 / 并发）

- **同一 SQLite 文件的多个 store 没有共享写锁**：`BaseStore._write_lock` 是**实例级**
  `threading.Lock`，而 `ConversationStore` / `TicketStore` / `FeedbackStore` /
  `AnswerCacheStore` 是**四个实例、同一个库文件**——它们之间根本不互斥，
  与类文档里「子类共享同一个库文件与写锁」的说法不符，并发下会把 SQLite 的
  单写者临界区暴露成偶发 `database is locked`。现在写锁按**库文件路径**注册
  （`_write_lock_for`），同文件必得同一把锁。
- **`PRAGMA foreign_keys` 实际未生效**：该开关是**连接级**的，原实现只在建表那条连接上
  打开，业务连接全都带着默认的 OFF——一旦后续给表加上外键约束，约束会静默失效。
  现在每条连接都开。
- **LLM 健康判定会永久卡在"不可用"**：`ok` 原先等价于「连续失败次数 == 0」，而失败计数
  只能被**下一次成功调用**清零。于是一次偶发超时之后若再无流量（夜间、低峰），
  `/readyz` 会一直返回 503，把其实健康的副本永久摘出负载均衡，直到有人手动发一次问答。
  现在引入**陈旧窗口**（`HEALTH_STALE_SECONDS = 300`）：失败记录过期且期间无新调用时，
  判为"信息陈旧"（`ok=True, stale=True`），不再断言依赖不可用；
  `quota_exhausted` 同样不再因三天前的一次欠费而长期亮红灯。
- **知识库更新后旧答案仍在缓存里**：问答缓存的键是「问题文本 + 语言」的哈希，
  **不含知识库版本**。运维改完文档、重新入库后，一小时内（`CONSOLE_CACHE_TTL_SECONDS`）
  老问题仍会命中**改之前**的答案，表现为「明明更新了知识库，客服还是答旧的」，
  极容易被误判成"检索没生效"。现在重新入库确实发生上传/删除时**自动作废问答缓存**。

### 修复（配置生效）

- **限流参数改了不生效**：`get_limiter()` 原先只在中**首次创建**时应用 `capacity` /
  `per_minute`，之后传入的值被静默忽略——改 `RATELIMIT_CHAT_PER_MINUTE` 完全没反应，
  且配置里写着 10 实际还按老值放行，极难排查。现在参数不一致即重建限流器。

### 修复（归因与可观测）

- **依赖故障被写成"买家高风险诉求"**：LLM 超时或上游返回 503 时，`IntentAgent.classify()`
  拿不到结果，原先一律兜底成 `intent=human`；`ChatService` 再按 `is_high_risk`
  （等价于 `intent == HUMAN`）归因，工单原因就写成了 `high_risk_intent`。
  表现为：一条普通的商品咨询会因为上游抖动被记成高风险投诉，按工单归因复盘时
  会把故障误读成投诉激增，而专为依赖故障定义的 `service_unavailable` 反而从不出现。
  现在区分三类结果——LLM 调用失败不再兜底而是上抛，由 `ChatService` 判
  `service_unavailable`；模型输出不可解析才兜底，且 `IntentResult.fallback=True`
  使其按 `low_confidence` 归因；只有模型明确判定 `human` 才归 `high_risk_intent`。
- **LLM 错误分类缺少"上游过载"一类**：新增 `unavailable`（503 / 502 / bad gateway /
  service is too busy 等）。原先这类错误落进 `unknown`，`/readyz` 的
  `last_error_category` 显示 `unknown`，看不出问题出在哪里。

### 测试

- 新增 `tests/unit/test_infra_hardening.py`：15 条用例，逐条锁住上面六个行为
  （同库共享锁、跨 store 并发写、连接级 foreign_keys、XFF 默认不可信 / 受信代理采信 /
  通配符、限流器重建与容量、健康判定新鲜失败 / 陈旧恢复 / 成功恢复、缓存按需作废）。
- 新增 `tests/unit/test_transfer_attribution.py`：锁住四条转人工归因路径
  （依赖不可用 / 代码异常 / 输出不可解析 / 模型判定高风险），并确认流式链路与同步链路
  归因一致——两条路径各自 catch，改的时候容易漏掉一条。

### 文档

- 新增 [`docs/audit-2026-10.md`](docs/audit-2026-10.md)：本轮审计报告（问题 / 严重度 /
  可利用性 / 修法 / 验证）。
- 新增 [`docs/improvement-roadmap.md`](docs/improvement-roadmap.md)：与 8 类开源竞品对标后
  的 **17 条改进清单**（8 条 UI/UX + 9 条功能），每条都落到具体文件与数据来源，并给出
  P0/P1/P2 排序。
- 新增 [`docs/pr-0.7.0.md`](docs/pr-0.7.0.md)：可直接粘贴到 GitHub 的 PR 描述与
  conventional commit 清单。

## [0.6.0] — 2026-10-14

主题：**安全与健壮性加固**。一次全量威胁排查挖出 2 个高危 + 3 个中危缺陷，全部修复并补了回归测试。

### 安全修复

- **【高危】SPA 回退的路径穿越 → 任意文件读取**：`/{full_path:path}` 会匹配到服务端**已解码**的
  `../../.env`，而 `Path.__truediv__` 不做归一化——`(dist / "../../.env").is_file()` 为真，
  于是 `.env`（LLM / RAGFlow Key）、`output/auth/users.db` 可被直接下载。
  实测 `curl --path-as-is 'http://host/%2e%2e/%2e%2e/.env'` 即可复现。
  现在改为「`resolve()` 后校验是否仍在 `dist` 内」（新增 `_resolve_static`），
  软链接指向库外同样被拦下。
- **【高危】图形验证码答案泄露且可无限重放**：旧实现是
  `captcha_id = base64(过期时间.答案.随机数) + HMAC 签名`，而 **base64 不是加密**——
  任何客户端 decode 一下就能直接读出答案，「防脚本撞库」形同虚设；
  同一 id 在有效期内还可无限重复提交。现在答案只存服务端进程内表：
  **不回传、通过即作废（一次性）、限次（5 次）过期（TTL）**，并带容量上限；
  答案本身也改用 CSPRNG 生成。⚠️ 取舍：放弃了"无状态"，多副本需换成 Redis——
  原来的"无状态"是靠泄密换来的（详见 `auth/captcha.py` docstring）。
- **【中危】前端 SSE 看门狗完全失效**：`chatStream` 里 90s 超时只调用了 `controller.abort()`，
  却没把 `signal` 交给 `fetch`，于是 `reader.read()` 永远挂着、连接断了界面一直转圈。
  现在中断器提前创建并把 `signal` 传入。
- **【中危】运维接口泄漏 HTTP 连接**：`/ops/knowledge/documents` 每次请求都新建一个
  `RagflowIngestor`（内含 `httpx.Client`）且从不关闭，连接池与文件描述符按请求数线性泄漏。
  现在用 `finally` 显式 `close()`（`/ops/knowledge/reingest` 的校验探针同样处理）。
- **【中危】`/metrics` 的令牌比较非常量时间**：改用 `secrets.compare_digest`；
  同时修正配置注释——`METRICS_TOKEN` 留空时是**完全不鉴权**（原注释声称"仅本机可读"，
  与实现不符，属误导性文档）。

### 健壮性修复

- **建单失败不再让买家收到 500**：`_transfer` 原先直接调 `ticket_service.create`，
  异常会穿透 `handle()` 的兜底（兜底本身也走 `_transfer`，再抛一次就真的 500）——
  违反「买家永远不会收到 500」这条承诺。现在建单自吞异常并记 `error` 日志。
- **入参长度上限**：`ChatMessage` 的 `session_id` / `buyer_id` / `language` 原先无长度限制，
  而它们会作为 SQLite 主键与内存字典键落库落内存、还会被拼进 prompt 与日志。
  现已分别限制为 128 / 64 / 8 字符。
- **Prometheus 标签转义**：`store/analytics.py` 的标签值未转义，值里出现 `"` 或换行会让
  **整个文本格式非法**导致抓取直接失败；现已按规范转义。

### 新增

- `tests/unit/test_captcha.py`：验证码不回传答案 / 一次性 / 限次 / 过期 / 容量行为。
- `tests/unit/test_hardening.py`：静态文件越界（含 Windows 分隔符与软链接）、
  入参长度、建单失败降级的回归测试。
- `docs/benchmark.md`：与 RAGFlow / FastGPT / Dify / Langfuse / RAGAS 等同类项目的
  对标、能力缺口矩阵与 P0/P1/P2 改进路线图。

### 测试

- 全量 **319 项通过**、1 项跳过（Windows 不允许建软链接），`ruff` / `mypy`(66 files) 全绿。
- 原先 `test_auth_tokens.py::TestCaptcha` 里有两条用例**依赖旧漏洞**（其中一条直接从
  `captcha_id` base64 解出答案、另一条验证"签名不符则拒绝"），已重写为守新不变量。

## [0.5.0] — 2026-10-14

主题：**把「贴商品链接提问」从碰运气改成确定性事实**。

### 新增

- **商品链接确定性锚定**：买家贴货源链接 / 商品番号时，按 `KB-XXXX.md` 直接把该商品文档的
  切片取回并置顶（新增 `RetrievalBackend.fetch_document`，RAGFlow 侧按文档名取回），
  不再指望相似度一定排得上来。锚定切片 `score=1.0` + `pinned=true`，与真实相似度在
  API 引用与工单里可区分；相似度只用于补足剩余名额、且不再从该来源取材；锚定失败
  （远端无此文档 / 后端不支持）自动退化为纯相似度检索——纯增强，不会让原本能答的问题变差。

### 修复

- **贴链接提问被误判「资料不足」**：目标商品文档在锚点查询下相似度仅 0.549、排不到第一
  （跨语言 + 同系列近义标题互相抢位），模型拿不到该商品资料就拒答转人工。现由确定性锚定
  保证该商品文档一定进入参考资料。
- **RAGFlow 文档名未规范化**：`reconcile_ragflow.py` 原先只删副本、不修名字，幸存者仍带
  `(1)` 后缀 → 「按 `KB-XXXX.md` 锚定」会失效。现删除副本后把幸存者**改名回规范名**
  （新增 `RagflowIngestor.list_chunks` / `rename_document`）。

### 测试

- 全量 **295 项通过**（新增锚定置顶 / `max_per_source` 豁免 / 退化路径、
  `fetch_document` 名字匹配与索引 TTL 缓存、`list_chunks` / `rename_document` 等 19 项）；
  `ruff` / `mypy`(66 files) 全绿。

## [0.4.0] — 2026-10-14

主题：**从「能问答」补齐到「可运营」**。新增运营控制台一整套能力，并把踩过的可靠性坑固化成代码。

### 新增

- **工单全生命周期**：工单从 JSONL 记录升级为 SQLite 实体，支持状态流转
  （`open → in_progress → resolved → closed`）、优先级（按转人工原因推导默认值）、指派、
  人工回复 / 内部备注、SLA（首次响应 / 解决时长）。新增 `/ops/tickets*` 接口与前端「工单中心」页。
- **服务端会话与消息持久化**：`ConversationStore` 成对原子落库（买家问题 + 客服回答），
  维护会话摘要（消息数 / 转人工次数）。新增 `/ops/conversations*` 接口；工作台侧栏可回看历史会话。
- **回答质量反馈闭环**：`POST /feedback`（👍/👎 + 原因，差评必须带原因）、
  `/ops/feedback/stats`、`/ops/feedback/dataset`（导出 JSONL 回归评测集）；前端每条回答可评价。
- **运营仪表盘**：`/ops/analytics` 聚合自助解决率、转人工率、分阶段延迟、意图/来源/转人工原因分布、
  按天趋势、工单积压、差评原因、LLM 健康与熔断状态；前端「运营仪表盘」页（内联 SVG 趋势图）。
- **LLM 熔断降级与配额健康**：`CircuitBreaker` + `LLMHealthMonitor`，错误分类
  （quota / auth / rate_limit / timeout / connection）；欠费时 `/readyz` 返回 503 且
  `checks.llm.quota_exhausted=true`，前端顶栏显示「LLM 欠费」；转人工原因新增 `service_unavailable`。
- **接口限流**：令牌桶限流（聊天按登录用户、登录按来源 IP），超限 `429` + `Retry-After`。
- **精确问答缓存**：同问题（同语言、无上下文）复用历史回答，只缓存「有依据且未转人工」的结果；
  命中时带 `cached` 标记并分块重放，不消耗模型额度。
- **知识库运维 API**：远端文档清单、在线检索测试（调 top_k / 阈值）、触发重新入库（202 + 后台线程）。
- **安全响应头与生产配置校验**：CSP / X-Frame-Options 等安全头；`ENVIRONMENT=production` 时
  弱密钥 / 开放 CORS / 空管理员密码直接拒绝启动。
- **结构化可观测性**：回答结果新增 `stage_ms` / `prompt_tokens` / `completion_tokens` /
  `estimated_cost`；新增 `/metrics`（Prometheus 文本，可用 `METRICS_TOKEN` 保护）。
- **前端导航体系**：新增 `AppLayout` 外壳（顶栏导航 + 用户菜单），页面拆分为
  工作台 / 工单中心 / 运营仪表盘；`useHealth` 统一健康轮询。
- **商品详情采集**：`scripts/harvest_goods_detail.mjs`（H5 接口签名 + AES 解密），
  60/60 SKU 全部抓取成功，含真实材质属性与尺码规格，富化进商品知识库文档。

### 修复

- **熔断器阈值泄漏**：`reset_resilience()` 原先只重置状态、不丢弃实例，导致
  `configure_breaker` 临时改写的阈值常驻并污染后续调用（症状：单个测试全绿、全量测试崩一片）。
  现改为清空注册表，恢复「进程刚启动」的状态。
- **`get_breaker` 的隐式参数**：同名熔断器已存在时静默忽略 `kwargs`，易误用；补充文档说明
  「创建参数只在首次生效，运行期改阈值请用 `configure_breaker`」。
- **RAGFlow 文档列表分页**：不再信任接口返回的 `total`（不可靠会导致漏列），改为按实际返回条数翻页；
  直接修复了「误判文档缺失 → 重复上传 → 产生 `(1)` 后缀副本」的问题。
- **`ingest --replace` 幂等性**：按文件大小比对，内容未变不再重复上传；旧版本清理带重试窗口。

### 变更

- 运营数据统一落到 `output/console/console.db`（会话 / 消息 / 工单 / 反馈 / 缓存共库、分表）；
  `TicketService` 改为 SQLite 门面，保留原有接口。
- 新增配置段：`CONSOLE_*`、`RATELIMIT_*`、`ENVIRONMENT`、`METRICS_TOKEN`、
  `LLM_CIRCUIT_*`、`LLM_COST_*`。
- `.env.example` 补充上述配置；修正 LLM 默认值示例（本机实测 base_url 不带 `/v1`、
  模型名以 key 实际开通为准）与 Docker 连 RAGFlow 的端口说明（用 9380）。

### 测试

- 全量 **276 项通过**（新增熔断/健康监控、问答缓存、会话落库、反馈、限流、控制台接口等用例）；
  `ruff` / `mypy`(66 files) / `vue-tsc` 全绿。

## [0.3.0] — 2026-10-13

- SSE 流式输出（思考阶段可见 + 逐字渲染），前端重做为 DeepSeek 式步骤条。
- 商品链接识别：`CatalogIndex` 把货源链接 / 商品番号映射成商品并锚定检索词。
- 聊天记录本地持久化（localStorage）修复刷新丢失。
- RAGFlow 入库幂等与 `reconcile_ragflow.py` 收敛脚本。

## [0.2.x] 及更早

- 鉴权体系（bcrypt + JWT + 无状态验证码 + 锁号 + 审计）。
- RAGFlow / Milvus 双检索后端切换；检索阈值标定。
- Docker 多阶段构建 + 单域名托管前端。
