<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { authApi, errorContext, humanizeError } from "@/api/client";
import { useAuth } from "@/composables/useAuth";
import type { CaptchaChallenge } from "@/types";

const { login } = useAuth();
const router = useRouter();
const route = useRoute();

const email = ref("");
const password = ref("");
const captchaCode = ref("");
const captcha = ref<CaptchaChallenge | null>(null);
const captchaRequired = ref(false);
const error = ref("");
const hint = ref("");
const loading = ref(false);
const showPassword = ref(false);

async function loadCaptcha(): Promise<void> {
  captcha.value = await authApi.captcha();
  captchaCode.value = "";
}

async function submit(): Promise<void> {
  error.value = "";
  hint.value = "";
  if (!email.value.trim() || !password.value) {
    error.value = "请输入邮箱和密码";
    return;
  }
  loading.value = true;
  try {
    await login(
      email.value.trim(),
      password.value,
      captchaRequired.value && captcha.value
        ? { id: captcha.value.captcha_id, code: captchaCode.value }
        : undefined,
    );
    const redirect = typeof route.query.redirect === "string" ? route.query.redirect : "/";
    await router.replace(redirect);
  } catch (err) {
    error.value = humanizeError(err);
    const ctx = errorContext(err);
    // 后端把下一步动作放在 context 里，前端据此切换 UI，而不是去匹配文案
    if (ctx.captcha_required) {
      captchaRequired.value = true;
      await loadCaptcha();
    }
    if (typeof ctx.remaining_attempts === "number") {
      hint.value = `还可尝试 ${ctx.remaining_attempts} 次，连续失败会临时锁定账号`;
    }
    if (ctx.locked_seconds) {
      hint.value = `账号已锁定，约 ${Math.ceil(ctx.locked_seconds / 60)} 分钟后可再试`;
    }
    password.value = "";
  } finally {
    loading.value = false;
  }
}

onMounted(() => {
  if (route.query.registered === "1") {
    hint.value = "注册成功，请使用新账号登录";
  }
});
</script>

<template>
  <div class="auth-page">
    <div class="card">
      <header class="head">
        <h1>くらし寝具 智能客服</h1>
        <p class="sub">跨境店铺多语言客服工作台 · 请登录后使用</p>
      </header>

      <form @submit.prevent="submit">
        <label class="field">
          <span>企业邮箱</span>
          <input v-model="email" type="email" autocomplete="username" placeholder="agent@kurashi-bedding.jp" />
        </label>

        <label class="field">
          <span>密码</span>
          <div class="with-affix">
            <input
              v-model="password"
              :type="showPassword ? 'text' : 'password'"
              autocomplete="current-password"
              placeholder="请输入密码"
            />
            <button type="button" class="affix" @click="showPassword = !showPassword">
              {{ showPassword ? "隐藏" : "显示" }}
            </button>
          </div>
        </label>

        <label v-if="captchaRequired" class="field">
          <span>图形验证码（连续失败后必需）</span>
          <div class="captcha-row">
            <input v-model="captchaCode" maxlength="6" placeholder="输入右侧字符" />
            <button type="button" class="captcha" :title="'点击刷新'" @click="loadCaptcha">
              <span v-if="captcha" v-html="captcha.image_svg" />
            </button>
          </div>
        </label>

        <p v-if="error" class="msg err">{{ error }}</p>
        <p v-if="hint" class="msg hint">{{ hint }}</p>

        <button class="primary" type="submit" :disabled="loading">
          {{ loading ? "登录中…" : "登录" }}
        </button>
      </form>

      <footer class="foot">
        还没有账号？<RouterLink to="/register">申请注册</RouterLink>
        <span class="dot">·</span>
        <span class="dim">初始管理员账号见后端启动日志</span>
      </footer>
    </div>
  </div>
</template>
