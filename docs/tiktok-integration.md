# 接入 TikTok Shop 客服：让本项目自动回复买家

> 结论：TikTok 没有"打开一个开关就让 AI 接私信"的能力。官方路径是
> **TikTok Shop Customer Service API**——本质是「**webhook 收进来 + 发送 API 发出去**」，
> 我们这套 RAG 服务正好卡在中间做大脑。
>
> 本文所有接口路径/字段/限制都标注了核实程度：✅ 官方文档页核实 / ⚠️ 二手来源（两篇独立文章一致）/ ❓ 待官方确认。
> 核实时间 2026-10。

---

## 1. 前提条件（不满足就先别写代码）

| # | 事项 | 说明 |
|---|---|---|
| 1 | TikTok Shop 卖家账号 | 日本站（`ja-JP` 是官方支持的 locale 之一 ✅） |
| 2 | 注册 TikTok Shop Partner Center | https://partner.tiktokshop.com ，创建 **Custom App** |
| 3 | 申请 scope | `seller.customer_service`（会话/消息 API）**默认未开通、需人工审核** ⚠️；配置 webhook 用 `seller.authorization.info` ⚠️ |
| 4 | OAuth 2.0 授权 | 引导店铺主账号授权 → 拿 `access_token` / `refresh_token` / `shop_cipher` ⚠️ |
| 5 | 公网 HTTPS 回调地址 | TLS 1.2+，且要能返回 200（本机开发用 ngrok/cloudflared 临时隧道） |

**关键认知**：Shop 客服会话 ≠ 普通 TikTok 私信。两者身份体系、数据、规则完全不同 ⚠️，
你在网上搜到的"TikTok 私信 API"方案**不能**用来接 Shop 客服。

---

## 2. 数据流

```
TikTok Shop 买家消息
      │  ① NEW_MESSAGE webhook（event type 14，HMAC-SHA256 签名）
      ▼
  /webhooks/tiktok/shop        ← 必须免登录！TikTok 没有我们的 JWT
      │  ② 对 **raw body** 验签 → 写 inbox（幂等）→ 立刻返回 200（<3s，空 body）
      ▼
  异步 worker（SQLite 队列）
      │  ③ 出队 → 幂等检查 → 映射成 ChatMessage
      ▼
  ChatService.handle()          ← 现有链路原封不动复用（RAG + 三道闸门 + 转人工）
      │  ④ 拿到 answer
      ▼
  POST /customer_service/202309/conversations/{conversation_id}/messages
      │  ⑤ 请求签名 + x-tts-access-token + shop_cipher
      ▼
  买家在 TikTok 里看到回复
```

---

## 3. 关键接口清单

| 用途 | 方法 + 路径 | 凭据 / 备注 |
|---|---|---|
| 配置 webhook | `PUT /event/202309/webhooks` ✅⚠️ | body `event_type: NEW_MESSAGE` + `address`；需 `shop_cipher` + 卖家 token |
| **收消息（入站）** | 平台回调我们的 `POST /webhooks/tiktok/shop` | `Authorization: <HMAC-SHA256 hex>` ⚠️ |
| **发回复（出站）** | `POST /customer_service/202309/conversations/{conversation_id}/messages` ✅ | scope `seller.customer_service`，header `x-tts-access-token` |
| 读会话历史 | `GET /customer_service/202309/conversations/{conversation_id}/messages` ⚠️ | `page_size ≤ 10`，用 `next_page_token` 翻页 |
| 列会话 | `GET /customer_service/202309/conversations` ✅ | `page_size` 1–20；可带 `need_session_info` 拿会话阶段 |
| 建会话 | `POST /customer_service/202309/conversations` ⚠️ | 主动发起时用 |
| 刷新 token | `GET /token/refresh` ⚠️ | access_token 会过期，必须实现自动刷新 |

- Base URL：`https://open-api.tiktokglobalshop.com` ✅
- 公共 query 参数：`app_key` / `sign` / `timestamp`（UTC Unix）/ `shop_cipher` ✅
- 限流：**50 req/s per shop** ⚠️

---

## 4. 落到本项目：新增文件清单

沿用现有分层（`config.py` 分层配置 → `channels/` 适配层 → `services/` 业务层），
**不动 `ChatService` 一行**（它是渠道无关的）。

```
src/customer_support/
├── config.py                      # + TikTokSettings（前缀 TIKTOK_，字段标 StrList）
├── channels/                      # 【新增】外部渠道适配层
│   └── tiktok/
│       ├── __init__.py
│       ├── signature.py           # HMAC-SHA256 验签（入站）+ 请求签名（出站）
│       ├── models.py              # webhook payload 的 Pydantic 模型
│       ├── client.py              # 调 TikTok Shop API（发消息/读历史/刷新 token）
│       ├── webhook.py             # FastAPI router：验签 → 落 inbox → 200
│       └── worker.py              # 后台协程：出队 → ChatService → 回发
├── store/
│   └── inbox.py                   # 【新增】webhook 事件表 + 幂等键（风格同 store/tickets.py）
└── api/v1/router.py               # 挂 webhook 路由，**必须挂在 get_current_user 之外**
```

### 4.1 入站验签（最容易写错的地方）

签名内容 = `app_key` + **原始请求体字节**，密钥 = `app_secret`，算法 HMAC-SHA256，结果 hex，
放在 `Authorization` 头 ⚠️。

```python
# channels/tiktok/signature.py
import hashlib, hmac

def verify_webhook(raw_body: bytes, authorization: str, app_key: str, app_secret: str) -> bool:
    """对**原始字节**验签。"""
    expected = hmac.new(app_secret.encode(), app_key.encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, (authorization or "").strip().lower())
```

⚠️ **两个致命细节**：
1. **必须拿 `await request.body()` 的原始字节**。用 Pydantic 模型接参会触发 JSON 重新序列化，
   空白/编码一变，签名就验不过——「明明测试通过、上线全 401」多半是这个原因。
2. 用 `hmac.compare_digest`，不要 `==`。

### 4.2 webhook 端点（3 秒铁律）

```python
# channels/tiktok/webhook.py
@router.post("/webhooks/tiktok/shop", include_in_schema=False)
async def receive(request: Request, inbox: InboxStore = Depends(get_inbox)) -> Response:
    raw = await request.body()                      # ← 原始字节
    if not verify_webhook(raw, request.headers.get("authorization", ""),
                          s.tiktok.app_key, s.tiktok.app_secret):
        raise HTTPException(401, "invalid signature")   # 验签失败返 401
    payload = json.loads(raw)
    if payload.get("type") != 14:                   # 只处理 NEW_MESSAGE
        return Response(status_code=200)            # 其它事件也要 200，否则平台重试
    # 幂等写入后立刻返回，绝不在请求里调 RAG
    inbox.insert_if_absent(payload["tts_notification_id"], payload["message_id"], raw)
    return Response(status_code=200)                # 空 body、<3s
```

> ⚠️ 「**先持久化、后处理**」是硬要求：TikTok 要求 3 秒内返回 200 空 body ⚠️。
> 你在这里调 RAG（要 1~3s）、调工单系统、下媒体，必然超时 → 平台重试 → 重复回复。

### 4.3 幂等（平台会重试 4 次）

失败重试节奏：**2 分钟 → 30 分钟 → 3 小时 → 12 小时** ⚠️。所以收到同一事件是**常态**：

- 主键去重：`tts_notification_id`（通知级）+ `message_id`（消息级）**双键**去重 ⚠️
- 重放安全：worker 重跑同一 job 必须不产生第二条回复
- 保留 `conversation_id` + `index`：用来**发现消息缺口**，而不只是"抑制重复"

### 4.4 消息顺序与补漏

- **按 `index` 排序，不要按到达顺序**：`index` 越大越新 ⚠️
- 出现 index 缺口 / worker 重启 / 人工要求回放时，调
  `GET /customer_service/202309/conversations/{id}/messages` 回补 ⚠️
- 注意：**读历史不会把消息标记为已读**，要另外调 Read Message，且只在你**确实消费了**这条消息后才调 ⚠️

### 4.5 回传路径：从 `ChatResult` 到买家聊天框

回传分两段：**worker 拿到结果** → **client 发出去**。

```python
# channels/tiktok/worker.py（要点）
async def _process(job: InboxJob) -> None:
    msg = ChatMessage(
        question=job.text,
        session_id=f"tts:{job.shop_id}:{job.conversation_id}",  # 多轮上下文直接生效
        buyer_id=job.sender_open_id,                            # 进工单与统计
        language=None,                                          # 交给本项目识别（ja 买家自动走日语）
    )
    # 用同步 handle()，不要 handle_stream()——TikTok 不需要 SSE 逐字
    result: ChatResult = await asyncio.to_thread(chat_service.handle, msg)

    # 只把「给买家看的文本」发出去
    await tiktok_client.send_message(
        conversation_id=job.conversation_id,
        text=result.answer,          # ← 回传的就是这一个字段
        index=job.index,             # 带上 index，平台侧保持顺序
    )
```

**只有 `answer` 会回到买家。** 其余字段全是内部观测信息，不上 TikTok：

| `ChatResult` 字段 | 去向 | 为什么 |
|---|---|---|
| `answer` | **发给买家** | 唯一面向买家的文本 |
| `references` / `confidence` | 留在本项目（运营控制台） | 给运营复盘用；发给买家是噪音，还会暴露知识库结构 |
| `need_human` / `human_reason` / `ticket_id` | 留在本项目（工单 + 统计） | 内部流程状态，平台侧看不到 |
| `trace_id` / `latency_ms` / `stage_ms` | 留在本项目（日志） | 排查与耗时分析 |
| `cached` / `prompt_tokens` / `estimated_cost` | 留在本项目（指标） | 成本核算 |

**转人工时发什么**：`answer` 此时是 `_human_answer(language)` 生成的「已转接人工专员」类
安全话术，**不含任何承诺**，所以照样发出——买家不会「问了没人理」，同时工单已在运营控制台排队。

**发送侧的三条纪律**（和入站一样重要）：
1. **发送前再查一次幂等**：平台重推 + worker 重跑都会走到这一步，用 `message_id` 判断
   「这条是否已回复过」，避免重复发送。
2. **按会话串行**：同一 `conversation_id` 的发送排队，避免一次追问连发多条、次序错乱。
3. **发送失败单独重试**：网络抖动 / `401` token 过期（先 `GET /token/refresh` 再重发）；
   重试计数与入站分开，别混进同一条队列。

**为什么可以慢慢发（不受 3 秒限制）**：3 秒铁律只约束**入站**（webhook 端点）。
RAG 那 1~3s 是在 **worker** 里跑的，此时早已回了 200，平台不再计时——
这正是「先 200、后处理」拆开的收益。

**出站请求的形状**：

```
POST https://open-api.tiktokglobalshop.com/customer_service/202309/conversations/{conversation_id}/messages
  ?app_key=...&sign=...&timestamp=...(UTC 秒)&shop_cipher=...
Headers:
  x-tts-access-token: <seller access_token>
  Content-Type: application/json
Body:
  { …回复内容字段… }
```

❓ **出站 `sign` 算法与 body 字段名**仍未从官方页面核实到 → 照官方「签名算法」页 +
Send Message 文档实现，**不要照抄网上博客**。这是全文唯一需要你查官方文档的地方。

---

## 5. 与会话 / 转人工如何衔接

| TikTok 字段 | 映射到本项目 |
|---|---|
| `shop_id` + `conversation_id` | `session_id = f"tts:{shop_id}:{conversation_id}"`（多轮上下文天然生效） |
| 发送者 open_id / user_id | `buyer_id`（进工单与统计） |
| 文本内容 | `ChatMessage.question` |
| `message_id` / `index` | 记进 inbox，用于去重与排查 |

**转人工的处理**（这是本项目的价值所在，别丢掉）：
- `need_human=true` 时，**仍然把兜底话术/转人工话术发给买家**（它是安全的、不含承诺），
  同时工单已进运营控制台，人工照常处理 → 买家不会"问了没人理"。
- `high_risk_intent`（退款/投诉/纠纷）**绝不自动承诺**，只回"已转专员跟进"。
- 可选增强：把会话在 TikTok 侧标为需要坐席（依 API 能力而定）。

---

## 6. 回复策略（避免回错、回重复、被处罚）

1. **只回买家发的、可见的文本消息**；系统消息 / 非文本（图片、商品卡片）→ 落到
   `review` 状态，**不要**硬转成文本（否则会把卡片内容当问题答）⚠️
2. **人工已接管就停自动回复**：用 `GET /conversations` 的 `need_session_info` 拿会话阶段
   （`AUTO_REPLY` / `QUEUED` / `ASSIGNED`）✅——阶段不是 `AUTO_REPLY` 说明已有坐席，别抢话。
3. **控频**：50 req/s/shop 是上限，但要更保守——加发送队列 + 每条会话串行，
   避免"一次追问连发三条回复"。
4. **回执与失败**：发送失败要有独立重试（与入站重试分开），并记日志便于复盘。

---

## 7. 上线前验收（照着跑一遍）

- [ ] 买家发一条消息 → 端点收到 `type=14`、验签通过、**3 秒内返回 200 空 body**
- [ ] 同一事件重投（手动重放）→ **不产生第二条回复**
- [ ] 一个会话正确建立/复用，`session_id` 稳定
- [ ] `GET messages` 的 `message_id` 与 webhook 一致；人为制造 index 缺口能被回补
- [ ] Partner Center → Development Kits → **Webhook Log** 显示 delivery accepted ⚠️
- [ ] 高风险词（返金/クレーム）→ 转人工工单生成、不自动承诺
- [ ] 人工接管后自动回复停止

---

## 8. 三种路线对比

| 路线 | 优点 | 缺点 | 建议 |
|---|---|---|---|
| **A. 自建（本文）** | 完全可控；RAG / 转人工 / 工单全部打通 | 需自行处理验签 / 幂等 / 限流 / 重试 | ✅ **推荐** |
| B. 第三方桥（UnifyPort 等） | 免写 webhook | 多一层依赖、多一份费用；仍需你自己发回复才能接 RAG | 仅当不想碰 webhook |
| C. 现成 MCP server（`mcp-tiktokshop`） | 出站发回复可快速验证 | 它不含你的 RAG，仍需自建入站+大脑 | 可作为**出站联调**的临时工具 |

---

## 9. 待你确认后才能开工的点

1. **是否已有 Partner Center 的 `seller.customer_service` scope 审批**（没有就先别写，写完整套也联调不了）
2. **出站 `sign` 算法 + Send Message 请求体字段**（需官方文档；我不编）
3. 店铺是**自营**还是**多店**：多店要引入 `shop_id → tenant` 映射（对应现有 `auth/` 角色体系）
