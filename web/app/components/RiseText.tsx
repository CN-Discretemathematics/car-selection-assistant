import type { CSSProperties } from "react";

/**
 * Hero 标题逐字浮现（Apple Intelligence 式入场）：
 * 每个字符带「模糊 → 清晰、上浮 → 归位」动画，按序号交错延迟。
 * 纯 CSS 动画，服务端组件可用，无需 JS 水合。
 * gradient 模式下渐变按字符数展开定位，保证整句渐变连续。
 */
export default function RiseText({
  text,
  className = "",
  startDelay = 120,
  perChar = 38,
  gradient = false,
  as: Tag = "span",
}: {
  text: string;
  className?: string;
  startDelay?: number;
  perChar?: number;
  gradient?: boolean;
  as?: "span" | "h1" | "p";
}) {
  const chars = Array.from(text);
  return (
    <Tag className={className}>
      {/* 屏幕阅读器读完整文本；逐字动画 span 对辅助技术隐藏 */}
      <span className="sr-only">{text}</span>
      {chars.map((ch, i) => (
        <span
          key={`${ch}-${i}`}
          className={`hero-char ${gradient ? "text-gradient" : ""}`}
          aria-hidden="true"
          style={
            {
              animationDelay: `${startDelay + i * perChar}ms`,
              ...(gradient && chars.length > 1
                ? {
                    backgroundSize: `${chars.length * 100}% 100%`,
                    backgroundPosition: `${(i * 100) / (chars.length - 1)}% 0`,
                  }
                : {}),
            } as CSSProperties
          }
        >
          {ch === " " ? "\u00A0" : ch}
        </span>
      ))}
    </Tag>
  );
}
