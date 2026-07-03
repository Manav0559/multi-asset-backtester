"use client";

/**
 * Small shared UI primitives for the liquid-glass system: shimmer skeletons
 * and empty states. Motion-heavy pieces live in template.tsx / ToastProvider /
 * Nav; these stay CSS-driven so tables and grids can use them freely.
 */

export function Skeleton({ className = "h-6 w-full" }: { className?: string }) {
  return <div className={`skeleton ${className}`} aria-hidden />;
}

export function SkeletonRows({ rows = 5 }: { rows?: number }) {
  return (
    <div className="space-y-2.5 py-1">
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className={`h-8 ${i % 2 ? "w-[97%]" : "w-full"}`} />
      ))}
    </div>
  );
}

export function EmptyState({ icon = "◈", title, hint }: {
  icon?: string; title: string; hint?: string;
}) {
  return (
    <div className="empty-state">
      <span className="icon">{icon}</span>
      <p className="text-sm text-slate-300">{title}</p>
      {hint && <p className="text-xs text-muted max-w-xs">{hint}</p>}
    </div>
  );
}
