"use client";

// Market-activity panel. Crypto: a live L2 ladder (bids/asks with cumulative
// depth bars) + spread/mid + a streaming trade tape. Equity: a last-session
// volume-at-price profile (badged LAST SESSION, never a live book).
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Book, Trade } from "@/lib/live";

function num(s: string) { return Number(s); }

// Adaptive precision so sub-dollar books (ADA/DOGE) aren't flattened to 2dp.
function fmt(n: number): string {
  const d = n >= 1000 ? 2 : n >= 1 ? 3 : n >= 0.01 ? 5 : 7;
  return n.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
}

function Ladder({ levels, side, max }: {
  levels: [string, string][]; side: "bid" | "ask"; max: number;
}) {
  const color = side === "bid" ? "text-up" : "text-down";
  const bar = side === "bid" ? "bg-up/10" : "bg-down/10";
  const rows = side === "ask" ? [...levels].reverse() : levels;
  return (
    <div className="space-y-0.5">
      {rows.slice(0, 12).map(([price, size], i) => {
        const pct = Math.min((num(size) / max) * 100, 100);
        return (
          <div key={i} className="relative flex justify-between px-2 py-0.5 text-xs font-mono">
            <div className={`absolute inset-y-0 ${side === "bid" ? "right-0" : "left-0"} ${bar}`}
              style={{ width: `${pct}%` }} />
            <span className={`relative ${color}`}>{fmt(num(price))}</span>
            <span className="relative text-slate-400">{num(size).toFixed(4)}</span>
          </div>
        );
      })}
    </div>
  );
}

export function LiveOrderBook({ book, tape }: { book: Book | null; tape: Trade[] }) {
  if (!book || (!book.bids?.length && !book.asks?.length)) {
    return <div className="skeleton h-64" data-testid="orderbook-loading" />;
  }
  const bestBid = book.bids[0] ? num(book.bids[0][0]) : 0;
  const bestAsk = book.asks[0] ? num(book.asks[0][0]) : 0;
  const spread = bestAsk && bestBid ? bestAsk - bestBid : 0;
  const mid = bestAsk && bestBid ? (bestAsk + bestBid) / 2 : 0;
  const max = Math.max(
    ...book.bids.slice(0, 12).map((l) => num(l[1])),
    ...book.asks.slice(0, 12).map((l) => num(l[1])), 1);

  return (
    <div className="grid grid-cols-2 gap-4" data-testid="orderbook">
      <div>
        <p className="text-[10px] uppercase tracking-wider text-muted mb-1 px-2">Bids</p>
        <Ladder levels={book.bids} side="bid" max={max} />
      </div>
      <div>
        <p className="text-[10px] uppercase tracking-wider text-muted mb-1 px-2">Asks</p>
        <Ladder levels={book.asks} side="ask" max={max} />
      </div>
      <div className="col-span-2 flex items-center justify-center gap-6 py-2 border-y border-border text-xs">
        <span className="text-muted">spread <span className="font-mono text-slate-200">{spread.toFixed(2)}</span></span>
        <span className="text-muted">mid <span className="font-mono text-accent">{fmt(mid)}</span></span>
      </div>
      <div className="col-span-2">
        <p className="text-[10px] uppercase tracking-wider text-muted mb-1 px-2">Tape</p>
        <div className="max-h-28 overflow-y-auto space-y-0.5">
          {tape.map((t, i) => (
            <div key={i} className="flex justify-between px-2 text-xs font-mono">
              <span className="text-slate-300">{fmt(num(t.price))}</span>
              <span className="text-muted">{num(t.qty).toFixed(4)}</span>
            </div>
          ))}
          {tape.length === 0 && <p className="text-muted text-xs px-2 py-2">waiting for trades…</p>}
        </div>
      </div>
    </div>
  );
}

export function VolumeProfile({ assetId }: { assetId: number }) {
  const [data, setData] = useState<{ levels: { price: number; volume: number }[]; session_date: string } | null>(null);
  useEffect(() => {
    api<{ levels: { price: number; volume: number }[]; session_date: string }>(
      `/market/${assetId}/volume-profile`).then(setData).catch(() => setData(null));
  }, [assetId]);

  if (!data) return <div className="skeleton h-64" />;
  const max = Math.max(...data.levels.map((l) => l.volume), 1);
  return (
    <div data-testid="volume-profile">
      <p className="text-xs text-muted mb-2">
        Volume at price · last session ({data.session_date})
      </p>
      <div className="space-y-0.5">
        {data.levels.map((l, i) => (
          <div key={i} className="relative flex justify-between px-2 py-0.5 text-xs font-mono">
            <div className="absolute inset-y-0 left-0 bg-accent/10" style={{ width: `${(l.volume / max) * 100}%` }} />
            <span className="relative text-slate-300">{fmt(l.price)}</span>
            <span className="relative text-muted">{l.volume.toLocaleString()}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
