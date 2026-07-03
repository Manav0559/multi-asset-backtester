"use client";

/**
 * Backtest launcher. The strategy picker, param forms, and BYOC editor are
 * all rendered from GET /strategies/registry — the frontend holds NO
 * hardcoded strategy list, so new backend algorithms appear here on deploy.
 */
import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import Guard from "@/components/Guard";
import { EmptyState } from "@/components/ui";
import { useToast } from "@/components/ToastProvider";
import { api, fetcher } from "@/lib/api";

type Asset = { id: number; symbol: string; exchange: string };
type Backtest = {
  id: string; status: string; config: any;
  total_return_pct: string | null; sharpe: string | null;
  deflated_sharpe: string | null; max_drawdown_pct: string | null;
  created_at: string;
};
type RegistryEntry = {
  key: string; kind: "single" | "portfolio"; category: string;
  description: string; defaults: Record<string, number | string | null>;
};
type Registry = { strategies: RegistryEntry[]; custom_template: string };

const CATEGORY_BADGE: Record<string, string> = {
  trend: "bg-accent/15 text-accent",
  mean_reversion: "bg-purple-500/15 text-purple-300",
  arbitrage: "bg-amber-500/15 text-amber-300",
  baseline: "bg-slate-500/20 text-slate-300",
  ml: "bg-emerald-500/15 text-emerald-300",
  custom: "bg-pink-500/15 text-pink-300",
};

function BacktestsInner() {
  const [assets, setAssets] = useState<Asset[]>([]);
  const [list, setList] = useState<Backtest[]>([]);
  const [assetId, setAssetId] = useState<number | null>(null);
  const [assetIds, setAssetIds] = useState<number[]>([]);
  const [strategy, setStrategy] = useState("sma_crossover");
  const [params, setParams] = useState<Record<string, number | string>>({});
  const [code, setCode] = useState("");
  const [validation, setValidation] = useState<{ ok: boolean; errors: string[] } | null>(null);
  const [borrowBps, setBorrowBps] = useState(50);
  const [maxLeverage, setMaxLeverage] = useState(2);
  const [running, setRunning] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const toast = useToast();

  const { data: registry } = useSWR<Registry>("/strategies/registry", fetcher, {
    revalidateOnFocus: false,
  });
  const strategies = useMemo(() => registry?.strategies ?? [], [registry]);
  const strat = strategies.find((s) => s.key === strategy);
  const isMulti = strat?.kind === "portfolio";
  const isCustom = strategy === "custom_code";

  function load() {
    api<Backtest[]>("/backtests").then(setList);
  }
  useEffect(() => {
    api<Asset[]>("/assets").then((a) => {
      setAssets(a);
      if (a.length) setAssetId(a[0].id);
      setAssetIds(a.slice(0, 2).map((x) => x.id));  // sensible default basket
    });
    load();
  }, []);

  // Reset the param form (and seed the editor) when the strategy changes.
  useEffect(() => {
    const entry = strategies.find((s) => s.key === strategy);
    const next: Record<string, number | string> = {};
    Object.entries(entry?.defaults ?? {}).forEach(([k, v]) => {
      if (v != null) next[k] = v;
    });
    setParams(next);
    setValidation(null);
    if (strategy === "custom_code" && !code && registry?.custom_template) {
      setCode(registry.custom_template);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [strategy, registry]);

  const grouped = useMemo(() => ({
    single: strategies.filter((s) => s.kind === "single" && s.key !== "custom_code"),
    portfolio: strategies.filter((s) => s.kind === "portfolio"),
  }), [strategies]);

  function toggleAsset(id: number) {
    setAssetIds((prev) => prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]);
  }

  async function validate() {
    setValidation(null);
    const res = await api<{ ok: boolean; errors: string[] }>("/strategies/validate", {
      method: "POST", body: JSON.stringify({ code, params }),
    });
    setValidation(res);
    return res.ok;
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitError("");
    if (isCustom && !(await validate())) return;
    setRunning(true);
    try {
      const sv = await api<{ version_id: string }>("/strategies", {
        method: "POST",
        body: JSON.stringify({ name: `${strategy} ${Date.now()}`, code: isCustom ? code : "" }),
      });
      const payload: Record<string, unknown> = {
        strategy_version_id: sv.version_id,
        timeframe: "1d", strategy, params,
        initial_capital: 100000, commission_bps: 5, n_trials: 20,
      };
      if (isCustom) payload.code = code;
      if (isMulti) {
        payload.asset_ids = assetIds;
        payload.borrow_bps_annual = borrowBps;
        payload.max_gross_leverage = maxLeverage;
      } else {
        payload.asset_id = assetId;
      }
      await api("/backtests", { method: "POST", body: JSON.stringify(payload) });
      toast.success(`Backtest queued: ${strategy}`);
      // Poll a few times while the Celery job finishes.
      let tries = 0;
      const iv = setInterval(() => {
        load();
        if (++tries > 6) clearInterval(iv);
      }, 1000);
    } catch (err: any) {
      setSubmitError(err?.message ?? String(err));
      toast.error(err?.message ?? "Backtest submission failed");
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Backtests</h1>
        <p className="text-sm text-muted">
          {strategies.length ? `${strategies.length} strategies` : "Vectorized strategies"} ·
          built-ins, classic quant algorithms, or your own Python
        </p>
      </div>

      <form onSubmit={submit} className="card p-5 space-y-4">
        <div className="flex flex-wrap items-end gap-4">
          <div className="w-72">
            <label className="label">Strategy</label>
            <select className="input" value={strategy} onChange={(e) => setStrategy(e.target.value)}>
              <optgroup label="Single-asset">
                {grouped.single.map((s) => <option key={s.key} value={s.key}>{s.key}</option>)}
              </optgroup>
              <optgroup label="Portfolio · long/short · multi-asset">
                {grouped.portfolio.map((s) => <option key={s.key} value={s.key}>{s.key}</option>)}
              </optgroup>
              <optgroup label="Bring your own code">
                <option value="custom_code">custom_code — write Python</option>
              </optgroup>
            </select>
          </div>

          {!isMulti && (
            <div className="w-52">
              <label className="label">Asset</label>
              <select className="input" value={assetId ?? ""} onChange={(e) => setAssetId(Number(e.target.value))}>
                {assets.map((a) => <option key={a.id} value={a.id}>{a.symbol} · {a.exchange}</option>)}
              </select>
            </div>
          )}

          {isMulti && (
            <>
              <div className="w-64">
                <label className="label">Basket ({assetIds.length} selected)</label>
                <div className="input h-auto max-h-32 overflow-y-auto py-2 flex flex-wrap gap-x-3 gap-y-1">
                  {assets.map((a) => (
                    <label key={a.id} className="flex items-center gap-1.5 text-xs cursor-pointer select-none">
                      <input type="checkbox" checked={assetIds.includes(a.id)}
                        onChange={() => toggleAsset(a.id)} className="accent-accent" />
                      <span className="font-mono">{a.symbol}</span>
                    </label>
                  ))}
                </div>
              </div>
              <div className="w-28">
                <label className="label">Borrow (bps/yr)</label>
                <input type="number" min={0} className="input" value={borrowBps}
                  onChange={(e) => setBorrowBps(Number(e.target.value))} />
              </div>
              <div className="w-28">
                <label className="label">Max gross lev.</label>
                <input type="number" min={0} step={0.5} className="input" value={maxLeverage}
                  onChange={(e) => setMaxLeverage(Number(e.target.value))} />
              </div>
            </>
          )}

          {/* param form generated from the registry's defaults */}
          {!isCustom && Object.entries(params).map(([k, v]) => (
            <div key={k} className="w-24">
              <label className="label font-mono">{k}</label>
              <input type={typeof v === "number" ? "number" : "text"} step="any" className="input"
                value={v}
                onChange={(e) => setParams((p) => ({
                  ...p, [k]: typeof v === "number" ? Number(e.target.value) : e.target.value,
                }))} />
            </div>
          ))}

          <button className="btn-primary"
            disabled={running || (isMulti ? assetIds.length < 2 : !assetId)}>
            {running ? "Queuing…" : "Run backtest"}
          </button>
        </div>

        {strat?.description && (
          <p className="text-xs text-muted -mt-1">
            <span className={`px-1.5 py-0.5 rounded mr-2 ${CATEGORY_BADGE[strat.category] ?? ""}`}>
              {strat.category}
            </span>
            {strat.description}
          </p>
        )}

        {isCustom && (
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <label className="label mb-0">
                Your strategy — extend <span className="font-mono text-accent">CustomStrategy</span>,
                return a weight in [-1, 1] per bar
              </label>
              <button type="button" onClick={validate}
                className="text-xs px-3 py-1.5 rounded-lg border border-border text-muted
                           hover:text-accent hover:border-accent transition-colors">
                Validate code
              </button>
            </div>
            <textarea value={code} onChange={(e) => { setCode(e.target.value); setValidation(null); }}
              spellCheck={false}
              className="w-full h-80 rounded-lg bg-[#0a0f1a] border border-border p-4 font-mono text-xs
                         leading-relaxed text-slate-200 focus:outline-none focus:border-accent resize-y" />
            {validation && (
              validation.ok ? (
                <p className="text-up text-xs">✓ Code accepted by the sandbox.</p>
              ) : (
                <ul className="text-down text-xs space-y-0.5">
                  {validation.errors.map((e, i) => <li key={i}>✕ {e}</li>)}
                </ul>
              )
            )}
            <p className="text-xs text-muted">
              Sandbox: <span className="font-mono">pd</span>, <span className="font-mono">np</span>,{" "}
              <span className="font-mono">math</span> are pre-loaded; imports are blocked.{" "}
              <span className="font-mono">self.indicator(&quot;rsi&quot;, length=14)</span> gives any of
              150+ indicators. The engine shifts your signal one bar — lookahead is impossible.
            </p>
          </div>
        )}

        {submitError && <p className="text-down text-xs">{submitError}</p>}
      </form>

      <div className="card overflow-hidden">
        <table className="w-full text-sm">
          <thead className="text-muted text-xs bg-panel2">
            <tr className="text-left">
              <th className="p-3">Strategy</th><th className="p-3">Status</th>
              <th className="p-3 text-right">Return</th><th className="p-3 text-right">Sharpe</th>
              <th className="p-3 text-right">Deflated Sharpe</th><th className="p-3 text-right">Max DD</th>
              <th className="p-3"></th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {list.map((b) => (
              <tr key={b.id} className="border-t border-border hover:bg-panel2/40">
                <td className="p-3 font-sans">{b.config?.strategy}</td>
                <td className="p-3"><StatusBadge status={b.status} /></td>
                <td className={`p-3 text-right ${num(b.total_return_pct) >= 0 ? "text-up" : "text-down"}`}>
                  {fmt(b.total_return_pct, "%")}
                </td>
                <td className="p-3 text-right">{fmt(b.sharpe)}</td>
                <td className="p-3 text-right text-accent">{fmt(b.deflated_sharpe)}</td>
                <td className="p-3 text-right text-down">{fmt(b.max_drawdown_pct, "%")}</td>
                <td className="p-3 text-right">
                  <Link href={`/backtests/${b.id}`} className="text-accent hover:underline">View →</Link>
                </td>
              </tr>
            ))}
            {list.length === 0 && (
              <tr><td colSpan={7} className="font-sans">
                <EmptyState icon="⚡" title="No backtests yet"
                  hint="Pick a strategy above — or write your own with custom_code — and run it on real historical prices." />
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function num(v: string | null) { return v == null ? 0 : Number(v); }
function fmt(v: string | null, suffix = "") { return v == null ? "—" : `${Number(v).toFixed(2)}${suffix}`; }

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, string> = {
    completed: "bg-up/20 text-up", running: "bg-accent/20 text-accent",
    queued: "bg-slate-600/30 text-slate-300", failed: "bg-down/20 text-down",
  };
  return <span className={`px-2 py-0.5 rounded text-xs ${map[status] ?? ""}`}>{status}</span>;
}

export default function BacktestsPage() {
  return <Guard><BacktestsInner /></Guard>;
}
