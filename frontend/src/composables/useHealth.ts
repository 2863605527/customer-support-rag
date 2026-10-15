/**
 * 后端健康状态的全局单例轮询。
 *
 * 为什么做成模块级单例：状态徽章（顶栏）与详情列表（工作台侧栏）都要读同一份
 * readyz 结果。若各自 `setInterval`，就是两次探针、两份可能不一致的状态。
 * 这里只维护一份状态 + 一个定时器，任何页面组件都能直接取用。
 */
import { computed, ref } from "vue";
import { fetchReady } from "@/api/client";
import type { ReadyzBody } from "@/types";

const DEFAULT_INTERVAL_MS = 30_000;

const ready = ref<ReadyzBody | null>(null);
const reachable = ref<boolean | null>(null);
let timer: number | undefined;

async function poll(): Promise<void> {
  try {
    ready.value = await fetchReady();
    reachable.value = true;
  } catch {
    reachable.value = false;
  }
}

function start(intervalMs = DEFAULT_INTERVAL_MS): void {
  if (timer !== undefined) return; // 幂等：多个页面挂载也只跑一个定时器
  void poll();
  timer = window.setInterval(poll, intervalMs);
}

function stop(): void {
  window.clearInterval(timer);
  timer = undefined;
}

/** 检索后端是否可用（兼容 ok 在顶层 / 嵌在 health 里的两种返回）。 */
const retrievalOk = computed(
  () => ready.value?.checks.retrieval?.ok ?? ready.value?.checks.retrieval?.health?.ok ?? false,
);

/** LLM 是否真的可用（欠费 / 熔断时 ok=false，与「key 有没有配」是两回事）。 */
const llmOk = computed(
  () => ready.value?.checks.llm?.ok ?? Boolean(ready.value?.checks.llm_configured),
);

const llmQuotaExhausted = computed(() => Boolean(ready.value?.checks.llm?.quota_exhausted));

const statusText = computed(() => {
  if (reachable.value === null) return "检测中…";
  if (!reachable.value) return "后端未连接";
  if (!ready.value?.ready) return "部分未就绪";
  return "服务就绪";
});

const docCount = computed(() => ready.value?.checks.retrieval?.count ?? null);

export function useHealth() {
  return {
    ready,
    reachable,
    statusText,
    retrievalOk,
    llmOk,
    llmQuotaExhausted,
    docCount,
    start,
    stop,
    poll,
  };
}
