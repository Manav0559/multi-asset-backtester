"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Line, LineChart,
  ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import Guard from "@/components/Guard";
import { api } from "@/lib/api";
import { macd, rsi, sma } from "@/lib/indicators";

type Asset = { id: number; symbol: string; exchange: string; asset_class: string };
// Named PriceBar to avoid clashing with recharts' <Bar> chart primitive.
type PriceBar = { time: string; open: number; high: number; low: number; close: number; volume: number };

function DashboardInner() {
  const [assets, setAssets] = useState<Asset[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [bars, setBars] = useState<PriceBar[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    api<Asset[]>("/assets").then((a) => {
      setAssets(a);
      if (a.length) setSelected(a[0].id);
    });
  }, []);

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
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Live Dashboard</h1>
          <p className="text-sm text-muted">Historical & real-time multi-asset prices with indicators</p>
        </div>
        <div className="flex items-center gap-3">
          <select className="input w-56" value={selected ?? ""}
            onChange={(e) => setSelected(Number(e.target.value))}>
            {assets.length === 0 && <option>No assets — run a backfill</option>}
            {assets.map((a) => (
              <option key={a.id} value={a.id}>
                {a.symbol} · {a.exchange}
              </option>
            ))}
          </select>
          {selected != null && (
            <Link href={`/assets/${selected}`}
              className="btn-primary whitespace-nowrap text-sm">
              Full chart →
            </Link>
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
