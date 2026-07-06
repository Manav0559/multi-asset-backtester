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
import StrategyPicker, { MlHonestyNote, StrategyEntry, categoryBadge } from "@/components/StrategyPicker";
import { EmptyState } from "@/components/ui";
import { useToast } from "@/components/ToastProvider";
import { api, fetcher } from "@/lib/api";
import { Asset, MARKETS, MarketKey, assetsOfMarket } from "@/lib/assets";
type Backtest = {
  id: string; status: string; config: any;
  total_return_pct: string | null; sharpe: string | null;
  deflated_sharpe: string | null; max_drawdown_pct: string | null;
  created_at: string;
};
type Registry = { strategies: StrategyEntry[]; custom_template: string };
type SavedStrategy = {
  strategy_id: string; version_id: string; name: string;
  version: number; code: string; created_at: string;
};

const TIMEFRAMES = [
  { value: "1m", label: "1 minute" },
  { value: "15m", label: "15 minutes" },
  { value: "1h", label: "1 hour" },
  { value: "1d", label: "1 day" },
];

function BacktestsInner() {
  const [assets, setAssets] = useState<Asset[]>([]);
  const [list, setList] = useState<Backtest[]>([]);
  const [market, setMarket] = useState<MarketKey>("nasdaq");
  const [assetId, setAssetId] = useState<number | null>(null);
  const [assetIds, setAssetIds] = useState<number[]>([]);
  const [strategy, setStrategy] = useState("sma_crossover");
  const [timeframe, setTimeframe] = useState("1d");
  const [params, setParams] = useState<Record<string, number | string>>({});
  const [code, setCode] = useState("");
  const [codeName, setCodeName] = useState("My strategy");
  const [validation, setValidation] = useState<{ ok: boolean; errors: string[] } | null>(null);
  const [borrowBps, setBorrowBps] = useState(50);
  const [maxLeverage, setMaxLeverage] = useState(2);
  const [running, setRunning] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const toast = useToast();

  const { data: registry } = useSWR<Registry>("/strategies/registry", fetcher, {
    revalidateOnFocus: false,
  });
  const { data: savedStrategies, mutate: refreshSaved } = useSWR<SavedStrategy[]>(
    "/strategies", fetcher, { revalidateOnFocus: false });
  const savedWithCode = useMemo(
    () => (savedStrategies ?? []).filter((s) => s.code.trim().length > 0),
    [savedStrategies]);
  const strategies = useMemo(() => registry?.strategies ?? [], [registry]);
  const strat = strategies.find((s) => s.key === strategy);
  const isMulti = strat?.kind === "portfolio";
  const isCustom = strategy === "custom_code";

  function load() {
    api<Backtest[]>("/backtests").then(setList);
  }
  const marketAssets = useMemo(() => assetsOfMarket(assets, market), [assets, market]);

  useEffect(() => {
    api<Asset[]>("/assets").then((a) => {
      setAssets(a);
      const scoped = assetsOfMarket(a, "nasdaq");
      if (scoped.length) setAssetId(scoped[0].id);
      setAssetIds(scoped.slice(0, 2).map((x) => x.id));  // sensible default basket
    });
    load();
  }, []);

  // Picking a market re-scopes both the single asset and the basket.
  function switchMarket(m: MarketKey) {
    setMarket(m);
    const scoped = assetsOfMarket(assets, m);
    setAssetId(scoped[0]?.id ?? null);
    setAssetIds(scoped.slice(0, 2).map((x) => x.id));
  }

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
      // Custom scripts save under the user's chosen name (re-saving the same
      // name creates version 2, 3, ... server-side); built-ins get a stable
      // per-key name so the saved list isn't spammed with timestamps.
      const sv = await api<{ version_id: string }>("/strategies", {
        method: "POST",
        body: JSON.stringify({
          name: isCustom ? (codeName.trim() || "My strategy") : `builtin: ${strategy}`,
          code: isCustom ? code : "",
        }),
      });
      if (isCustom) refreshSaved();
      const payload: Record<string, unknown> = {
        strategy_version_id: sv.version_id,
        timeframe, strategy, params,
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

      {/* relative z-20: .card's backdrop-blur creates a stacking context, so the
          picker dropdown's z-index can't escape it — lift the whole form above
          the sibling results card or the table intercepts the dropdown's clicks. */}
      <form onSubmit={submit} className="card p-5 space-y-4 relative z-20">
        <div className="flex flex-wrap items-end gap-4">
          <div className="w-96">
            <label className="label">Strategy</label>
            <StrategyPicker strategies={strategies} value={strategy} onChange={setStrategy} />
          </div>

          {/* Market first: WHERE do you want to backtest? */}
          <div>
            <label className="label">Market</label>
            <div className="segmented">
              {MARKETS.map((m) => (
                <button key={m.key} type="button" onClick={() => switchMarket(m.key)}
                  className={`px-3 py-2 text-xs transition-all active:scale-95 ${
                    market === m.key ? "bg-accent/20 text-accent shadow-glow-sm"
                                     : "text-muted hover:text-slate-200"}`}>
                  {m.label}
                </button>
              ))}
            </div>
          </div>

          {!isMulti && (
            <div className="w-52">
              <label className="label">Asset ({marketAssets.length} in {MARKETS.find((m) => m.key === market)!.label})</label>
              <select className="input" value={assetId ?? ""} onChange={(e) => setAssetId(Number(e.target.value))}>
                {marketAssets.map((a) => <option key={a.id} value={a.id}>{a.symbol}</option>)}
              </select>
            </div>
          )}

          <div className="w-36">
            <label className="label">Timeframe</label>
            <select className="input" value={timeframe} onChange={(e) => setTimeframe(e.target.value)}>
              {TIMEFRAMES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
            </select>
          </div>

          {isMulti && (
            <>
              <div className="w-80">
                <label className="label">
                  Basket · {MARKETS.find((m) => m.key === market)!.label} ({assetIds.length} selected)
                </label>
                <div className="input h-auto max-h-40 overflow-y-auto py-2">
                  <div className="flex flex-wrap gap-x-3 gap-y-1">
                    {marketAssets.map((a) => (
                      <label key={a.id} className="flex items-center gap-1.5 text-xs cursor-pointer select-none">
                        <input type="checkbox" checked={assetIds.includes(a.id)}
                          onChange={() => toggleAsset(a.id)} className="accent-accent" />
                        <span className="font-mono">{a.symbol}</span>
                      </label>
                    ))}
                  </div>
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
            <span className={`px-1.5 py-0.5 rounded mr-2 ${categoryBadge(strat.category)}`}>
              {strat.category}
            </span>
            {strat.description}
          </p>
        )}

        {strat?.category === "ml" && <MlHonestyNote />}

        {isCustom && (
          <div className="space-y-3">
            <div className="flex flex-wrap items-end gap-3">
              <div className="w-64">
                <label className="label">Script name (how it appears in results)</label>
                <input className="input" value={codeName} maxLength={100}
                  onChange={(e) => setCodeName(e.target.value)} placeholder="My golden cross v2" />
              </div>
              {savedWithCode.length > 0 && (
                <div className="w-72">
                  <label className="label">Load a saved script</label>
                  <select className="input" value=""
                    onChange={(e) => {
                      const s = savedWithCode.find((x) => x.strategy_id === e.target.value);
                      if (s) { setCodeName(s.name); setCode(s.code); setValidation(null); }
                    }}>
                    <option value="">— {savedWithCode.length} saved —</option>
                    {savedWithCode.map((s) => (
                      <option key={s.strategy_id} value={s.strategy_id}>
                        {s.name} (v{s.version})
                      </option>
                    ))}
                  </select>
                </div>
              )}
              <button type="button" onClick={validate}
                className="btn-ghost !py-2 text-xs ml-auto">
                Validate code
              </button>
            </div>

            <div className="grid lg:grid-cols-3 gap-4">
              <div className="lg:col-span-2 space-y-2">
                <textarea value={code} onChange={(e) => { setCode(e.target.value); setValidation(null); }}
                  spellCheck={false}
                  className="w-full h-[26rem] rounded-lg bg-[#0a0f1a] border border-border p-4 font-mono
                             text-xs leading-relaxed text-slate-200 focus:outline-none
                             focus:border-accent resize-y" />
                {validation && (
                  validation.ok ? (
                    <p className="text-up text-xs">✓ Code accepted by the sandbox.</p>
                  ) : (
                    <ul className="text-down text-xs space-y-0.5">
                      {validation.errors.map((e, i) => <li key={i}>✕ {e}</li>)}
                    </ul>
                  )
                )}
              </div>

              {/* Cheat-sheet lives NEXT TO the editor so users never leave the page */}
              <aside className="rounded-lg border border-border bg-panel2/40 p-4 text-xs
                                leading-relaxed space-y-3 h-[26rem] overflow-y-auto">
                <div>
                  <p className="font-medium text-slate-200 mb-1">How to write a strategy</p>
                  <p className="text-muted">
                    Define one class extending <span className="font-mono text-accent">CustomStrategy</span>.
                    Each bar, return a <b>target weight</b>: <span className="font-mono">1.0</span> fully
                    long, <span className="font-mono">0.0</span> flat, <span className="font-mono">-1.0</span> fully
                    short (fractions size the position).
                  </p>
                </div>
                <div>
                  <p className="font-medium text-slate-200 mb-1">What you have</p>
                  <ul className="text-muted space-y-1">
                    <li><span className="font-mono text-slate-300">self.data</span> — OHLCV DataFrame
                      (<span className="font-mono">open/high/low/close/volume</span>, DatetimeIndex)</li>
                    <li><span className="font-mono text-slate-300">self.params</span> — your params dict</li>
                    <li><span className="font-mono text-slate-300">self.indicator(&quot;rsi&quot;, length=14)</span> —
                      any of 150+ indicators (macd, bbands, supertrend, atr, adx, stoch, vwap …)</li>
                    <li><span className="font-mono text-slate-300">pd, np, math</span> — pre-loaded</li>
                  </ul>
                </div>
                <div>
                  <p className="font-medium text-slate-200 mb-1">Hooks (pick ONE style)</p>
                  <ul className="text-muted space-y-1">
                    <li><span className="font-mono text-slate-300">setup(self)</span> — precompute series once</li>
                    <li><span className="font-mono text-slate-300">next(self, i, bar)</span> — per-bar decision,
                      return the weight</li>
                    <li><span className="font-mono text-slate-300">generate(self, data)</span> — vectorized:
                      return the whole weight Series at once (fastest)</li>
                  </ul>
                </div>
                <div>
                  <p className="font-medium text-slate-200 mb-1">Vectorized example</p>
                  <pre className="bg-[#0a0f1a] rounded p-2 overflow-x-auto text-[11px] text-slate-300">{`class Momentum(CustomStrategy):
    params = {"lookback": 20}
    def generate(self, data):
        r = data["close"].pct_change(
            self.params["lookback"])
        return (r > 0).astype(float)`}</pre>
                </div>
                <div>
                  <p className="font-medium text-slate-200 mb-1">Rules of the sandbox</p>
                  <ul className="text-muted space-y-1">
                    <li>No <span className="font-mono">import</span> — everything you need is injected</li>
                    <li>No file/network/OS access, no <span className="font-mono">eval</span>/dunders</li>
                    <li>Runs are killed after 10 minutes (infinite-loop guard)</li>
                    <li>The engine trades your signal on the NEXT bar
                      (<span className="font-mono">shift(1)</span>) — lookahead is impossible</li>
                    <li>Weights are clipped to [-1, 1]</li>
                  </ul>
                </div>
                <div>
                  <p className="font-medium text-slate-200 mb-1">Tips</p>
                  <ul className="text-muted space-y-1">
                    <li>Return <span className="font-mono">0.0</span> during indicator warm-up
                      (<span className="font-mono">pd.isna(...)</span> check)</li>
                    <li>Name your script — re-saving the same name creates v2, v3 …</li>
                    <li>Deflated Sharpe in results corrects for how many variants you tried</li>
                  </ul>
                </div>
              </aside>
            </div>
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
                <td className="p-3 font-sans">
                  {b.config?.strategy === "custom_code" && b.config?.label
                    ? <>{b.config.label} <span className="text-muted text-xs">(custom)</span></>
                    : b.config?.strategy}
                  <span className="ml-2 px-1.5 py-0.5 rounded bg-panel2 text-muted text-[11px] font-mono">
                    {b.config?.timeframe ?? "1d"}
                  </span>
                </td>
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
