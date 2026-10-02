import type { FavoriteOut, UserOut } from "@/lib/api";
import { postJson, requestJson } from "@/lib/http";

const TOKEN_KEY = "auth_token";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  // 读取也要 try/catch：Safari 无痕模式 / 禁用 Cookie / 存储配额异常时
  // `getItem` 会**抛异常**。此前只有 SSR 守卫没有容错，页面在 render 期读它，
  // 一抛就整页渲染失败。写法对齐 CompareBar 的 readCompareIds。
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null) {
  if (token) window.localStorage.setItem(TOKEN_KEY, token);
  else window.localStorage.removeItem(TOKEN_KEY);
}

const post = postJson;
const request = requestJson;

export interface AuthResult {
  token: string;
  user: UserOut;
  dev_code: string | null;
}

export function requestCode(email: string, isRegister: boolean): Promise<AuthResult> {
  return post(`/api/v1/auth/${isRegister ? "register" : "login"}`, { email });
}

export function verifyCode(email: string, code: string): Promise<AuthResult> {
  return post("/api/v1/auth/verify-code", { email, code });
}

export function listFavorites(token: string): Promise<FavoriteOut[]> {
  return request<FavoriteOut[]>("/api/v1/me/favorites", {
    headers: { Authorization: `Bearer ${token}` },
  });
}

export function addFavorite(token: string, vehicleId: number, kind: "series" | "variant"): Promise<FavoriteOut> {
  return request<FavoriteOut>(`/api/v1/me/favorites/${vehicleId}?kind=${kind}`, {
    method: "PUT",
    headers: { Authorization: `Bearer ${token}` },
  });
}

export function removeFavorite(token: string, vehicleId: number, kind: "series" | "variant"): Promise<{ status: string }> {
  return request<{ status: string }>(`/api/v1/me/favorites/${vehicleId}?kind=${kind}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${token}` },
  });
}

export function deleteAccount(token: string): Promise<{ status: string }> {
  return request<{ status: string }>("/api/v1/me", {
    method: "DELETE",
    headers: { Authorization: `Bearer ${token}` },
  });
}
