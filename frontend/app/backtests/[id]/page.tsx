"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import {
  Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import Guard from "@/components/Guard";
import { api } from "@/lib/api";
import { ccySymbol } from "@/lib/format";

type Backtest = {
  id: string; status: string; config: any; error: string | null;
  total_return_pct: string | null; cagr_pct: string | null;
  sharpe: string | null; sortino: string | null; deflated_sharpe: string | null;
  max_drawdown_pct: string | null; trade_count: number | null; win_rate_pct: string | null;
  equity_curve: [string, number][] | null;
  diagnostics: {
    oos_accuracy?: number; n_predictions?: number;
    feature_importance?: Record<string, number>;
    // Portfolio (long/short, multi-asset) diagnostics.
    n_assets?: number; avg_gross_exposure?: number;
    max_net_exposure?: number; is_market_neutral?: boolean;
    // Tail risk (per-bar loss fractions) + factor attribution.
    risk?: Record<string, number>;
    currency?: string;
    attribution?: {
      alpha_annual_pct?: number; r_squared?: number; n_obs?: number;
      betas?: Record<string, number>; factors_note?: string;
    };
  } | null;
};
type Yearly = {
  year: number; return_pct: string; max_drawdown_pct: string;
  sharpe: string; sortino: string; volatility_pct: string; trade_count: number; win_rate_pct: string;
};

function BacktestDetail() {
  const { id } = useParams<{ id: string }>();
  const [bt, setBt] = useState<Backtest | null>(null);
  const [yearly, setYearly] = useState<Yearly[]>([]);

  useEffect(() => {
    function load() {
      api<Backtest>(`/backtests/${id}`).then((b) => {
        setBt(b);
        if (b.status === "completed") api<Yearly[]>(`/backtests/${id}/yearly`).then(setYearly);
      });
    }
    load();
    const iv = setInterval(load, 1500);
    return () => clearInterval(iv);
  }, [id]);

  if (!bt) {
    return (
      <div className="space-y-6">
        <div className="skeleton h-7 w-72" />
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <div className="skeleton h-24" /><div className="skeleton h-24" />
          <div className="skeleton h-24" /><div className="skeleton h-24" />
        </div>
        <div className="skeleton h-72" />
      </div>
    );
  }

  const curve = (bt.equity_curve ?? []).map(([t, v]) => ({ time: t.slice(0, 10), equity: v }));

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">
          {bt.config?.strategy} · {bt.config?.timeframe}
        </h1>
        <p className="text-sm text-muted">
          Backtest {bt.id.slice(0, 8)} — status: <span className="text-slate-200">{bt.status}</span>
        </p>
      </div>

      {bt.error && <div className="card p-4 border-down text-down text-sm">{bt.error}</div>}

      {bt.diagnostics?.oos_accuracy != null && (
        <div className="card p-5 border-accent/40">
          <div className="flex items-center gap-2 mb-4">
            <span className="text-accent">◈</span>
            <h2 className="text-sm font-medium">Machine-Learning Diagnostics</h2>
            <span className="text-xs text-muted">walk-forward · out-of-sample</span>
          </div>
          <div className="grid md:grid-cols-3 gap-6">
            <div>
              <p className="text-[11px] text-muted mb-1">OOS Directional Accuracy</p>
              <p className="text-2xl font-mono font-semibold text-accent">
                {(bt.diagnostics.oos_accuracy * 100).toFixed(2)}%
              </p>
              <p className="text-xs text-muted mt-1">
                over {bt.diagnostics.n_predictions?.toLocaleString()} held-out predictions
              </p>
              <p className="text-[11px] text-muted mt-2 leading-relaxed">
                A value near 50% is expected & honest — anything implausibly high would signal leakage.
              </p>
            </div>
            <div className="md:col-span-2">
              <p className="text-[11px] text-muted mb-2">Feature Importance (top drivers)</p>
              <div className="space-y-1.5">
                {Object.entries(bt.diagnostics.feature_importance ?? {})
                  .slice(0, 8)
                  .map(([name, val]) => {
                    const max = Math.max(
                      ...Object.values(bt.diagnostics!.feature_importance ?? { x: 1 })
                    );
                    return (
                      <div key={name} className="flex items-center gap-2 text-xs">
                        <span className="w-28 font-mono text-slate-300">{name}</span>
                        <div className="flex-1 h-2 bg-panel2 rounded overflow-hidden">
                          <div className="h-full bg-accent"
                            style={{ width: `${(val / max) * 100}%` }} />
                        </div>
                        <span className="w-12 text-right font-mono text-muted">
                          {val.toFixed(3)}
                        </span>
                      </div>
                    );
                  })}
              </div>
            </div>
          </div>
        </div>
      )}

      {bt.diagnostics?.avg_gross_exposure != null && (
        <div className="card p-5 border-accent/40">
          <div className="flex items-center gap-2 mb-4">
            <span className="text-accent">⇄</span>
            <h2 className="text-sm font-medium">Portfolio Exposure</h2>
            <span className="text-xs text-muted">long/short · multi-asset</span>
            {bt.diagnostics.is_market_neutral ? (
              <span className="ml-auto px-2 py-0.5 rounded text-xs bg-up/20 text-up">
                ● Market-neutral
              </span>
            ) : (
              <span className="ml-auto px-2 py-0.5 rounded text-xs bg-slate-600/30 text-slate-300">
                Directional
              </span>
            )}
          </div>
          <div className="grid grid-cols-3 gap-6">
            <div>
              <p className="text-[11px] text-muted mb-1">Assets in Basket</p>
              <p className="text-2xl font-mono font-semibold">{bt.diagnostics.n_assets}</p>
            </div>
            <div>
              <p className="text-[11px] text-muted mb-1">Avg Gross Exposure</p>
              <p className="text-2xl font-mono font-semibold text-accent">
                {((bt.diagnostics.avg_gross_exposure ?? 0) * 100).toFixed(1)}%
              </p>
              <p className="text-[11px] text-muted mt-1">sum(|weights|) — deployed leverage</p>
            </div>
            <div>
              <p className="text-[11px] text-muted mb-1">Max Net Exposure</p>
              <p className="text-2xl font-mono font-semibold">
                {((bt.diagnostics.max_net_exposure ?? 0) * 100).toFixed(1)}%
              </p>
              <p className="text-[11px] text-muted mt-1">|sum(weights)| — directional tilt</p>
            </div>
          </div>
        </div>
      )}

      <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-4">
        <Metric label="Total Return" value={pct(bt.total_return_pct)} tone={num(bt.total_return_pct)} />
        <Metric label="CAGR" value={pct(bt.cagr_pct)} tone={num(bt.cagr_pct)} />
        <Metric label="Sharpe" value={fx(bt.sharpe)} />
        <Metric label="Sortino" value={fx(bt.sortino)} />
        <Metric label="Deflated Sharpe" value={fx(bt.deflated_sharpe)} accent />
        <Metric label="Max Drawdown" value={pct(bt.max_drawdown_pct)} tone={-1} />
        <Metric label="Win Rate" value={pct(bt.win_rate_pct)} />
      </div>

      {bt.diagnostics?.risk?.var_95 != null && (
        <div className="card p-5" data-testid="risk-card">
          <div className="flex items-baseline gap-2 mb-4">
            <h2 className="text-sm font-medium">Tail Risk</h2>
            <span className="text-xs text-muted">
              per-bar loss estimates · historical + Cornish-Fisher
              (skew {bt.diagnostics.risk.skew?.toFixed(2)}, ex-kurt{" "}
              {bt.diagnostics.risk.excess_kurtosis?.toFixed(2)})
            </span>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
            <Metric label="VaR 95%" value={lossPct(bt.diagnostics.risk.var_95)} tone={-1} />
            <Metric label="ES 95%" value={lossPct(bt.diagnostics.risk.es_95)} tone={-1} />
            <Metric label="CF-VaR 95%" value={lossPct(bt.diagnostics.risk.cf_var_95)} tone={-1} />
            <Metric label="VaR 99%" value={lossPct(bt.diagnostics.risk.var_99)} tone={-1} />
            <Metric label="ES 99%" value={lossPct(bt.diagnostics.risk.es_99)} tone={-1} />
            <Metric label="CF-VaR 99%" value={lossPct(bt.diagnostics.risk.cf_var_99)} tone={-1} />
          </div>
          <p className="text-[11px] text-muted mt-3">
            ES = average loss in the worst 5%/1% of bars — the &ldquo;how bad when it&rsquo;s
            bad&rdquo; number. Cornish-Fisher widens the normal estimate for skew/fat tails;
            CF &gt; VaR means the smooth curve hides an ugly tail.
          </p>
        </div>
      )}

      {bt.diagnostics?.attribution?.betas != null && (
        <div className="card p-5" data-testid="attribution-card">
          <div className="flex items-baseline gap-2 mb-4">
            <h2 className="text-sm font-medium">Factor Attribution</h2>
            <span className="text-xs text-muted">
              OLS vs universe factors · R² {bt.diagnostics.attribution.r_squared?.toFixed(2)} ·{" "}
              {bt.diagnostics.attribution.n_obs} bars
            </span>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <Metric label="Alpha (annualized)"
              value={`${(bt.diagnostics.attribution.alpha_annual_pct ?? 0).toFixed(2)}%`}
              tone={bt.diagnostics.attribution.alpha_annual_pct ?? 0} accent />
            <Metric label="β Market" value={(bt.diagnostics.attribution.betas.MKT ?? 0).toFixed(2)} />
            <Metric label="β Momentum" value={(bt.diagnostics.attribution.betas.MOM ?? 0).toFixed(2)} />
            <Metric label="β Liquidity (size proxy)" value={(bt.diagnostics.attribution.betas.LIQ ?? 0).toFixed(2)} />
          </div>
          <p className="text-[11px] text-muted mt-3">
            Returns explained by factor exposure aren&rsquo;t alpha — a &ldquo;momentum strategy&rdquo;
            with β<sub>MOM</sub> ≈ 1 and α ≈ 0 is buyable factor risk. {bt.diagnostics.attribution.factors_note}.
          </p>
        </div>
      )}

      <div className="card p-5">
        <h2 className="text-sm font-medium text-muted mb-3">Equity Curve</h2>
        {curve.length ? (
          <ResponsiveContainer width="100%" height={300}>
            <AreaChart data={curve}>
              <defs>
                <linearGradient id="eq" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#22d3ee" stopOpacity={0.4} />
                  <stop offset="100%" stopColor="#22d3ee" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="#1f2937" vertical={false} />
              <XAxis dataKey="time" tick={{ fill: "#64748b", fontSize: 11 }} minTickGap={50} />
              <YAxis domain={["auto", "auto"]} tick={{ fill: "#64748b", fontSize: 11 }} width={70}
                tickFormatter={(v) => `${ccySymbol(bt.diagnostics?.currency)}${(v / 1000).toFixed(0)}k`} />
              <Tooltip contentStyle={{ background: "#111725", border: "1px solid #1f2937", borderRadius: 8 }} />
              <Area type="monotone" dataKey="equity" stroke="#22d3ee" fill="url(#eq)" strokeWidth={2} />
            </AreaChart>
          </ResponsiveContainer>
        ) : <p className="text-muted text-sm">Waiting for results…</p>}
      </div>

      <div className="card overflow-hidden">
        <div className="p-4 border-b border-border">
          <h2 className="text-sm font-medium">Year-over-Year Performance</h2>
          <p className="text-xs text-muted">Performance slicing — per calendar year</p>
        </div>
        <table className="w-full text-sm">
          <thead className="text-muted text-xs bg-panel2">
            <tr className="text-left">
              <th className="p-3">Year</th><th className="p-3 text-right">Return</th>
              <th className="p-3 text-right">Max DD</th><th className="p-3 text-right">Sharpe</th>
              <th className="p-3 text-right">Sortino</th><th className="p-3 text-right">Volatility</th>
              <th className="p-3 text-right">Trades</th><th className="p-3 text-right">Win Rate</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {yearly.map((y) => (
              <tr key={y.year} className="border-t border-border">
                <td className="p-3 font-semibold">{y.year}</td>
                <td className={`p-3 text-right ${num(y.return_pct) >= 0 ? "text-up" : "text-down"}`}>{pct(y.return_pct)}</td>
                <td className="p-3 text-right text-down">{pct(y.max_drawdown_pct)}</td>
                <td className="p-3 text-right">{fx(y.sharpe)}</td>
                <td className="p-3 text-right">{fx(y.sortino)}</td>
                <td className="p-3 text-right">{pct(y.volatility_pct)}</td>
                <td className="p-3 text-right">{y.trade_count}</td>
                <td className="p-3 text-right">{pct(y.win_rate_pct)}</td>
              </tr>
            ))}
            {yearly.length === 0 && (
              <tr><td colSpan={8} className="p-6 text-center text-muted">No yearly data yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function num(v: string | null) { return v == null ? 0 : Number(v); }
function pct(v: string | null) { return v == null ? "—" : `${Number(v).toFixed(2)}%`; }
function fx(v: string | null) { return v == null ? "—" : Number(v).toFixed(3); }
// Tail-risk values arrive as positive per-bar loss FRACTIONS.
function lossPct(v: number | undefined) { return v == null ? "—" : `${(v * 100).toFixed(2)}%`; }

function Metric({ label, value, tone, accent }: {
  label: string; value: string; tone?: number; accent?: boolean;
}) {
  const cls = accent ? "text-accent" : tone == null ? "" : tone >= 0 ? "text-up" : "text-down";
  return (
    <div className="card p-3">
      <p className="text-[11px] text-muted mb-1">{label}</p>
      <p className={`text-lg font-mono font-semibold ${cls}`}>{value}</p>
    </div>
  );
}

export default function Page() {
  return <Guard><BacktestDetail /></Guard>;
}
