"use client";

import { useCallback, useEffect, useState } from "react";
import { deletePreference, listPreferences, readIdentityToken } from "@/lib/agent";
import type { PreferenceMemory } from "@/lib/api";
import { describePreference } from "@/lib/preferences";

/**
 * 「我记住的偏好」面板：让用户**看得见**被记住的东西，并能**逐条撤回**。
 *
 * ## 为什么需要这个入口（2026-10-21）
 *
 * 后端早就准备好了 `GET /agent/preferences` 与 `DELETE /agent/preferences/{id}`，
 * 但前端**没有入口** ⇒ 用户既看不到自己被记住了什么，也没法撤回。
 *
 * 而「记住了什么」正是这套能力里**最该给用户看**的东西：
 * 记忆会改变下一次推荐的**硬约束**（预算 / 排除品牌 / 不想要的车型），
 * 用户看不到就改不了 ⇒ 隐私上说不清、体验上不可控。
 *
 * ## 三条诚实性约束
 *
 * 1. **读不到 ≠ 没有**：读失败时显示「暂时读不到」，**不**显示成空列表
 *    （空列表会被读成「你什么也没被记住」，那是谎）。
 * 2. **不凭空创建身份**：列表用 `readIdentityToken()`（只读），
 *    不调 `ensureIdentityToken()` —— 只想看一眼的人，不该凭空多出一个身份。
 * 3. **撤回即时可见**：撤回成功后立刻从列表里去掉，不等下一次刷新。
 */
export default function MemoryPanel({ onBack }: { onBack: () => void }) {
  const [items, setItems] = useState<PreferenceMemory[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  // 身份是否还有效（2026-10-24）：后端对「没记住」和「身份已失效」都返回空列表，
  // 分不清 ⇒ 空列表时探一次。false = 这个浏览器的身份服务端已经不认识了
  // （服务端清过库 / 身份被删）⇒ 那不是「你什么也没被记住」，是**读不到**。
  const [identityValid, setIdentityValid] = useState(true);

  const load = useCallback(async () => {
    try {
      setError(null);
      const { items: loaded, identityValid: valid } = await listPreferences(readIdentityToken());
      setItems(loaded);
      setIdentityValid(valid);
    } catch (err) {
      setItems(null);
      setError(err instanceof Error ? err.message : "读取失败，请稍后再试");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function remove(id: number) {
    setBusyId(id);
    try {
      await deletePreference(id, readIdentityToken());
      setItems((prev) => (prev ?? []).filter((m) => m.id !== id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "撤回失败，请稍后再试");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="flex-1 overflow-y-auto bg-canvas/40 px-4 py-4">
      <div className="mb-3 flex items-center justify-between">
        <p className="text-[13px] font-semibold tracking-tight text-ink">我记住的偏好</p>
        <button
          type="button"
          onClick={onBack}
          className="press rounded-full bg-black/[0.06] px-2.5 py-1 text-[11px] font-medium text-ash hover:bg-black/10 hover:text-ink"
        >
          返回对话
        </button>
      </div>

      {error && (
        <div className="msg-pop mb-3 rounded-2xl border border-red-200/60 bg-red-50/90 p-2.5 text-xs text-red-600">
          {error}
        </div>
      )}

      {!error && items === null && (
        <p className="text-xs text-ash">正在读取…</p>
      )}

      {!error && items !== null && items.length === 0 && identityValid && (
        <div className="msg-pop rounded-[22px] border border-apple/12 bg-ice/70 p-3.5 text-[13px] leading-6 text-ink-soft">
          现在还没有记住任何偏好。
          <br />
          你在对话里说过的预算、用途、人数，或不想要的车型和品牌，会出现在这里。
        </div>
      )}

      {/* ⚠️ 「读不到」**不等于**「没记住」（2026-10-24 生产实测）：
          浏览器里存着一个已被服务端删除的身份 token ⇒ 后端返回空列表
          ⇒ 若照旧显示「你什么也没被记住」，那是在**撒谎**——
          真相是「读不到」。这里如实区分。 */}
      {!error && items !== null && items.length === 0 && !identityValid && (
        <div className="msg-pop rounded-[22px] border border-amber-200/60 bg-amber-50/90 p-3.5 text-[13px] leading-6 text-ink-soft">
          暂时读不到你之前被记住的偏好。
          <br />
          这个浏览器里的匿名标识已经失效了（服务端不再认识它），所以取不到内容。
          <br />
          继续问车即可——下一次对话会重新建立标识，之后说过的条件就会开始被记住。
        </div>
      )}

      {items !== null && items.length > 0 && (
        <>
          <ul className="space-y-2">
            {items.map((memory) => (
              <li
                key={memory.id}
                className="lift msg-pop flex items-start justify-between gap-3 rounded-[22px] border border-black/[0.06] bg-white/95 p-3 shadow-sm"
              >
                <p className="text-[13px] leading-6 text-ink">{describePreference(memory)}</p>
                <button
                  type="button"
                  onClick={() => void remove(memory.id)}
                  disabled={busyId === memory.id}
                  className="press shrink-0 rounded-full border border-apple/25 bg-white px-2.5 py-1 text-[11px] font-medium text-apple hover:bg-ice disabled:opacity-40"
                >
                  {busyId === memory.id ? "撤回中" : "不再记这条"}
                </button>
              </li>
            ))}
          </ul>
          <p className="mt-3 text-[11px] leading-5 text-ash">
            撤回后，下次提问就不会再带上这一条；当前这段对话里已经说过的条件不受影响。
          </p>
        </>
      )}
    </div>
  );
}
