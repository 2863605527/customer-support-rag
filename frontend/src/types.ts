/** 与后端 Pydantic 模型（src/customer_support/models/*）一一对应的类型定义。 */

export interface Reference {
  source: string;
  doc_id: string;
  score: number;
  snippet: string;
}

export interface ChatResult {
  question: string;
  answer: string;
  intent: string;
  need_human: boolean;
  human_reason: string;
  language: string;
  source: string;
  confidence: number;
  references: Reference[];
  ticket_id: string | null;
  session_id: string | null;
  trace_id: string;
  latency_ms: number;
  answered_at: string;
  /** 是否命中问答缓存（命中时不消耗模型额度） */
  cached?: boolean;
  /** 分阶段耗时（毫秒）：language / rewrite / intent / retrieval_generate */
  stage_ms?: Record<string, number>;
  prompt_tokens?: number;
  completion_tokens?: number;
  estimated_cost?: number;
}

export interface ReadyzChecks {
  llm_configured?: boolean;
  /** LLM 实际可用性（欠费/熔断时 ok=false） */
  llm?: {
    ok?: boolean;
    quota_exhausted?: boolean;
    last_error_category?: string | null;
    last_error?: string | null;
    consecutive_failures?: number;
    total_calls?: number;
    total_failures?: number;
  };
  retrieval?: {
    /** 检索后端健康（后端已从 health.ok 提升到顶层） */
    ok?: boolean;
    /** 兼容字段：部分版本把 ok 嵌在 health 里 */
    health?: { ok?: boolean; dataset_ids?: string[] };
    count?: number;
    error?: string;
  };
}

export interface ReadyzBody {
  ready: boolean;
  checks: ReadyzChecks;
}

/** 思考过程的一个步骤（对齐后端 status 事件，DeepSeek 式逐步展示）。 */
export interface ChatStep {
  label: string;
  /** 补充信息（如「命中 N 条资料」「已识别商品 XXX」） */
  detail?: string;
  /** active = 进行中（脉冲点），done = 已完成（对勾） */
  state: "active" | "done";
}

/** 前端本地的一条会话消息（在 ChatResult 基础上增加渲染状态）。 */
export interface ChatUiMessage {
  role: "buyer" | "agent";
  text: string;
  pending?: boolean;
  error?: boolean;
  result?: ChatResult;
  at: number;
  /** 流式渲染中：文本末尾显示光标 */
  streaming?: boolean;
  /** 思考过程步骤列表（识别语言 → 改写 → 意图 → 检索 → 生成），完成后保留可展开 */
  steps?: ChatStep[];
}

/** SSE 流式事件（对应后端 /chat/stream 的事件帧）。 */
export interface ChatStreamHandlers {
  onStatus?: (stage: string, detail?: Record<string, unknown>) => void;
  onDelta?: (text: string) => void;
  onReplace?: (text: string) => void;
}

// ==================== 鉴权（对应 models/user.py） ====================

export type Role = "admin" | "agent";

export interface UserPublic {
  id: string;
  email: string;
  full_name: string;
  role: Role;
  is_active: boolean;
  created_at: string;
  last_login_at: string | null;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
  user: UserPublic;
}

export interface CaptchaChallenge {
  captcha_id: string;
  image_svg: string;
  expires_in: number;
}

/** 后端统一错误体（含可供前端分支的非敏感上下文）。 */
export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    detail?: string | null;
    context?: {
      failed_attempts?: number;
      remaining_attempts?: number;
      captcha_required?: boolean;
      locked_seconds?: number;
      retry_after_seconds?: number;
    };
  };
}

export interface AuditEntry {
  id: number;
  email: string;
  user_id: string | null;
  event: string;
  ip: string;
  user_agent: string;
  detail: string;
  created_at: string;
}

// ==================== 运营控制台（对应 models/console.py 等） ====================

export type TicketStatus = "open" | "in_progress" | "resolved" | "closed";
export type TicketPriority = "low" | "normal" | "high" | "urgent";

export interface TicketReply {
  id: number;
  ticket_id: string;
  author: string;
  body: string;
  internal: boolean;
  created_at: string;
}

export interface Ticket {
  ticket_id: string;
  created_at: string;
  updated_at: string;
  status: TicketStatus;
  priority: TicketPriority;
  assignee: string;
  question: string;
  language: string;
  intent: string;
  confidence: number;
  reason: string;
  asked_answer: string;
  buyer_id: string | null;
  session_id: string | null;
  trace_id: string;
  sources: string[];
  first_response_at: string | null;
  resolved_at: string | null;
  replies: TicketReply[];
}

export interface TicketPage {
  items: Ticket[];
  total: number;
  limit: number;
  offset: number;
}

export interface TicketStats {
  window_days: number;
  total: number;
  backlog: number;
  unassigned: number;
  overdue: number;
  by_status: Record<string, number>;
  by_priority: Record<string, number>;
  by_reason: Record<string, number>;
  by_assignee: Record<string, number>;
  avg_first_response_minutes: number | null;
  avg_resolve_minutes: number | null;
}

export interface StoredMessage {
  id: number;
  session_id: string;
  role: "buyer" | "agent";
  text: string;
  intent: string;
  need_human: boolean;
  human_reason: string;
  source: string;
  confidence: number;
  ticket_id: string | null;
  trace_id: string;
  latency_ms: number;
  references: Reference[];
  created_at: string;
}

export interface ConversationSummary {
  session_id: string;
  buyer_id: string;
  language: string;
  created_at: string;
  updated_at: string;
  message_count: number;
  handoff_count: number;
  last_question: string;
  ticket_ids: string[];
}

export interface ConversationDetail extends ConversationSummary {
  messages: StoredMessage[];
}

export interface ConversationPage {
  items: ConversationSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface FeedbackStats {
  total: number;
  up: number;
  down: number;
  positive_rate: number | null;
  by_source: Record<string, { up: number; down: number; total: number }>;
  by_reason: Record<string, number>;
  recent_down: FeedbackRecord[];
}

export interface FeedbackRecord {
  id: number;
  trace_id: string;
  session_id: string | null;
  rating: "up" | "down";
  reasons: string[];
  comment: string;
  question: string;
  answer: string;
  source: string;
  created_at: string;
}

/** 运营总览（/ops/analytics 的返回结构，只声明用到的字段）。 */
export interface AnalyticsOverview {
  window_days: number;
  conversation: {
    conversations: number;
    questions: number;
    answers: number;
    handoffs: number;
    containment_rate: number | null;
    handoff_rate: number | null;
    avg_latency_ms: number;
    by_source: Record<string, number>;
    by_intent: Record<string, number>;
    by_handoff_reason: Record<string, number>;
    daily: { day: string; questions: number; answers: number; handoffs: number; avg_latency_ms: number }[];
  };
  ticket: TicketStats;
  feedback: FeedbackStats;
  cache: { entries: number; hits: number };
  llm: ReadyzChecks["llm"];
  breakers: { name: string; state: string; failures: number; successes: number; retry_after_seconds: number }[];
}

export interface KnowledgeDocument {
  id: string;
  name: string;
  size: number;
  chunk_count: number;
  run: string;
  status: string;
}

export interface SearchProbeHit {
  source: string;
  doc_id: string;
  score: number;
  text: string;
}

export interface SearchProbeResult {
  query: string;
  hits: SearchProbeHit[];
  total: number;
  threshold_passed: number;
}
