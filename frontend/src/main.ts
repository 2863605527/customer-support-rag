import { createApp } from "vue";
import App from "@/App.vue";
import { configureAuth, setUnauthorizedHandler } from "@/api/client";
import { clearSession, getAccessToken, refreshAccessToken, restoreSession } from "@/composables/useAuth";
import { router } from "@/router";
import "@/styles/main.css";
import "@/styles/auth.css";

// 注入令牌读写（放这里而不是 client 内部直接 import，避免 client ↔ useAuth 循环依赖）
configureAuth({ getToken: getAccessToken, refresh: refreshAccessToken });

// 令牌彻底失效（刷新也失败）→ 清空状态并回登录页；否则页面会停留在半死状态疯狂报错
setUnauthorizedHandler(() => {
  clearSession();
  void router.replace({ name: "login" });
});

// 刷新页面后先用 refresh_token 恢复登录态，再挂载，避免首屏闪烁到登录页
void restoreSession().finally(() => {
  createApp(App).use(router).mount("#app");
});
