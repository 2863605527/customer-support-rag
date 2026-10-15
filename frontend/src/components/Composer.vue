<script setup lang="ts">
import { ref } from "vue";

defineProps<{ sending: boolean }>();
const emit = defineEmits<{ send: [question: string] }>();

const draft = ref("");

function submit(): void {
  const text = draft.value.trim();
  if (!text) return;
  emit("send", text);
  draft.value = "";
}

function onEnter(e: Event): void {
  if ((e as KeyboardEvent).shiftKey) return; // Shift+Enter 换行
  e.preventDefault();
  submit();
}
</script>

<template>
  <footer class="composer">
    <textarea
      v-model="draft"
      rows="2"
      placeholder="输入买家问题，Enter 发送，Shift+Enter 换行（例：返品はできますか？）"
      :disabled="sending"
      @keydown.enter="onEnter"
    />
    <button class="send" :disabled="sending || !draft.trim()" @click="submit">
      {{ sending ? "处理中…" : "发送" }}
    </button>
  </footer>
</template>

<style scoped>
.composer {
  display: flex;
  gap: 10px;
  padding: 12px 16px;
  border-top: 1px solid var(--c-border);
  background: var(--c-panel);
}
textarea {
  flex: 1;
  resize: none;
  border: 1px solid var(--c-border);
  border-radius: 8px;
  padding: 8px 10px;
  outline: none;
  background: var(--c-bg);
}
textarea:focus { border-color: var(--c-primary); }
.send {
  align-self: flex-end;
  padding: 8px 22px;
  background: var(--c-primary);
  color: #fff;
}
.send:disabled { opacity: 0.45; cursor: not-allowed; }
</style>
