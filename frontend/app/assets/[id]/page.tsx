"use client";

/**
 * Full-screen interactive chart for one asset: candles/line/bars modes,
 * timeframe + depth controls, and server-computed indicator overlays picked
 * from the 150+ entry IndicatorService catalog. Overlay math runs on the
 * backend — the exact engine backtests use — so what you see IS what the
 * backtester traded.
 */
import { useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import useSWR from "swr";
import Guard from "@/components/Guard";
import AssetChart, { Bar, ChartMode, IndicatorSeries } from "@/components/AssetChart";
import ProvenanceBadge from "@/components/ProvenanceBadge";
import { LiveOrderBook, VolumeProfile } from "@/components/OrderBook";
import { api, fetcher } from "@/lib/api";
import { ccySymbol } from "@/lib/format";
import { useLive } from "@/lib/live";

type Asset = { id: number; symbol: string; exchange: string; asset_class: string; currency?: string };
type CatalogEntry = { name: string; category: string; params: { name: string; default: number | null }[] };
type ActiveIndicator = { name: string; params: Record<string, number> };

const MODES: { key: ChartMode; label: string }[] = [
  { key: "candles", label: "Candles" },
  { key: "line", label: "Line" },
  { key: "bars", label: "Bars" },
];

// Enough decimals to show real movement at any magnitude.
function fmtPrice(n: number): string {
  const d = n >= 1000 ? 2 : n >= 1 ? 3 : n >= 0.01 ? 5 : 7;
  return n.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
}

// Curated "greatest hits" pinned to the top of the picker; the full catalog
// is grouped by category below them.
const FEATURED = ["sma", "ema", "rsi", "macd", "bbands", "supertrend", "vwap", "atr", "adx", "ichimoku"];

function specOf(active: ActiveIndicator[]): string {
  return active
    .map((a) => {
      const kv = Object.entries(a.params).map(([k, v]) => `${k}=${v}`).join(",");
      return kv ? `${a.name}:${kv}` : a.name;
    })
    .join(";");
}

function AssetPageInner() {
  const params = useParams<{ id: string }>();
  const assetId = Number(params.id);

  const [mode, setMode] = useState<ChartMode>("candles");
  const [timeframe, setTimeframe] = useState("1d");
  const [active, setActive] = useState<ActiveIndicator[]>([]);
  const [pickerValue, setPickerValue] = useState("");

  const { data: assets } = useSWR<Asset[]>("/assets", fetcher);
  // Only offer timeframes this asset actually has bars for (most equities are
  // 1d-only) — an empty chart made every indicator on it look broken.
  const { data: tfAvail } = useSWR<Record<string, { timeframe: string; bars: number }[]>>(
    Number.isFinite(assetId) ? `/assets/timeframes?ids=${assetId}` : null, fetcher,
    { revalidateOnFocus: false });
  const availableTfs = tfAvail?.[String(assetId)]?.map((t) => t.timeframe) ?? null;
  useEffect(() => {
    if (availableTfs && availableTfs.length && !availableTfs.includes(timeframe)) {
      setTimeframe(availableTfs[availableTfs.length - 1]); // coarsest available
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [availableTfs?.join(",")]);
  const asset = assets?.find((a) => a.id === assetId);
  const isCrypto = asset?.asset_class === "crypto";
  const live = useLive(assetId);

  const { data: bars } = useSWR<Bar[]>(
    `/assets/${assetId}/bars?timeframe=${timeframe}&limit=500`, fetcher,
    { refreshInterval: 5000 } // live-ish: streamed bars appear as they close
  );
  const { data: catalog } = useSWR<CatalogEntry[]>("/indicators", fetcher, {
    revalidateOnFocus: false,
  });

  const spec = specOf(active);
  const { data: indicatorData, error: indicatorError } = useSWR(
    spec ? `/assets/${assetId}/indicators?timeframe=${timeframe}&limit=500&spec=${encodeURIComponent(spec)}` : null,
    fetcher
  );

  const overlays: IndicatorSeries[] = useMemo(() => {
    if (!indicatorData) return [];
    return Object.entries(indicatorData.series as Record<string, (number | null)[]>)
      .map(([name, values]) => ({ name, times: indicatorData.time, values }));
  }, [indicatorData]);

  const grouped = useMemo(() => {
    const byCat: Record<string, CatalogEntry[]> = {};
    (catalog ?? []).forEach((c) => { (byCat[c.category] ??= []).push(c); });
    return byCat;
  }, [catalog]);

  function addIndicator(name: string) {
    const entry = catalog?.find((c) => c.name === name);
    if (!entry || active.some((a) => a.name === name)) return;
    const defaults: Record<string, number> = {};
    entry.params.forEach((p) => {
      if (typeof p.default === "number") defaults[p.name] = p.default;
    });
    setActive((prev) => [...prev, { name, params: defaults }]);
  }

  function updateParam(name: string, param: string, value: number) {
    setActive((prev) => prev.map((a) =>
      a.name === name ? { ...a, params: { ...a.params, [param]: value } } : a));
  }

  const last = bars?.[bars.length - 1];
  const prev = bars?.[bars.length - 2];
  const change = last && prev ? ((last.close - prev.close) / prev.close) * 100 : 0;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-baseline gap-3">
          <h1 className="text-xl font-semibold font-mono">{asset?.symbol ?? "…"}</h1>
          <span className="text-sm text-muted">{asset?.exchange} · {asset?.asset_class}</span>
          {/* Live price streams from ticks; falls back to the last bar close.
              Adaptive precision so sub-dollar assets (ADA, DOGE) show real
              movement, not a 2-decimal round-off. */}
          <span className="font-mono text-lg" data-testid="live-price">
            {ccySymbol(asset?.currency)}
            {live.price != null ? fmtPrice(Number(live.price))
              : last ? fmtPrice(last.close) : "…"}
          </span>
          {last && (
            <span className={`font-mono text-sm ${change >= 0 ? "text-up" : "text-down"}`}>
              {change >= 0 ? "+" : ""}{change.toFixed(2)}%
            </span>
          )}
          <ProvenanceBadge provenance={live.provenance}
            title={live.status?.label ? `market ${live.status.label}` : undefined} />
        </div>
        <Link href="/dashboard" className="text-sm text-muted hover:text-accent transition-colors">
          ← Dashboard
        </Link>
      </div>

      <div className="card p-4 space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          {/* mode toggle */}
          <div className="segmented">
            {MODES.map((m) => (
              <button key={m.key} onClick={() => setMode(m.key)}
                className={`px-3 py-1.5 text-xs transition-all active:scale-95 ${
                  mode === m.key ? "bg-accent/20 text-accent shadow-glow-sm" : "text-muted hover:text-slate-200"}`}>
                {m.label}
              </button>
            ))}
          </div>

          <select className="input w-28 !py-1.5 text-xs" value={timeframe}
            onChange={(e) => setTimeframe(e.target.value)}>
            {(availableTfs ?? ["1d"]).map((tf) => (
              <option key={tf} value={tf}>{tf === "1d" ? "1D" : tf}</option>
            ))}
          </select>

          {/* indicator picker — the whole IndicatorService catalog */}
          <select className="input w-64 !py-1.5 text-xs" value={pickerValue}
            onChange={(e) => { addIndicator(e.target.value); setPickerValue(""); }}>
            <option value="">+ Add indicator ({catalog?.length ?? "…"} available)</option>
            <optgroup label="★ Popular">
              {FEATURED.filter((f) => catalog?.some((c) => c.name === f)).map((f) => (
                <option key={f} value={f}>{f.toUpperCase()}</option>
              ))}
            </optgroup>
            {Object.entries(grouped).map(([cat, entries]) => (
              <optgroup key={cat} label={cat}>
                {entries.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
              </optgroup>
            ))}
          </select>
        </div>

        {/* active indicator chips with editable params */}
        {active.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {active.map((a) => (
              <span key={a.name}
                className="inline-flex items-center gap-1.5 rounded-full bg-panel2 border border-border px-3 py-1 text-xs">
                <span className="font-mono text-accent">{a.name.toUpperCase()}</span>
                {Object.entries(a.params).map(([k, v]) => (
                  <label key={k} className="inline-flex items-center gap-0.5 text-muted">
                    {k}
                    <input type="number" value={v}
                      onChange={(e) => updateParam(a.name, k, Number(e.target.value))}
                      className="w-12 bg-transparent border-b border-border text-slate-200 text-xs
                                 focus:outline-none focus:border-accent" />
                  </label>
                ))}
                <button onClick={() => setActive((p) => p.filter((x) => x.name !== a.name))}
                  className="text-muted hover:text-down ml-0.5">✕</button>
              </span>
            ))}
          </div>
        )}
        {indicatorError && (
          <p className="text-down text-xs">Indicator error: {String(indicatorError.message ?? indicatorError)}</p>
        )}

        {bars === undefined ? (
          <div className="skeleton h-[480px]" />
        ) : bars.length === 0 ? (
          <div className="h-[480px] flex flex-col items-center justify-center gap-2 text-center">
            <span className="text-3xl opacity-40">◈</span>
            <p className="text-sm text-slate-300">No {timeframe} bars for {asset?.symbol ?? "this asset"}</p>
            <p className="text-xs text-muted max-w-sm">
              Intraday history is backfilled for crypto and a core set of megacaps —
              try 1D, or run <span className="font-mono">scripts/backfill_universe.py</span> to extend coverage.
            </p>
          </div>
        ) : (
          <AssetChart bars={bars} mode={mode} indicators={overlays} />
        )}
      </div>

      {/* Market activity: live L2 book + tape for crypto; last-session volume
          profile for equities. Both carry a provenance badge. */}
      <div className="card p-4">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-medium">
            {isCrypto ? "Order book" : "Market depth"}
          </h2>
          <ProvenanceBadge provenance={live.provenance} />
        </div>
        {isCrypto
          ? <LiveOrderBook book={live.book} tape={live.tape} />
          : <VolumeProfile assetId={assetId} />}
        {!isCrypto && (
          <p className="text-[11px] text-muted mt-2">
            Equities have no live L2 feed here — this is real volume-at-price from the
            last stored session, not a live book.
          </p>
        )}
      </div>
    </div>
  );
}

export default function Page() {
  return <Guard><AssetPageInner /></Guard>;
}
