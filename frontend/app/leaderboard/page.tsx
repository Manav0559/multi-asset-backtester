"use client";

import { useState } from "react";
import useSWR from "swr";
import Link from "next/link";
import Guard from "@/components/Guard";
import ProvenanceBadge from "@/components/ProvenanceBadge";
import { EmptyState, SkeletonRows } from "@/components/ui";
import { fetcher } from "@/lib/api";

type Entry = {
  rank: number;
  portfolio_id: string;
  name: string;
  members: string[];
  initial_cash: string;
  equity: string;
  return_pct: string;
  spark: string[];
};

// Tiny inline sparkline — a polyline is far cheaper than a recharts instance
// per table row, and the leaderboard re-polls every few seconds.
function Spark({ values, up }: { values: string[]; up: boolean }) {
  if (values.length < 2) return <span className="text-muted text-xs">—</span>;
  const nums = values.map(Number);
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const span = max - min || 1;
  const W = 96, H = 28, PAD = 2;
  const pts = nums
    .map((v, i) => {
      const x = PAD + (i * (W - 2 * PAD)) / (nums.length - 1);
      const y = H - PAD - ((v - min) * (H - 2 * PAD)) / span;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg width={W} height={H} className={up ? "text-up" : "text-down"}>
      <polyline points={pts} fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}

const WINDOWS = [
  { key: "24h", label: "24H" },
  { key: "7d", label: "7D" },
  { key: "all", label: "All time" },
] as const;
type Window = (typeof WINDOWS)[number]["key"];

function Leaderboard() {
  const [win, setWin] = useState<Window>("all");
  const { data, error, isLoading } = useSWR<Entry[]>(
    `/leaderboard?limit=50&window=${win}`, fetcher, {
      refreshInterval: 5000, // live-ish: rankings move as collaborators trade
      keepPreviousData: true, // no table flash when flipping windows
    });

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Leaderboard</h1>
          <p className="text-sm text-muted flex items-center gap-2 flex-wrap">
            Public portfolios ranked by {win === "all" ? "total" : win} return ·
            equity = cash + positions
            <ProvenanceBadge provenance="last_session" label="MARKED AT LAST CLOSE"
              title="Positions valued at the latest stored close — not a live mark" />
          </p>
        </div>
        <div className="segmented">
          {WINDOWS.map((w) => (
            <button key={w.key} onClick={() => setWin(w.key)}
              className={`px-3 py-1.5 text-xs transition-all active:scale-95 ${
                win === w.key ? "bg-accent/20 text-accent shadow-glow-sm" : "text-muted hover:text-slate-200"}`}>
              {w.label}
            </button>
          ))}
        </div>
      </div>

      <div className="card p-5">
        {error ? (
          <EmptyState icon="⚠" title="Failed to load leaderboard" hint={error.message} />
        ) : isLoading ? (
          <SkeletonRows rows={6} />
        ) : !data || data.length === 0 ? (
          <EmptyState icon="🏆" title="No public portfolios yet"
            hint="Create a portfolio with “public” enabled to compete on the board." />
        ) : (
          <table className="w-full text-sm">
            <thead className="text-muted text-xs">
              <tr className="text-left">
                <th className="pb-2 w-12">#</th>
                <th className="pb-2">Portfolio</th>
                <th className="pb-2">Members</th>
                <th className="pb-2 text-right">Equity</th>
                <th className="pb-2 text-right">Return</th>
                <th className="pb-2 text-right w-28">Curve</th>
              </tr>
            </thead>
            <tbody>
              {data.map((e) => {
                const ret = Number(e.return_pct);
                const up = ret >= 0;
                return (
                  <tr key={e.portfolio_id} className="border-t border-border">
                    <td className="py-2.5 font-mono text-muted">
                      {e.rank <= 3 ? ["🥇", "🥈", "🥉"][e.rank - 1] : e.rank}
                    </td>
                    <td className="py-2.5">
                      <Link href={`/portfolios/${e.portfolio_id}`}
                        className="hover:text-accent transition-colors">
                        {e.name}
                      </Link>
                    </td>
                    <td className="py-2.5 text-slate-400">{e.members.join(", ")}</td>
                    <td className="py-2.5 text-right font-mono">
                      ${Number(e.equity).toLocaleString(undefined, { minimumFractionDigits: 2 })}
                    </td>
                    <td className={`py-2.5 text-right font-mono ${up ? "text-up" : "text-down"}`}>
                      {up ? "+" : ""}{ret.toFixed(2)}%
                    </td>
                    <td className="py-2.5">
                      <div className="flex justify-end">
                        <Spark values={e.spark} up={up} />
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

export default function Page() {
  return <Guard><Leaderboard /></Guard>;
}
