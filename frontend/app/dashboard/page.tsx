"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Line, LineChart,
  ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import Guard from "@/components/Guard";
import { api } from "@/lib/api";
import { Asset, MARKETS, MarketKey, assetsOfMarket } from "@/lib/assets";
import { macd, rsi, sma } from "@/lib/indicators";
// Named PriceBar to avoid clashing with recharts' <Bar> chart primitive.
type PriceBar = { time: string; open: number; high: number; low: number; close: number; volume: number };

function DashboardInner() {
  const [assets, setAssets] = useState<Asset[]>([]);
  const [market, setMarket] = useState<MarketKey>("nasdaq");
  const [selected, setSelected] = useState<number | null>(null);
  const [bars, setBars] = useState<PriceBar[]>([]);
  const [loading, setLoading] = useState(false);

  const marketAssets = useMemo(() => assetsOfMarket(assets, market), [assets, market]);

  useEffect(() => {
    api<Asset[]>("/assets").then((a) => {
      setAssets(a);
      const first = assetsOfMarket(a, "nasdaq")[0] ?? a[0];
      if (first) setSelected(first.id);
    });
  }, []);

  function switchMarket(m: MarketKey) {
    setMarket(m);
    const first = assetsOfMarket(assets, m)[0];
    if (first) setSelected(first.id);
  }

  useEffect(() => {
    if (selected == null) return;
    setLoading(true);
    api<PriceBar[]>(`/assets/${selected}/bars?timeframe=1d&limit=300`)
      .then(setBars)
      .finally(() => setLoading(false));
  }, [selected]);

  const closes = useMemo(() => bars.map((b) => b.close), [bars]);
  const rsiVals = useMemo(() => rsi(closes, 14), [closes]);
  const macdVals = useMemo(() => macd(closes), [closes]);
  const sma20 = useMemo(() => sma(closes, 20), [closes]);
  const sma50 = useMemo(() => sma(closes, 50), [closes]);

  const chartData = useMemo(
    () => bars.map((b, i) => ({
      time: b.time.slice(0, 10),
      close: b.close,
      sma20: sma20[i], sma50: sma50[i],
      rsi: rsiVals[i], macd: macdVals.line[i],
      signal: macdVals.signal[i], hist: macdVals.hist[i],
    })),
    [bars, sma20, sma50, rsiVals, macdVals]
  );

  const last = bars[bars.length - 1];
  const prev = bars[bars.length - 2];
  const change = last && prev ? ((last.close - prev.close) / prev.close) * 100 : 0;
  const asset = assets.find((a) => a.id === selected);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Live Dashboard</h1>
          <p className="text-sm text-muted">Historical & real-time multi-asset prices with indicators</p>
        </div>
        {/* Market tabs: NASDAQ-100 / NIFTY 50 / Crypto */}
        <div className="segmented">
          {MARKETS.map((m) => {
            const n = assetsOfMarket(assets, m.key).length;
            return (
              <button key={m.key} onClick={() => switchMarket(m.key)}
                className={`px-4 py-2 text-xs transition-all active:scale-95 ${
                  market === m.key ? "bg-accent/20 text-accent shadow-glow-sm"
                                   : "text-muted hover:text-slate-200"}`}>
                <span className="font-medium">{m.label}</span>
                <span className="ml-1.5 opacity-60">{n}</span>
              </button>
            );
          })}
        </div>
      </div>

      {/* Every instrument of the active market — click to preview, open for the
          full interactive chart. */}
      <div className="card p-4">
        <div className="flex items-center justify-between mb-3">
          <p className="text-xs text-muted">
            {MARKETS.find((m) => m.key === market)!.label} ·{" "}
            {MARKETS.find((m) => m.key === market)!.sub} · {marketAssets.length} instruments
          </p>
          {selected != null && (
            <Link href={`/assets/${selected}`} className="btn-primary !py-1.5 text-xs">
              Open full chart: {assets.find((a) => a.id === selected)?.symbol} →
            </Link>
          )}
        </div>
        <div className="max-h-44 overflow-y-auto flex flex-wrap gap-1.5">
          {marketAssets.map((a) => (
            <button key={a.id} onClick={() => setSelected(a.id)}
              className={`px-2.5 py-1 rounded-lg font-mono text-xs transition-all active:scale-95 ${
                selected === a.id
                  ? "bg-accent/20 text-accent border border-accent/40 shadow-glow-sm"
                  : "bg-white/[0.04] border border-white/[0.08] text-slate-300 hover:border-accent/30 hover:text-slate-100"}`}>
              {a.symbol}
            </button>
          ))}
          {marketAssets.length === 0 && (
            <p className="text-muted text-sm py-4">
              No instruments yet — run <span className="font-mono">scripts/backfill_universe.py</span>.
            </p>
          )}
        </div>
      </div>

      {last && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <Stat label={`${asset?.symbol} Price`} value={`$${last.close.toFixed(2)}`} />
          <Stat label="Change (1d)" value={`${change >= 0 ? "+" : ""}${change.toFixed(2)}%`}
            tone={change >= 0 ? "up" : "down"} />
          <Stat label="RSI (14)" value={rsiVals[rsiVals.length - 1]?.toFixed(1) ?? "—"} />
          <Stat label="Volume" value={last.volume.toLocaleString()} />
        </div>
      )}

      <div className="card p-5">
        <h2 className="text-sm font-medium text-muted mb-3">Price · SMA 20 / 50</h2>
        {loading ? <Skeleton /> : (
          <ResponsiveContainer width="100%" height={320}>
            <LineChart data={chartData}>
              <CartesianGrid stroke="#1f2937" vertical={false} />
              <XAxis dataKey="time" tick={{ fill: "#64748b", fontSize: 11 }} minTickGap={40} />
              <YAxis domain={["auto", "auto"]} tick={{ fill: "#64748b", fontSize: 11 }} width={55} />
              <Tooltip contentStyle={TOOLTIP} />
              <Line type="monotone" dataKey="close" stroke="#22d3ee" dot={false} strokeWidth={1.8} />
              <Line type="monotone" dataKey="sma20" stroke="#eab308" dot={false} strokeWidth={1} />
              <Line type="monotone" dataKey="sma50" stroke="#a855f7" dot={false} strokeWidth={1} />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>

      <div className="grid md:grid-cols-2 gap-6">
        <div className="card p-5">
          <h2 className="text-sm font-medium text-muted mb-3">RSI (14)</h2>
          <ResponsiveContainer width="100%" height={180}>
            <AreaChart data={chartData}>
              <CartesianGrid stroke="#1f2937" vertical={false} />
              <XAxis dataKey="time" tick={{ fill: "#64748b", fontSize: 10 }} minTickGap={40} />
              <YAxis domain={[0, 100]} tick={{ fill: "#64748b", fontSize: 10 }} width={30} />
              <ReferenceLine y={70} stroke="#ef4444" strokeDasharray="3 3" />
              <ReferenceLine y={30} stroke="#22c55e" strokeDasharray="3 3" />
              <Area type="monotone" dataKey="rsi" stroke="#22d3ee" fill="#22d3ee22" strokeWidth={1.5} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
        <div className="card p-5">
          <h2 className="text-sm font-medium text-muted mb-3">MACD (12/26/9)</h2>
          <ResponsiveContainer width="100%" height={180}>
            <BarChart data={chartData}>
              <CartesianGrid stroke="#1f2937" vertical={false} />
              <XAxis dataKey="time" tick={{ fill: "#64748b", fontSize: 10 }} minTickGap={40} />
              <YAxis tick={{ fill: "#64748b", fontSize: 10 }} width={40} />
              <Tooltip contentStyle={TOOLTIP} />
              <Bar dataKey="hist" fill="#334155" />
              <Line type="monotone" dataKey="macd" stroke="#22d3ee" dot={false} />
              <Line type="monotone" dataKey="signal" stroke="#eab308" dot={false} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
    </div>
  );
}

const TOOLTIP = {
  background: "#111725", border: "1px solid #1f2937", borderRadius: 8, fontSize: 12,
};

function Stat({ label, value, tone }: { label: string; value: string; tone?: "up" | "down" }) {
  return (
    <div className="card p-4">
      <p className="text-xs text-muted mb-1">{label}</p>
      <p className={`stat ${tone === "up" ? "text-up" : tone === "down" ? "text-down" : ""}`}>
        {value}
      </p>
    </div>
  );
}

function Skeleton() {
  return <div className="skeleton h-[320px]" />;
}

export default function DashboardPage() {
  return <Guard><DashboardInner /></Guard>;
}
