/** axios 实例与鉴权拦截器：统一 baseURL、令牌注入、401 自动刷新重试、错误归一化。 */
import axios, { AxiosError, type InternalAxiosRequestConfig } from "axios";
import type {
  AnalyticsOverview,
  ApiErrorBody,
  AuditEntry,
  CaptchaChallenge,
  ChatResult,
  ChatStreamHandlers,
  ConversationDetail,
  ConversationPage,
  FeedbackRecord,
  FeedbackStats,
  KnowledgeDocument,
  ReadyzBody,
  SearchProbeResult,
  Ticket,
  TicketPage,
  TicketPriority,
  TicketStats,
  TicketStatus,
  TokenPair,
  UserPublic,
} from "@/types";

export const api = axios.create({
  // 开发走 vite 代理（见 vite.config.ts）；生产可覆盖为完整后端地址
  baseURL: import.meta.env.VITE_API_BASE_URL ?? "/api/v1",
  timeout: 60_000, // RAG 链路含检索 + LLM 生成，长超时是刻意的
});

// 由 useAuth 注册：401 且刷新失败时清空本地登录态
let unauthorizedHandler: (() => void) | null = null;
export function setUnauthorizedHandler(handler: () => void): void {
  unauthorizedHandler = handler;
}

/** 令牌提供者（避免 client 与 useAuth 循环依赖，这里用注入而非直接 import）。 */
let tokenProvider: () => string | null = () => null;
let refreshHandler: (() => Promise<string | null>) | null = null;
export function configureAuth(options: {
  getToken: () => string | null;
  refresh: () => Promise<string | null>;
}): void {
  tokenProvider = options.getToken;
  refreshHandler = options.refresh;
}

api.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = tokenProvider();
  if (token) {
    config.headers.set("Authorization", `Bearer ${token}`);
  }
  return config;
});

api.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const original = error.config as (InternalAxiosRequestConfig & { _retried?: boolean }) | undefined;
    const status = error.response?.status;

    // 401：访问令牌过期 → 静默刷新一次并重放原请求；刷新失败才真正登出
    if (status === 401 && original && !original._retried && refreshHandler) {
      original._retried = true;
      const newToken = await refreshHandler();
      if (newToken) {
        original.headers.set("Authorization", `Bearer ${newToken}`);
        return api.request(original);
      }
      unauthorizedHandler?.();
    }
    return Promise.reject(error);
  },
);

/** 把任意异常归一化成可展示的字符串（不向外暴露堆栈细节）。 */
export function humanizeError(err: unknown): string {
  if (err instanceof AxiosError) {
    if (err.code === "ECONNABORTED") return "请求超时，后端可能正在处理复杂问题，请稍后重试。";
    const body = err.response?.data as ApiErrorBody | undefined;
    if (body?.error?.message) return body.error.message;
    const detail = (err.response?.data as { detail?: unknown } | undefined)?.detail;
    if (typeof detail === "string") return `后端返回错误：${detail}`;
    if (err.response) return `后端返回错误：HTTP ${err.response.status}`;
    return "无法连接后端服务，请确认 customer-support serve 已启动。";
  }
  return err instanceof Error ? err.message : "未知错误";
}

/** 取出后端错误里的非敏感上下文（剩余次数、是否需要验证码…）。 */
export function errorContext(err: unknown): NonNullable<ApiErrorBody["error"]["context"]> {
  if (err instanceof AxiosError) {
    const body = err.response?.data as ApiErrorBody | undefined;
    return body?.error?.context ?? {};
  }
  return {};
}

// ==================== 鉴权接口 ====================

export const authApi = {
  captcha: async (): Promise<CaptchaChallenge> => (await api.get<CaptchaChallenge>("/auth/captcha")).data,

  login: async (payload: {
    email: string;
    password: string;
    captcha_id?: string | null;
    captcha_code?: string | null;
  }): Promise<TokenPair> => (await api.post<TokenPair>("/auth/login", payload)).data,

  register: async (payload: {
    email: string;
    password: string;
    full_name?: string;
  }): Promise<UserPublic> => (await api.post<UserPublic>("/auth/register", payload)).data,

  refresh: async (refreshToken: string): Promise<TokenPair> =>
    (await api.post<TokenPair>("/auth/refresh", { refresh_token: refreshToken })).data,

  logout: async (refreshToken: string | null): Promise<void> => {
    await api.post("/auth/logout", refreshToken ? { refresh_token: refreshToken } : {});
  },

  me: async (): Promise<UserPublic> => (await api.get<UserPublic>("/auth/me")).data,

  changePassword: async (oldPassword: string, newPassword: string): Promise<void> => {
    await api.post("/auth/change-password", { old_password: oldPassword, new_password: newPassword });
  },

  listUsers: async (): Promise<UserPublic[]> => (await api.get<UserPublic[]>("/auth/users")).data,

  auditLogs: async (limit = 50): Promise<AuditEntry[]> =>
    (await api.get<AuditEntry[]>(`/auth/audit-logs?limit=${limit}`)).data,
};

// ==================== 业务接口 ====================

export async function chat(
  question: string,
  sessionId: string | null,
  buyerId?: string,
): Promise<ChatResult> {
  const { data } = await api.post<ChatResult>("/chat", {
    question,
    session_id: sessionId,
    buyer_id: buyerId ?? "web-console",
  });
  return data;
}

/**
 * SSE 流式聊天：与 /chat 同一条业务链路，逐字接收回答。
 *
 * - 事件：status（链路阶段）/ delta（回答增量）/ replace（整段替换）/ done（终态 ChatResult）；
 * - 鉴权：手动带 Bearer；401 时静默刷新一次并重试（与 axios 拦截器同策略）；
 * - 看门狗：连续 90s 没有任何事件视为连接异常，中断并抛错（避免 UI 永远转圈）。
 */
export async function chatStream(
  question: string,
  sessionId: string | null,
  handlers: ChatStreamHandlers,
  buyerId?: string,
): Promise<ChatResult> {
  const baseURL = String(api.defaults.baseURL ?? "/api/v1");

  // 中断器必须建在 fetch 之前，并把 signal 传给 fetch：旧实现只在 90s 超时后
  // 调用了 controller.abort()，却没把 signal 交给 fetch，于是 reader.read() 永远
  // 挂着、看门狗形同虚设（连接断了 UI 会一直转圈）。
  const controller = new AbortController();

  const post = (token: string | null): Promise<Response> =>
    fetch(`${baseURL}/chat/stream`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({
        question,
        session_id: sessionId,
        buyer_id: buyerId ?? "web-console",
      }),
      signal: controller.signal,
    });

  let resp = await post(tokenProvider());
  if (resp.status === 401 && refreshHandler) {
    const newToken = await refreshHandler();
    if (newToken) {
      resp = await post(newToken);
    } else {
      unauthorizedHandler?.();
    }
  }
  if (!resp.ok || !resp.body) {
    let message = `后端返回错误：HTTP ${resp.status}`;
    try {
      const body = (await resp.json()) as ApiErrorBody;
      if (body?.error?.message) message = body.error.message;
    } catch {
      // 非 JSON 错误体，保持默认文案
    }
    throw new Error(message);
  }

  // ---- 解析 SSE：按空行分帧，帧内 event:/data: 行 ----
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalResult = null as ChatResult | null;
  let watchdog = 0;
  const resetWatchdog = (): void => {
    window.clearTimeout(watchdog);
    watchdog = window.setTimeout(() => controller.abort(), 90_000);
  };
  resetWatchdog();

  const handleFrame = (frame: string): void => {
    let event = "message";
    const dataLines: string[] = [];
    for (const line of frame.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
    }
    if (!dataLines.length) return;
    let payload: Record<string, unknown>;
    try {
      payload = JSON.parse(dataLines.join("\n")) as Record<string, unknown>;
    } catch {
      return; // 半帧或非 JSON，忽略
    }
    if (event === "status") {
      const { stage, ...detail } = payload;
      handlers.onStatus?.(String(stage ?? ""), detail as Record<string, unknown>);
    } else if (event === "delta") handlers.onDelta?.(String(payload.text ?? ""));
    else if (event === "replace") handlers.onReplace?.(String(payload.text ?? ""));
    else if (event === "done") finalResult = payload.result as ChatResult;
  };

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      resetWatchdog();
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        if (frame.trim()) handleFrame(frame);
      }
    }
    if (buffer.trim()) handleFrame(buffer);
  } catch (err) {
    if (controller.signal.aborted) {
      throw new Error("连接超时：后端长时间没有返回内容，请稍后重试。");
    }
    throw err;
  } finally {
    window.clearTimeout(watchdog);
  }

  if (!finalResult) {
    throw new Error("连接中断：没有收到完整回答，请重试。");
  }
  return finalResult;
}

export async function resetSession(sessionId: string): Promise<void> {
  await api.post(`/sessions/${encodeURIComponent(sessionId)}/reset`);
}

export async function fetchReady(): Promise<ReadyzBody> {
  // readyz 在根路径，不在 /api/v1 下；探针免鉴权
  const baseURL = api.defaults.baseURL ?? "/api/v1";
  const root = String(baseURL).replace(/\/api\/v1\/?$/, "");
  const { data } = await axios.get<ReadyzBody>(`${root}/readyz`, { timeout: 15_000 });
  return data;
}

// ==================== 运营控制台接口 ====================

export interface TicketQuery {
  status?: string;
  priority?: string;
  assignee?: string;
  reason?: string;
  search?: string;
  limit?: number;
  offset?: number;
}

/** 把查询参数对象拼成 query string（跳过 undefined / 空串，避免后端把空串当有效值）。 */
function qs(params: object): string {
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== "");
  if (!entries.length) return "";
  return "?" + entries.map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`).join("&");
}

export const consoleApi = {
  // ---- 工单闭环 ----
  tickets: async (query: TicketQuery = {}): Promise<TicketPage> =>
    (await api.get<TicketPage>(`/ops/tickets${qs(query)}`)).data,

  ticket: async (ticketId: string): Promise<Ticket> =>
    (await api.get<Ticket>(`/ops/tickets/${encodeURIComponent(ticketId)}`)).data,

  ticketStats: async (days = 7): Promise<TicketStats> =>
    (await api.get<TicketStats>(`/ops/tickets/stats${qs({ days })}`)).data,

  updateTicket: async (
    ticketId: string,
    patch: { status?: TicketStatus; priority?: TicketPriority; assignee?: string },
  ): Promise<Ticket> =>
    (await api.patch<Ticket>(`/ops/tickets/${encodeURIComponent(ticketId)}`, patch)).data,

  replyTicket: async (ticketId: string, body: string, internal = false): Promise<Ticket> =>
    (await api.post<Ticket>(`/ops/tickets/${encodeURIComponent(ticketId)}/replies`, { body, internal }))
      .data,

  // ---- 会话复盘 ----
  conversations: async (query: { search?: string; days?: number; limit?: number; offset?: number } = {}) =>
    (await api.get<ConversationPage>(`/ops/conversations${qs(query)}`)).data,

  conversation: async (sessionId: string): Promise<ConversationDetail> =>
    (await api.get<ConversationDetail>(`/ops/conversations/${encodeURIComponent(sessionId)}`)).data,

  // ---- 反馈 ----
  feedbackStats: async (days = 30): Promise<FeedbackStats> =>
    (await api.get<FeedbackStats>(`/ops/feedback/stats${qs({ days })}`)).data,

  // ---- 指标 ----
  analytics: async (days = 7): Promise<AnalyticsOverview> =>
    (await api.get<AnalyticsOverview>(`/ops/analytics${qs({ days })}`)).data,

  breakers: async (): Promise<AnalyticsOverview["breakers"]> =>
    (await api.get<AnalyticsOverview["breakers"]>("/ops/breakers")).data,

  // ---- 知识库运维 ----
  knowledgeDocuments: async (): Promise<KnowledgeDocument[]> =>
    (await api.get<KnowledgeDocument[]>("/ops/knowledge/documents")).data,

  knowledgeSearch: async (payload: {
    query: string;
    top_k?: number;
    score_threshold?: number;
  }): Promise<SearchProbeResult> =>
    (await api.post<SearchProbeResult>("/ops/knowledge/search", payload)).data,

  reingest: async (payload: { replace?: boolean; prune?: boolean; wait?: boolean }) =>
    (await api.post<{ accepted: boolean; message: string }>("/ops/knowledge/reingest", payload)).data,
};

export const feedbackApi = {
  reasons: async (): Promise<{ up: Record<string, string>; down: Record<string, string> }> =>
    (await api.get<{ up: Record<string, string>; down: Record<string, string> }>("/feedback/reasons")).data,

  submit: async (payload: {
    trace_id: string;
    rating: "up" | "down";
    reasons?: string[];
    comment?: string;
    session_id?: string | null;
    question?: string;
    answer?: string;
    source?: string;
  }): Promise<FeedbackRecord> => (await api.post<FeedbackRecord>("/feedback", payload)).data,
};
