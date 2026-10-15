<script setup lang="ts">
import { computed, ref } from "vue";
import { useRouter } from "vue-router";
import { humanizeError } from "@/api/client";
import { useAuth } from "@/composables/useAuth";

const { register } = useAuth();
const router = useRouter();

const email = ref("");
const fullName = ref("");
const password = ref("");
const confirm = ref("");
const error = ref("");
const loading = ref(false);

/** 与后端策略保持一致的**前端预校验**：只为即时反馈，最终判定仍在服务端。 */
const rules = computed(() => {
  const value = password.value;
  return [
    { label: "长度至少 10 位", pass: value.length >= 10 },
    { label: "同时包含字母和数字", pass: /[A-Za-z]/.test(value) && /\d/.test(value) },
    { label: "至少 4 个不同字符", pass: new Set(value).size >= 4 },
    { label: "不含邮箱账号名", pass: !email.value || !value.toLowerCase().includes(localPart.value) },
  ];
});

const localPart = computed(() => email.value.split("@")[0].trim().toLowerCase());

const strength = computed(() => {
  const all = rules.value.every((r) => r.pass);
  if (!password.value) return { level: 0, label: "未填写", color: "#c9ced6", width: "0%" };
  if (!all) return { level: 1, label: "不满足策略", color: "var(--c-err)", width: "33%" };
  if (password.value.length >= 14 && /[^A-Za-z0-9]/.test(password.value)) {
    return { level: 3, label: "强", color: "var(--c-ok)", width: "100%" };
  }
  return { level: 2, label: "合格", color: "#d99a2b", width: "66%" };
});

async function submit(): Promise<void> {
  error.value = "";
  if (!email.value.trim()) {
    error.value = "请输入企业邮箱";
    return;
  }
  if (!rules.value.every((r) => r.pass)) {
    error.value = "密码未满足安全要求，请按下方清单调整";
    return;
  }
  if (password.value !== confirm.value) {
    error.value = "两次输入的密码不一致";
    return;
  }
  loading.value = true;
  try {
    await register(email.value.trim(), password.value, fullName.value.trim());
    await router.replace({ name: "login", query: { registered: "1" } });
  } catch (err) {
    error.value = humanizeError(err);
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="auth-page">
    <div class="card">
      <header class="head">
        <h1>申请账号</h1>
        <p class="sub">注册后由客服团队使用；账号权限默认「客服」角色</p>
      </header>

      <form @submit.prevent="submit">
        <label class="field">
          <span>企业邮箱</span>
          <input v-model="email" type="email" autocomplete="username" placeholder="you@kurashi-bedding.jp" />
        </label>

        <label class="field">
          <span>姓名 / 昵称（可选）</span>
          <input v-model="fullName" maxlength="64" placeholder="用于工作台显示" />
        </label>

        <label class="field">
          <span>设置密码</span>
          <input v-model="password" type="password" autocomplete="new-password" placeholder="至少 10 位，含字母和数字" />
        </label>

        <div class="strength">
          <div class="bar"><i :style="{ width: strength.width, background: strength.color }" /></div>
          <span class="label">强度：{{ strength.label }}</span>
        </div>

        <ul class="policy">
          <li v-for="rule in rules" :key="rule.label" :class="{ pass: rule.pass }">{{ rule.label }}</li>
        </ul>

        <label class="field">
          <span>确认密码</span>
          <input v-model="confirm" type="password" autocomplete="new-password" placeholder="再输入一次" />
        </label>

        <p v-if="error" class="msg err">{{ error }}</p>

        <button class="primary" type="submit" :disabled="loading">
          {{ loading ? "提交中…" : "注册" }}
        </button>
      </form>

      <footer class="foot">
        已有账号？<RouterLink to="/login">返回登录</RouterLink>
      </footer>
    </div>
  </div>
</template>
