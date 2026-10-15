# GitHub 提交文件 · v0.7.0

> 用途：把本轮工作提交到 GitHub 时**直接复制粘贴**的 PR 描述 + commit 清单。
> 仓库当前**没有 git 仓库**（未 `git init`），文末给了完整的初始化与提交命令。

---

## 一、PR 标题

```
fix(security,infra): 修复登录限流绕过与 SQLite 并发写锁缺失；新增审计报告与改进路线图
```

（若你更偏好中文标题：`修复登录限流绕过 / SQLite 写锁缺失，并补齐审计报告与改进路线图`）

---

## 二、PR 描述

<!-- 复制下面这一整块到 GitHub 的 PR 描述框 -->

### 这个 PR 做了什么

第二轮全量审计（第一轮见 0.6.0），**逐文件通读 + 对可疑点实测复现**，
确认并修复 **1 个安全缺陷 + 5 个健壮性/正确性缺陷**，补 **15 条回归测试**；
同时产出与 8 类开源竞品的对标报告与 **17 条可落地改进清单**。

### 为什么要改（问题与影响）

| # | 严重度 | 问题 | 原来会发生什么 |
|---|---|---|---|
| 1 | 中危 | 登录限流无条件采信 `X-Forwarded-For` | 该头客户端可伪造，而登录限流按 IP 建桶 → **每次换个假 IP 就能无限试密码**，防撞库只剩账号锁定与验证码两道 |
| 2 | 中危 | 同一 SQLite 文件的多 store 各持一把写锁 | 四个 store 指向同一个 `console.db` 却互不互斥，并发下把 SQLite 单写者临界区暴露成偶发 `database is locked`（与类文档「共享写锁」的说明不符） |
| 3 | 中危 | LLM 健康判定只认"连续失败次数" | 一次偶发超时后若再无流量，`/readyz` **永久 503**，K8s 会把健康副本一直摘出负载均衡 |
| 4 | 中危 | 缓存键不含知识库版本 | 更新文档并重新入库后，**一小时内仍命中旧答案**，表现为"更新了知识库却还答旧的"，极易误判成检索没生效 |
| 5 | 低危 | `PRAGMA foreign_keys` 只在建表连接上执行 | 该开关是连接级的，业务连接全是 OFF → 后续新增外键约束会**静默失效** |
| 6 | 低危 | 限流器只在首次创建时应用参数 | 改 `RATELIMIT_*` 完全没反应：配置写着 10，实际仍按旧值放行 |

### 怎么改的

**1. 登录限流不再被伪造头绕过** — `src/customer_support/api/deps.py`、`config.py`、`.env.example`

```python
peer = request.client.host
trusted = get_config().trusted_proxies
if not trusted or not ("*" in trusted or peer in trusted):
    return peer            # 默认：完全不信任 X-Forwarded-For
return request.headers.get("x-forwarded-for", "").split(",")[0].strip() or peer
```

- 新增配置 `TRUSTED_PROXIES`（逗号分隔，**默认空 = 不信任任何代理**，直连部署的正确默认值）
- `production_warnings()` 对 `TRUSTED_PROXIES=*` 告警

**2. 写锁按库文件注册** — `src/customer_support/store/base.py`

```python
_write_lock_for(db_path)   # 模块级注册表，同文件必得同一把锁
```

**3. 健康判定引入"陈旧窗口"** — `src/customer_support/common/resilience.py`

`HEALTH_STALE_SECONDS = 300`；失败记录过期且期间无新调用时判为 `ok=True, stale=True`
（"信息陈旧"而不是"断言不可用"），`snapshot()` 新增 `stale` 字段；`quota_exhausted` 同步加陈旧判定。

**4. 知识库变更即作废答案缓存** — `src/customer_support/api/v1/ops.py`

抽出可测函数 `_invalidate_answer_cache(report)`：有上传/删除才清缓存，纯跳过不动缓存。

**5. 每条连接都开 `foreign_keys`** — 同上 `store/base.py`

**6. 限流参数变更即重建限流器** — `src/customer_support/common/ratelimit.py`

新增 `RateLimiter.matches()`；参数不一致重建，一致则复用（不白丢令牌状态）。

### 测试

新增 `tests/unit/test_infra_hardening.py`（**15 条**），逐条锁住上述行为：

- 同库四 store → **同一把锁对象**（确定性断言，不依赖时序）；不同库 → 不同锁
- 跨 store 并发真实交叉写入（含规模说明：本机 Windows 下每次开/关连接约 0.3s，故刻意压小）
- 每条连接 `PRAGMA foreign_keys == 1`
- XFF 三种模式：默认忽略 / 受信代理采信第一跳 / 通配符
- 限流器：参数变更重建、参数不变复用、容量上限生效
- 健康判定：新鲜失败 → `ok=False`；陈旧 → `ok=True, stale=True`；成功 → 恢复
- 缓存作废：有变更清空 / 无变更保留 / 缓存关闭不抛错

### 兼容性与迁移（⚠️ 请 Reviewer 注意）

- **新增配置项 `TRUSTED_PROXIES`，默认空**。直连部署（本项目默认形态）**无需任何操作**。
  **但如果你用 Nginx / 网关反向代理，必须配置它**，否则所有访客会被算成同一个 IP，
  `RATELIMIT_LOGIN_*` 会"误伤"整站。
- `/readyz` 的 `llm` 检查新增 `stale` 字段；`ok` 语义从"有失败就不健康"变为"有**近期**失败才不健康"。
  消费该接口的监控若做了严格字段校验需同步。
- 数据库 schema **无变更**，无需迁移。
- 版本由 0.6.0 → **0.7.0**（`config.py` + `frontend/package.json`）。

### 验证证据

```
$ pytest -q                       # 334 passed, 1 skipped
$ ruff check src tests scripts    # All checks passed!
$ mypy src/customer_support       # Success: no issues found in 66 source files
$ cd frontend && npm run typecheck  # vue-tsc --noEmit 通过
```

### 顺带产出的文档

- `docs/audit-2026-10.md` — 本轮审计报告：每条含「怎么复现 / 改了什么 / 怎么验」，
  并单列**查过但确认没问题**的区域（10 项）与**已知未修**的项（4 项），便于复核。
- `docs/improvement-roadmap.md` — 对标 Dify / Chatwoot / Chainlit / RAGAS / Langfuse 等
  8 类竞品后的 **17 条改进清单**（8 条 UI/UX + 9 条功能），每条点名要改的**文件与数据来源**，
  且对引用到的竞品行为标注了出处与"未核实"项。
- `CHANGELOG.md` — 新增 `[0.7.0]` 段。

### Reviewer 检查清单

- [ ] `TRUSTED_PROXIES` 的默认值（空）是否符合你们的部署形态
- [ ] `/readyz` 的 `stale` 语义是否符合监控预期（是否需要更严格的主动探针）
- [ ] 写锁按文件注册后，是否有"故意让不同库并行写"的场景被意外串行化
- [ ] `test_infra_hardening.py` 里的并发用例规模是否合适（可在 CI 上适当调大）

<!-- 描述到此结束 -->

---

## 三、拆分为多个 commit（推荐，便于 review）

按"一个问题一个 commit"拆开，每个 commit 都能独立通过测试：

```
fix(security): 不再无条件采信 X-Forwarded-For，新增 TRUSTED_PROXIES 白名单

登录限流按来源 IP 建桶，而 X-Forwarded-For 是客户端可伪造的请求头——
无条件采信等于把防撞库交给攻击者（每次换假 IP 即得一把新桶）。
改为默认完全不信任该头，仅当直连对端在 TRUSTED_PROXIES 白名单内才采信。

Refs: docs/audit-2026-10.md#21
```

```
fix(store): SQLite 写锁按库文件注册，修正同库多 store 未互斥的问题

BaseStore._write_lock 原是实例级锁，而 ConversationStore / TicketStore /
FeedbackStore / AnswerCacheStore 指向同一个 console.db，彼此并不互斥，
与类文档「共享同一个库文件与写锁」不符。改为按库文件路径注册，
并对每条连接开启 PRAGMA foreign_keys（该开关是连接级的）。

Refs: docs/audit-2026-10.md#22 #25
```

```
fix(resilience): LLM 健康判定引入陈旧窗口，避免 /readyz 永久 503

ok 原等价于「连续失败次数 == 0」，而该计数只能被下一次成功调用清零——
一次偶发超时后若无流量，就绪探针会一直 503，把健康副本永久摘出负载均衡。
引入 HEALTH_STALE_SECONDS（默认 300s）区分「真的坏了」与「信息陈旧」，
snapshot 新增 stale 字段，quota_exhausted 同步加陈旧判定。

Refs: docs/audit-2026-10.md#23
```

```
fix(ops): 知识库重新入库后作废问答缓存

缓存键是「问题文本 + 语言」的哈希，不含知识库版本——更新文档后一小时内
老问题仍命中改之前的答案，容易被误判成检索失效。抽出 _invalidate_answer_cache，
仅在上传/删除确实发生时清理，纯跳过不动热缓存。

Refs: docs/audit-2026-10.md#24
```

```
fix(ratelimit): 参数变更后重建限流器，修正改配置不生效的问题

get_limiter 原只在首次创建时应用 capacity/per_minute，之后传入值被静默忽略，
表现为「配置写 10、实际按旧值放行」。新增 RateLimiter.matches()，不一致即重建。

Refs: docs/audit-2026-10.md#26
```

```
test: 新增 15 条基础设施加固回归测试

逐条锁住写锁共享、连接级 foreign_keys、XFF 信任策略、限流器重建、
健康判定陈旧窗口、缓存按需作废。

Refs: tests/unit/test_infra_hardening.py
```

```
docs: 新增审计报告与 17 条改进路线图，版本升至 0.7.0

- docs/audit-2026-10.md：问题分级 + 复现方式 + 修法 + 验证；
  并列出「查过但确认没问题」10 项与「已知未修」4 项，便于复核。
- docs/improvement-roadmap.md：对标 Dify/Chatwoot/Chainlit/RAGAS/Langfuse 等，
  8 条 UI/UX + 9 条功能改进，每条落到具体文件与数据来源。
- CHANGELOG 增 [0.7.0]；config.py 与 frontend/package.json 版本对齐。
```

---

## 四、初始化仓库并提交

```bash
cd /path/to/customer-support-rag

git init -b main

# 确认 .gitignore 已排除敏感物（.env / output / logs / .venv / node_modules / dist）
git status --short

git add -A
git commit -m "feat: 跨境店铺多语言智能客服 RAG 系统（FastAPI + Vue 3 + RAGFlow）"

# 若想按上面的拆分留下历史，可以把工作区分批 add/commit，或用 git add -p

git remote add origin git@github.com:<你的账号>/customer-support-rag.git
git push -u origin main
```

> ⚠️ 提交前务必确认 `.env` 未被跟踪（`.gitignore` 已排除，但请在 `git status` 里**亲眼确认**），
> `output/auth/users.db`、`output/console/console.db` 同理——它们含真实账号与买家数据。
> 建议在 GitHub 上把仓库设为 **private**，或先清理 `output/` 与 `logs/` 再公开。
