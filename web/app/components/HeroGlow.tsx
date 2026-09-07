/**
 * Hero 极光氛围背景（AI 建站式）：三团缓慢漂移的高斯模糊光斑。
 * 纯装饰，pointer-events-none；prefers-reduced-motion 时动画自动停止（全局 CSS 兜底）。
 */
export default function HeroGlow({ className = "" }: { className?: string }) {
  return (
    <div className={`pointer-events-none absolute inset-0 overflow-hidden ${className}`} aria-hidden="true">
      <div className="aurora-blob animate-aurora-a -top-24 left-[10%] h-72 w-72 bg-[#0a84ff]/22 sm:h-96 sm:w-96" />
      <div className="aurora-blob animate-aurora-b -top-16 right-[6%] h-80 w-80 bg-[#5e5ce6]/18 sm:h-[26rem] sm:w-[26rem]" />
      <div className="aurora-blob animate-aurora-c top-8 left-[42%] h-64 w-64 bg-[#64d2ff]/20 sm:h-80 sm:w-80" />
    </div>
  );
}
