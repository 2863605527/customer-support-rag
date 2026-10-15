# customer-support-web

智能客服前端（Vue 3 + Vite + TypeScript），对接 `customer-support-rag` 后端 API。

## 启动

```bash
cd frontend
npm install
npm run dev        # http://127.0.0.1:5173
```

前置条件：后端已在 `http://127.0.0.1:8002` 运行（`customer-support serve --port 8002`）。
开发服务器通过 Vite 代理把 `/api`、`/healthz`、`/readyz` 转发到后端（`vite.config.ts`），
后端地址可用环境变量覆盖：`BACKEND_ORIGIN=http://127.0.0.1:9000 npm run dev`。

## 构建

```bash
npm run build      # 产物在 dist/
npm run preview    # 本地预览构建产物
```

生产部署：`dist/` 是纯静态文件，交给 Nginx 托管，并把 `/api` 反向代理到 FastAPI（见根 README §部署）。

## 结构

```
src/
├── api/client.ts        # axios 封装：baseURL、令牌注入、401 自动刷新重试、错误归一化
├── composables/
│   ├── useAuth.ts       # 登录态：令牌存取、登录/注册/登出、单飞刷新
│   └── useChat.ts       # 会话状态：消息列表、session_id 持久化、发送/重置
├── router/index.ts      # 路由 + 登录守卫（默认全部需要登录，meta.public 才放行）
├── views/
│   ├── LoginView.vue    # 登录（含图形验证码按需出现）
│   ├── RegisterView.vue # 注册（实时密码强度 + 策略清单）
│   └── WorkspaceView.vue# 工作台：顶栏用户菜单 / 改密 / 管理员审计弹窗
├── components/
│   ├── ChatPanel.vue    # 消息列表 + 自动滚动 + 空态
│   ├── MessageItem.vue  # 气泡：回答/引用/意图徽章/工单/trace_id
│   ├── Composer.vue     # 输入框（Enter 发送，Shift+Enter 换行）
│   └── SidePanel.vue    # 会话管理、快捷提问、服务健康明细
├── types.ts             # 与后端 Pydantic 模型一一对应的类型
└── styles/              # main.css（设计变量）+ auth.css（登录注册页样式）
```

## 鉴权流程（前端视角）

1. **登录**：`POST /auth/login` → 拿到 `access_token`(60min) + `refresh_token`(14d)，存 localStorage；
2. **带令牌**：请求拦截器自动加 `Authorization: Bearer <token>`；
3. **静默续期**：收到 401 且未重试过 → 用 refresh 换新令牌（**并发只发一次**，避免互相作废刚轮换的令牌）→ 重放原请求；
4. **彻底失效**：刷新也失败 → 清空登录态并跳登录页，页面不会卡在「一直报错」的状态；
5. **刷新页面不闪**：应用挂载前先用 refresh 恢复会话（`main.ts` 里 `restoreSession()`）；
6. **路由守卫**：白名单式——只有 `meta.public` 的登录/注册页可匿名，其余一律要求登录。

## 功能对照

| 后端能力 | 前端呈现 |
|---|---|
| 多语言问答 + 引用溯源 | 气泡内展示引用来源、相似度分、切片摘要（默认折叠） |
| 转人工 + 工单 | `need_human` 红色徽章 + 工单号 + 转人工原因 |
| 多轮会话记忆 | `session_id` 存 localStorage，可一键「新会话」调 `/sessions/{id}/reset` |
| `trace_id` 链路追踪 | 每条 AI 回答可展开查看，报障时与后端日志对账 |
| `/readyz` 探针 | 顶栏状态灯 + 侧栏 LLM/检索后端健康明细，30s 轮询 |
| 登录/注册/改密/登出 | 独立页面 + 顶栏用户菜单；登录失败按后端 `context` 切换验证码 |
| 管理员审计 | 用户菜单 → 登录审计：时间 / 邮箱 / 事件 / IP / 详情 |
