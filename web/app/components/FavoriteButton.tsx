"use client";

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import {
  addFavorite,
  getToken,
  listFavorites,
  removeFavorite,
  requestCode,
  setToken,
  verifyCode,
} from "@/lib/auth";

/**
 * 收藏按钮：收藏需要登录；
 * 未登录时先走最小注册/登录流程（邮箱 + 验证码），只收集邮箱，不收集其他信息。
 * 登录成功后立即执行本次收藏。
 */
export default function FavoriteButton({
  vehicleId,
  kind,
}: {
  vehicleId: number;
  kind: "series" | "variant";
}) {
  const [token, setTokenState] = useState<string | null>(null);
  const [faved, setFaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showLogin, setShowLogin] = useState(false);

  useEffect(() => {
    const t = getToken();
    setTokenState(t);
    if (!t) return;
    // 初始状态与服务端一致（刷新后仍显示「已收藏」）
    listFavorites(t)
      .then((items) => {
        if (items.some((item) => item.vehicle_id === vehicleId && item.kind === kind)) {
          setFaved(true);
        }
      })
      .catch(() => {});
  }, [vehicleId, kind]);

  async function toggle() {
    setError(null);
    if (!token) {
      setShowLogin(true);
      return;
    }
    setBusy(true);
    try {
      if (faved) {
        await removeFavorite(token, vehicleId, kind);
        setFaved(false);
      } else {
        await addFavorite(token, vehicleId, kind);
        setFaved(true);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }

  async function onLoggedIn(newToken: string) {
    setTokenState(newToken);
    setShowLogin(false);
    setBusy(true);
    setError(null);
    try {
      // 登录成功后立即执行本次收藏（首次收藏流程闭环）
      await addFavorite(newToken, vehicleId, kind);
      setFaved(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "收藏失败，请重试");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button
        type="button"
        onClick={toggle}
        disabled={busy}
        aria-pressed={faved}
        className={`press inline-flex items-center gap-1.5 rounded-full border px-3.5 py-1.5 text-[13px] font-medium shadow-sm disabled:opacity-50 ${
          faved
            ? "border-[#ff9f0a]/40 bg-[#fdf6ec] text-[#b25e09]"
            : "border-black/[0.08] bg-white/85 text-ink-soft hover:border-[#ff9f0a]/45 hover:text-[#b25e09]"
        }`}
      >
        <svg
          key={String(faved)}
          viewBox="0 0 24 24"
          className={`msg-pop h-4 w-4 ${faved ? "fill-[#ff9f0a] stroke-[#ff9f0a]" : "fill-none stroke-current"}`}
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M12 20.5S4 14.9 4 9.7A4.7 4.7 0 0 1 8.7 5c1.6 0 2.7.8 3.3 1.7C12.6 5.8 13.7 5 15.3 5A4.7 4.7 0 0 1 20 9.7c0 5.2-8 10.8-8 10.8Z" />
        </svg>
        {busy ? "…" : faved ? "已收藏" : "收藏"}
      </button>
      {error && <span className="ml-2 text-xs text-red-600">{error}</span>}
      {showLogin && <LoginModal onClose={() => setShowLogin(false)} onLoggedIn={onLoggedIn} />}
    </>
  );
}

function LoginModal({
  onClose,
  onLoggedIn,
}: {
  onClose: () => void;
  onLoggedIn: (token: string) => void;
}) {
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [step, setStep] = useState<"email" | "code">("email");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function sendCode() {
    if (!email.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await requestCode(email.trim(), true); // 注册即登录（未注册自动创建）
      setStep("code");
    } catch (err) {
      setError(err instanceof Error ? err.message : "发送失败");
    } finally {
      setBusy(false);
    }
  }

  async function submitCode() {
    if (code.length !== 6) return;
    setBusy(true);
    setError(null);
    try {
      const result = await verifyCode(email.trim(), code);
      setToken(result.token);
      onLoggedIn(result.token);
    } catch (err) {
      setError(err instanceof Error ? err.message : "验证失败");
    } finally {
      setBusy(false);
    }
  }

  const inputCls =
    "w-full rounded-2xl border border-transparent bg-canvas px-4 py-3 text-sm text-ink transition-all duration-300 placeholder:text-ash/70 focus:border-apple/50 focus:bg-white focus:outline-none focus:ring-4 focus:ring-apple/12";

  if (typeof document === "undefined") return null;
  return createPortal(
    <div
      className="animate-fade-in fixed inset-0 z-50 flex items-center justify-center bg-black/35 p-4 backdrop-blur-sm"
      onClick={onClose}
      role="presentation"
    >
      <div
        className="glass-strong animate-scale-in w-full max-w-sm rounded-[28px] border border-black/[0.07] p-6"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="登录后收藏"
      >
        <div className="flex items-start justify-between">
          <div>
            <h3 className="text-[17px] font-semibold tracking-tight text-ink">登录后收藏</h3>
            <p className="mt-1.5 text-xs leading-5 text-ash">
              只需邮箱验证码，无需密码；我们只保存邮箱，不收集其他信息（隐私政策见页面底部）。
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="press ml-3 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-black/[0.06] text-xs text-ash hover:bg-black/10 hover:text-ink"
          >
            ✕
          </button>
        </div>
        {step === "email" ? (
          <div className="mt-5 space-y-3">
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="你的邮箱"
              className={inputCls}
            />
            <button
              type="button"
              onClick={sendCode}
              disabled={busy || !email.trim()}
              className="press w-full rounded-2xl bg-apple py-3 text-sm font-medium text-white shadow-md shadow-apple/25 hover:bg-[#0077ed] disabled:opacity-40 disabled:shadow-none"
            >
              获取验证码
            </button>
          </div>
        ) : (
          <div className="mt-5 space-y-3">
            <input
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
              placeholder="6 位验证码"
              inputMode="numeric"
              className={`${inputCls} text-center text-lg tracking-[0.5em]`}
            />
            <button
              type="button"
              onClick={submitCode}
              disabled={busy || code.length !== 6}
              className="press w-full rounded-2xl bg-apple py-3 text-sm font-medium text-white shadow-md shadow-apple/25 hover:bg-[#0077ed] disabled:opacity-40 disabled:shadow-none"
            >
              验证并登录
            </button>
            <button
              type="button"
              onClick={() => setStep("email")}
              className="press w-full rounded-full py-1 text-xs text-ash hover:text-ink"
            >
              更换邮箱
            </button>
          </div>
        )}
        {error && <p className="mt-3 rounded-xl bg-red-50 px-3 py-2 text-xs text-red-600">{error}</p>}
      </div>
    </div>,
    document.body,
  );
}
