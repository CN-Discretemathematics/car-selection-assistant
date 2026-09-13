"use client";

import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import type { FilterOption } from "@/lib/filterOptions";

interface FilterSelectProps {
  /** 无障碍名称，如「能源类型」。 */
  label: string;
  value: string;
  options: FilterOption[];
  onChange: (value: string) => void;
  className?: string;
}

/**
 * 筛选栏下拉（自定义渲染，替代原生 select）。
 *
 * 原生 select 的弹出列表由操作系统绘制，圆角与配色都无法与本站 Liquid Glass 主题统一，
 * 而且聚焦时会被浏览器默认样式接管。这里用按钮 + 浮层自己实现，形状/配色/动效与页面一致，
 * 同时保留键盘操作（↑↓ 移动、Enter/Space 选中、Esc 关闭、Home/End 首尾）与 ARIA 语义。
 */
export default function FilterSelect({
  label,
  value,
  options,
  onChange,
  className = "",
}: FilterSelectProps) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(() => Math.max(options.findIndex((o) => o.value === value), 0));
  const listId = useId();
  const boxRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);

  const selected = options.find((o) => o.value === value) ?? options[0];

  // 打开时把高亮定位到当前选项
  useEffect(() => {
    if (open) setActive(Math.max(options.findIndex((o) => o.value === value), 0));
  }, [open, options, value]);

  // 点击外部关闭
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  function commit(index: number) {
    const option = options[index];
    setOpen(false);
    buttonRef.current?.focus();
    if (option && option.value !== value) onChange(option.value);
  }

  function onKeyDown(e: KeyboardEvent<HTMLButtonElement>) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp" || e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (!open) {
        setOpen(true);
        return;
      }
      if (e.key === "ArrowDown") setActive((i) => (i + 1) % options.length);
      else if (e.key === "ArrowUp") setActive((i) => (i <= 0 ? options.length - 1 : i - 1));
      else commit(active);
    } else if (e.key === "Home") {
      e.preventDefault();
      setActive(0);
    } else if (e.key === "End") {
      e.preventDefault();
      setActive(options.length - 1);
    } else if (e.key === "Escape") {
      if (open) {
        e.preventDefault();
        setOpen(false);
      }
    } else if (e.key === "Tab") {
      setOpen(false);
    }
  }

  const triggerCls =
    "flex h-9 items-center gap-1.5 whitespace-nowrap rounded-full border border-black/[0.08] " +
    "bg-white/85 px-3.5 text-[13px] text-ink-soft shadow-sm transition-all duration-300 " +
    "ease-[cubic-bezier(0.25,0.1,0.25,1)] hover:border-apple/35 hover:bg-white " +
    "focus:border-apple focus:bg-white focus:outline-none focus:ring-4 focus:ring-apple/12";

  return (
    <div ref={boxRef} className={`relative ${className}`}>
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        onKeyDown={onKeyDown}
        aria-label={label}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        className={`${triggerCls} ${open ? "border-apple/60 bg-white ring-4 ring-apple/12" : ""}`}
      >
        <span className={selected && selected.value ? "text-ink" : undefined}>{selected?.label}</span>
        <svg
          viewBox="0 0 20 20"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          aria-hidden
          className={`h-3.5 w-3.5 text-ash transition-transform duration-300 ${open ? "rotate-180" : ""}`}
        >
          <path d="M6 8l4 4 4-4" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>

      {open && (
        <div
          id={listId}
          role="listbox"
          aria-label={label}
          className="animate-scale-in absolute left-0 top-[calc(100%+0.4rem)] z-50 max-h-[320px] min-w-full origin-top overflow-y-auto rounded-[20px] border border-black/[0.08] bg-white p-1.5 shadow-[0_22px_50px_-14px_rgba(0,0,0,0.32)]"
        >
          {options.map((option, i) => {
            const isSelected = option.value === value;
            return (
              <button
                key={option.value || "__all__"}
                type="button"
                role="option"
                aria-selected={isSelected}
                onMouseEnter={() => setActive(i)}
                onClick={() => commit(i)}
                className={`flex w-full items-center justify-between gap-4 whitespace-nowrap rounded-[14px] px-3 py-2 text-left text-[13px] transition-colors duration-200 ${
                  i === active ? "bg-ice" : ""
                } ${isSelected ? "font-medium text-apple" : "text-ink-soft"}`}
              >
                {option.label}
                {isSelected && (
                  <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="2.2" aria-hidden className="h-3.5 w-3.5">
                    <path d="M4.5 10.5l3.5 3.5 7-7.5" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
