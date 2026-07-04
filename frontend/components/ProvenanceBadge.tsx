"use client";

// Honest provenance badge for every price surface. LIVE = real Binance WS;
// DELAYED = yfinance equity (vendor-delayed ~15 min); LAST SESSION = a
// reconstructed profile from stored bars, not a live book.
import { Provenance } from "@/lib/live";

const STYLE: Record<Provenance, { label: string; cls: string; dot?: boolean }> = {
  live: { label: "LIVE", cls: "bg-up/15 text-up border-up/30", dot: true },
  delayed: { label: "DELAYED ~15m", cls: "bg-amber-500/15 text-amber-300 border-amber-500/30" },
  last_session: { label: "LAST SESSION", cls: "bg-slate-500/15 text-slate-300 border-slate-500/30" },
  unknown: { label: "—", cls: "bg-slate-600/20 text-muted border-border" },
};

export default function ProvenanceBadge({ provenance, title }: {
  provenance: Provenance; title?: string;
}) {
  const s = STYLE[provenance] ?? STYLE.unknown;
  return (
    <span title={title}
      className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full border text-[10px]
                  font-medium tracking-wide ${s.cls}`}>
      {s.dot && <span className="h-1.5 w-1.5 rounded-full bg-up animate-pulse" />}
      {s.label}
    </span>
  );
}
