/** 鉴权状态：令牌存取、登录/注册/登出、自动刷新。

设计要点：
- **令牌存 localStorage**：本项目是内网工作台形态，localStorage 在刷新后仍可用；
  若将来要防 XSS 窃取，应改为 HttpOnly Cookie + CSRF 令牌（README 已注明取舍）。
- **"单飞"刷新**：并发请求同时收到 401 时，只发一次 refresh，其余请求等这一次结果，
  否则 N 个 401 会打 N 次刷新、并把彼此刚轮换出来的令牌作废（刷新即轮换的副作用）。
- 令牌过期前的主动刷新交给 axios 拦截器统一处理，业务代码不需要关心。
*/
import { computed, ref } from "vue";
import { authApi, humanizeError } from "@/api/client";
import type { TokenPair, UserPublic } from "@/types";

const ACCESS_KEY = "cs.access_token";
const REFRESH_KEY = "cs.refresh_token";
const USER_KEY = "cs.user";

const accessToken = ref<string | null>(localStorage.getItem(ACCESS_KEY));
const refreshToken = ref<string | null>(localStorage.getItem(REFRESH_KEY));
const currentUser = ref<UserPublic | null>(readStoredUser());

function readStoredUser(): UserPublic | null {
  const raw = localStorage.getItem(USER_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as UserPublic;
  } catch {
    return null;
  }
}

function persist(pair: TokenPair | null): void {
  accessToken.value = pair?.access_token ?? null;
  refreshToken.value = pair?.refresh_token ?? null;
  currentUser.value = pair?.user ?? null;

  if (pair) {
    localStorage.setItem(ACCESS_KEY, pair.access_token);
    localStorage.setItem(REFRESH_KEY, pair.refresh_token);
    localStorage.setItem(USER_KEY, JSON.stringify(pair.user));
  } else {
    localStorage.removeItem(ACCESS_KEY);
    localStorage.removeItem(REFRESH_KEY);
    localStorage.removeItem(USER_KEY);
  }
}

export function getAccessToken(): string | null {
  return accessToken.value;
}

/** 当前登录用户（同步可读：模块初始化时已从 localStorage 恢复）。 */
export function getCurrentUser(): UserPublic | null {
  return currentUser.value;
}

export function getRefreshToken(): string | null {
  return refreshToken.value;
}

let refreshInFlight: Promise<string | null> | null = null;

/** 刷新访问令牌；并发调用共享同一个请求（单飞）。 */
export async function refreshAccessToken(): Promise<string | null> {
  if (!refreshToken.value) return null;
  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      try {
        const pair = await authApi.refresh(refreshToken.value as string);
        persist(pair);
        return pair.access_token;
      } catch {
        persist(null);
        return null;
      } finally {
        refreshInFlight = null;
      }
    })();
  }
  return refreshInFlight;
}

async function login(email: string, password: string, captcha?: { id: string; code: string }): Promise<TokenPair> {
  const pair = await authApi.login({
    email,
    password,
    captcha_id: captcha?.id ?? null,
    captcha_code: captcha?.code ?? null,
  });
  persist(pair);
  return pair;
}

async function register(email: string, password: string, fullName: string): Promise<UserPublic> {
  return authApi.register({ email, password, full_name: fullName });
}

async function logout(): Promise<void> {
  try {
    await authApi.logout(refreshToken.value);
  } catch {
    // 后端不可达也要让本地登出成功（否则用户被困在登录态）
  } finally {
    persist(null);
  }
}

/** 启动时用 refresh_token 换取新令牌，恢复登录态并校验令牌是否仍有效。 */
export async function restoreSession(): Promise<boolean> {
  if (!accessToken.value && !refreshToken.value) return false;
  const token = await refreshAccessToken();
  if (!token) {
    persist(null);
    return false;
  }
  try {
    currentUser.value = await authApi.me();
    localStorage.setItem(USER_KEY, JSON.stringify(currentUser.value));
  } catch {
    persist(null);
    return false;
  }
  return true;
}

/** 清空本地登录态（令牌彻底失效时由拦截器/登出调用）。 */
export function clearSession(): void {
  persist(null);
}

export function useAuth() {
  return {
    currentUser: computed(() => currentUser.value),
    isAuthenticated: computed(() => Boolean(accessToken.value)),
    login,
    register,
    logout,
    restoreSession,
    clearSession,
    humanizeError,
  };
}
