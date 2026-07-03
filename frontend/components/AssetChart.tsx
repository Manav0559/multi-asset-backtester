"use client";

/**
 * Interactive price chart on TradingView's lightweight-charts (v5).
 *
 * - Candles / Line / Bars display modes on the main pane.
 * - Indicator overlays: series whose scale is commensurate with price
 *   (SMA/EMA/BBands/…) render ON the price pane; oscillators (RSI/MACD/…)
 *   get their own pane below — decided data-driven by comparing medians,
 *   so it works for all 150+ backend indicators without a hand-kept list.
 */
import { useEffect, useRef } from "react";
import {
  BarSeries,
  CandlestickSeries,
  ColorType,
  createChart,
  IChartApi,
  LineSeries,
  UTCTimestamp,
} from "lightweight-charts";

export type Bar = { time: string; open: number; high: number; low: number; close: number; volume: number };
export type ChartMode = "candles" | "line" | "bars";
export type IndicatorSeries = { name: string; times: string[]; values: (number | null)[] };

const UP = "#22c55e";
const DOWN = "#ef4444";
const PALETTE = ["#22d3ee", "#eab308", "#a855f7", "#f97316", "#34d399", "#f472b6", "#818cf8", "#facc15"];

const ts = (iso: string) => Math.floor(Date.parse(iso) / 1000) as UTCTimestamp;

function median(xs: number[]): number {
  if (!xs.length) return 0;
  const s = [...xs].sort((a, b) => a - b);
  return s[Math.floor(s.length / 2)];
}

export default function AssetChart({
  bars, mode, indicators,
}: { bars: Bar[]; mode: ChartMode; indicators: IndicatorSeries[] }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);

  useEffect(() => {
    const el = containerRef.current;
    if (!el || bars.length === 0) return;

    const chart = createChart(el, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#64748b",
        fontSize: 11,
        panes: { separatorColor: "#1f2937", enableResize: true },
      },
      grid: {
        vertLines: { color: "#16202f" },
        horzLines: { color: "#16202f" },
      },
      rightPriceScale: { borderColor: "#1f2937" },
      timeScale: { borderColor: "#1f2937", timeVisible: false },
      crosshair: { horzLine: { labelBackgroundColor: "#334155" }, vertLine: { labelBackgroundColor: "#334155" } },
    });
    chartRef.current = chart;

    // ---- main price series (pane 0) --------------------------------------
    if (mode === "line") {
      const s = chart.addSeries(LineSeries, { color: "#22d3ee", lineWidth: 2 });
      s.setData(bars.map((b) => ({ time: ts(b.time), value: b.close })));
    } else {
      const s = chart.addSeries(mode === "candles" ? CandlestickSeries : BarSeries, {
        upColor: UP, downColor: DOWN,
        ...(mode === "candles"
          ? { borderUpColor: UP, borderDownColor: DOWN, wickUpColor: UP, wickDownColor: DOWN }
          : {}),
      } as any);
      s.setData(bars.map((b) => ({
        time: ts(b.time), open: b.open, high: b.high, low: b.low, close: b.close,
      })));
    }

    // ---- indicator overlays ----------------------------------------------
    const priceMedian = median(bars.map((b) => b.close));
    let oscillatorPane = 0; // assigned lazily so the pane only exists if needed
    indicators.forEach((ind, i) => {
      const data = ind.times
        .map((t, j) => ({ time: ts(t), value: ind.values[j] }))
        .filter((p): p is { time: UTCTimestamp; value: number } => p.value != null);
      if (!data.length) return;

      const m = median(data.map((p) => Math.abs(p.value)));
      const onPricePane = priceMedian > 0 && m > priceMedian * 0.3 && m < priceMedian * 3;
      if (!onPricePane && oscillatorPane === 0) oscillatorPane = 1;

      const s = chart.addSeries(
        LineSeries,
        {
          color: PALETTE[i % PALETTE.length], lineWidth: 1,
          priceLineVisible: false, lastValueVisible: true, title: ind.name,
        },
        onPricePane ? 0 : oscillatorPane,
      );
      s.setData(data);
    });

    // price pane dominates; oscillator pane is a strip below
    const panes = chart.panes();
    if (panes.length > 1) {
      panes[0].setStretchFactor(3);
      panes[1].setStretchFactor(1);
    }

    chart.timeScale().fitContent();
    return () => { chart.remove(); chartRef.current = null; };
  }, [bars, mode, indicators]);

  return <div ref={containerRef} className="h-[480px] w-full" />;
}
