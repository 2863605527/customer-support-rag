/** 会话状态管理：消息列表、session_id 持久化、发送/重置（SSE 流式渲染）。 */
import { reactive, readonly, ref } from "vue";
import { chatStream, consoleApi, humanizeError, resetSession } from "@/api/client";
import type { ChatStep, ChatUiMessage } from "@/types";

const SESSION_KEY = "cs.session_id";
const MESSAGES_KEY = "cs.messages";
const MAX_PERSISTED = 60; // 本地最多保留的消息条数（防 localStorage 膨胀）

/** 链路阶段 → 用户可读的「思考过程」文案（与后端 status 事件一一对应）。 */
const STAGE_LABEL: Record<string, string> = {
  detect_language: "正在识别语言",
  rewrite: "正在结合上下文理解问题",
  intent: "正在判断买家意图",
  retrieval: "正在检索知识库",
  generate: "正在生成回答",
  cache_hit: "命中缓存，复用历史回答",
};

function stageLabel(stage: string): string {
  return STAGE_LABEL[stage] ?? stage;
}

/** 把 status 事件的 detail 转成人类可读的补充说明。 */
function stageDetail(stage: string, detail: Record<string, unknown> | undefined): string | undefined {
  if (!detail) return undefined;
  if (stage === "generate" && typeof detail.hits === "number") {
    return detail.hits > 0 ? `命中 ${detail.hits} 条资料` : "未检索到相关资料";
  }
  if (stage === "retrieval" && typeof detail.matched_product === "string") {
    return `已识别商品「${detail.matched_product}」`;
  }
  return undefined;
}

function newSessionId(): string {
  return `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

const sessionId = ref<string>(localStorage.getItem(SESSION_KEY) ?? newSessionId());
localStorage.setItem(SESSION_KEY, sessionId.value);

const messages = ref<ChatUiMessage[]>(restoreMessages());
const sending = ref(false);

/** 刷新/重开浏览器后恢复上次会话（只恢复可序列化的渲染字段）。 */
function restoreMessages(): ChatUiMessage[] {
  try {
    const raw = localStorage.getItem(MESSAGES_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as ChatUiMessage[];
    if (!Array.isArray(parsed)) return [];
    // 中断中的流式消息恢复为普通完成态，避免恢复出永远转圈的气泡
    return parsed
      .filter((m) => m && (m.role === "buyer" || m.role === "agent"))
      .map((m) => ({ ...m, pending: false, streaming: false }));
  } catch {
    return [];
  }
}

function persistMessages(): void {
  try {
    const trimmed = messages.value.slice(-MAX_PERSISTED).map((m) => ({
      role: m.role,
      text: m.text,
      at: m.at,
      pending: false,
      error: m.error,
      steps: m.steps,
      result: m.result,
    }));
    localStorage.setItem(MESSAGES_KEY, JSON.stringify(trimmed));
  } catch {
    // localStorage 满/被禁用时静默放弃持久化，不影响聊天
  }
}

async function send(question: string): Promise<void> {
  const text = question.trim();
  if (!text || sending.value) return;

  messages.value.push({ role: "buyer", text, at: Date.now() });
  // 必须 reactive：普通对象 push 进列表后，模板持有的是代理，
  // 直接改原始对象不会触发重渲染（气泡会永远停在"…"）。
  const pending = reactive<ChatUiMessage>({ role: "agent", text: "", pending: true, at: Date.now() });
  messages.value.push(pending);
  sending.value = true;
  persistMessages();

  /** 标记所有步骤为已完成，并可追加一个新步骤。 */
  const finishSteps = (next?: ChatStep): void => {
    pending.steps = (pending.steps ?? []).map((s) => ({ ...s, state: "done" as const }));
    if (next) pending.steps = [...pending.steps, next];
  };

  try {
    const result = await chatStream(text, sessionId.value, {
      onStatus: (stage, detail) => {
        // 新阶段到来 → 之前的阶段都算完成，展示新阶段为进行中
        finishSteps({ label: stageLabel(stage), detail: stageDetail(stage, detail), state: "active" });
      },
      onDelta: (delta) => {
        // 首个增量到达即结束「思考中」，开始逐字渲染
        finishSteps();
        pending.streaming = true;
        pending.text += delta;
      },
      onReplace: (full) => {
        // 资料不足：已流出的内容（哨兵/残句）整段替换为兜底话术
        finishSteps();
        pending.streaming = true;
        pending.text = full;
      },
    });
    pending.text = result.answer;
    finishSteps();
    pending.streaming = false;
    pending.pending = false;
    pending.result = result;
    persistMessages();
  } catch (err) {
    pending.text = humanizeError(err);
    finishSteps();
    pending.streaming = false;
    pending.pending = false;
    pending.error = true;
    persistMessages();
  } finally {
    sending.value = false;
  }
}

async function clearSession(): Promise<void> {
  try {
    await resetSession(sessionId.value);
  } catch {
    // 后端不可达也要允许前端换新会话
  }
  sessionId.value = newSessionId();
  localStorage.setItem(SESSION_KEY, sessionId.value);
  messages.value = [];
  localStorage.removeItem(MESSAGES_KEY);
}

/**
 * 打开服务端保存的一通会话（跨设备 / 复盘用）。
 *
 * 与 localStorage 恢复的区别：这里读到的是**服务端完整记录**（含引用、工单号），
 * 而不是本浏览器缓存的渲染快照，因此换一台设备也能看到同样的历史。
 */
async function openConversation(target: string): Promise<void> {
  const detail = await consoleApi.conversation(target);
  sessionId.value = detail.session_id;
  localStorage.setItem(SESSION_KEY, detail.session_id);

  const restored: ChatUiMessage[] = detail.messages.map((m) => {
    const at = new Date(m.created_at).getTime();
    if (m.role === "buyer") return { role: "buyer", text: m.text, at };
    return {
      role: "agent",
      text: m.text,
      at,
      result: {
        question: "",
        answer: m.text,
        intent: m.intent,
        need_human: m.need_human,
        human_reason: m.human_reason,
        language: detail.language,
        source: m.source,
        confidence: m.confidence,
        references: m.references,
        ticket_id: m.ticket_id,
        session_id: detail.session_id,
        trace_id: m.trace_id,
        latency_ms: m.latency_ms,
        answered_at: m.created_at,
      },
    };
  });
  messages.value = restored;
  persistMessages();
}

export function useChat() {
  return {
    sessionId,
    messages,
    sending: readonly(sending),
    send,
    clearSession,
    openConversation,
  };
}
