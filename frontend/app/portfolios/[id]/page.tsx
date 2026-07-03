"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import {
  Area, AreaChart, CartesianGrid, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import Guard from "@/components/Guard";
import { EmptyState, Skeleton, SkeletonRows } from "@/components/ui";
import { useToast } from "@/components/ToastProvider";
import { api } from "@/lib/api";
import { Asset, groupAssets } from "@/lib/assets";
import { Hub } from "@/lib/ws";

type Portfolio = { id: string; name: string; cash_balance: string; initial_cash: string; version: number };
type Position = { asset_id: number; qty: string; avg_entry_price: string; realized_pnl: string };
type Ledger = { id: number; entry_type: string; amount: string; balance_after: string; note: string | null; created_at: string };
type EquityPoint = { time: string; cash: string; equity: string };

function PortfolioDetail() {
  const { id } = useParams<{ id: string }>();
  const [pf, setPf] = useState<Portfolio | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);
  const [ledger, setLedger] = useState<Ledger[]>([]);
  const [equityHistory, setEquityHistory] = useState<EquityPoint[]>([]);
  const [assets, setAssets] = useState<Asset[]>([]);
  const [flash, setFlash] = useState(false);

  // Trade form
  const [assetId, setAssetId] = useState<number | null>(null);
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [qty, setQty] = useState("1");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const toast = useToast();

  const load = useCallback(() => {
    api<Portfolio>(`/portfolios/${id}`).then(setPf);
    api<Position[]>(`/portfolios/${id}/positions`).then(setPositions);
    api<Ledger[]>(`/portfolios/${id}/ledger`).then(setLedger);
    api<EquityPoint[]>(`/portfolios/${id}/equity-history`).then(setEquityHistory);
  }, [id]);

  useEffect(() => {
    load();
    api<Asset[]>("/assets").then((a) => {
      setAssets(a);
      if (a.length) setAssetId(a[0].id);
    });
  }, [load]);

  // Live shared-ledger sync: when ANY collaborator trades, the hub pushes a
  // portfolio:{id} event and we refresh + flash the balance.
  const hubRef = useRef<Hub | null>(null);
  useEffect(() => {
    const hub = new Hub();
    hubRef.current = hub;
    hub.connect();
    const off = hub.subscribe(`portfolio:${id}`, () => {
      setFlash(true);
      setTimeout(() => setFlash(false), 800);
      load();
    });
    return () => { off(); hub.close(); };
  }, [id, load]);

  async function trade(e: React.FormEvent) {
    e.preventDefault();
    setMsg(null);
    try {
      const r = await api<{ status: string; reason: string | null; fill_price: string | null }>(
        `/portfolios/${id}/orders`,
        { method: "POST", body: JSON.stringify({ asset_id: assetId, side, qty: Number(qty) }) }
      );
      if (r.status === "filled") {
        setMsg({ text: `Filled ${side} ${qty} @ $${Number(r.fill_price).toFixed(2)}`, ok: true });
        toast.success(`Filled ${side} ${qty} @ $${Number(r.fill_price).toFixed(2)}`);
      } else {
        setMsg({ text: r.reason || "Rejected", ok: false });
        toast.error(r.reason || "Order rejected");
      }
      load(); // local echo; collaborators get it via WS
    } catch (e: any) {
      setMsg({ text: e.message, ok: false });
      toast.error(e.message);
    }
  }

  if (!pf) {
    return (
      <div className="space-y-6">
        <div className="space-y-2">
          <Skeleton className="h-7 w-56" />
          <Skeleton className="h-4 w-80" />
        </div>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
          <Skeleton className="h-24" /><Skeleton className="h-24" /><Skeleton className="h-24" />
        </div>
        <div className="card p-5"><SkeletonRows rows={5} /></div>
      </div>
    );
  }
  const pnl = Number(pf.cash_balance) - Number(pf.initial_cash);
  const symOf = (aid: number) => assets.find((a) => a.id === aid)?.symbol ?? `#${aid}`;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">{pf.name}</h1>
        <p className="text-sm text-muted">Shared portfolio · live-synced across collaborators</p>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
        <div className={`card p-4 transition-colors ${flash ? "border-accent" : ""}`}>
          <p className="text-xs text-muted mb-1">Shared cash balance</p>
          <p className={`stat ${flash ? "text-accent" : ""}`}>
            ${Number(pf.cash_balance).toLocaleString()}
          </p>
        </div>
        <div className="card p-4">
          <p className="text-xs text-muted mb-1">Cash vs start</p>
          <p className={`stat ${pnl >= 0 ? "text-up" : "text-down"}`}>
            {pnl >= 0 ? "+" : ""}${pnl.toLocaleString()}
          </p>
        </div>
        <div className="card p-4">
          <p className="text-xs text-muted mb-1">Ledger version</p>
          <p className="stat">v{pf.version}</p>
        </div>
      </div>

      {equityHistory.length >= 2 && (
        <div className="card p-5">
          <h2 className="text-sm font-medium mb-3">Equity over time</h2>
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart
                data={equityHistory.map((p) => ({
                  time: p.time.slice(0, 16).replace("T", " "),
                  equity: Number(p.equity),
                  cash: Number(p.cash),
                }))}
                margin={{ top: 4, right: 8, bottom: 0, left: 8 }}
              >
                <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                <XAxis dataKey="time" tick={{ fontSize: 10 }} stroke="#475569" minTickGap={40} />
                <YAxis tick={{ fontSize: 10 }} stroke="#475569" domain={["auto", "auto"]}
                  tickFormatter={(v: number) => `$${v.toLocaleString()}`} width={70} />
                <Tooltip
                  contentStyle={{ background: "#0f172a", border: "1px solid #1e293b", fontSize: 12 }}
                  formatter={(v: number, name: string) => [`$${v.toLocaleString()}`, name]}
                />
                <ReferenceLine y={Number(pf.initial_cash)} stroke="#475569" strokeDasharray="4 4" />
                <Area type="stepAfter" dataKey="equity" stroke="#22d3ee" fill="#22d3ee"
                  fillOpacity={0.08} strokeWidth={1.5} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      <div className="grid lg:grid-cols-3 gap-6">
        <form onSubmit={trade} className="card p-5 space-y-4 h-fit">
          <h2 className="text-sm font-medium">Place order</h2>
          <div>
            <label className="label">Asset</label>
            <select className="input" value={assetId ?? ""}
              onChange={(e) => setAssetId(Number(e.target.value))}>
              {groupAssets(assets).map((g) => (
                <optgroup key={g.label} label={g.label}>
                  {g.items.map((a) => <option key={a.id} value={a.id}>{a.symbol}</option>)}
                </optgroup>
              ))}
            </select>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <button type="button" onClick={() => setSide("buy")}
              className={side === "buy" ? "btn bg-up text-bg" : "btn-ghost"}>Buy</button>
            <button type="button" onClick={() => setSide("sell")}
              className={side === "sell" ? "btn bg-down text-white" : "btn-ghost"}>Sell</button>
          </div>
          <div>
            <label className="label">Quantity</label>
            <input className="input" type="number" value={qty} min={0} step="any"
              onChange={(e) => setQty(e.target.value)} />
          </div>
          <button className="btn-primary w-full">Submit {side}</button>
          {msg && <p className={`text-sm ${msg.ok ? "text-up" : "text-down"}`}>{msg.text}</p>}
        </form>

        <div className="card p-5 lg:col-span-2">
          <h2 className="text-sm font-medium mb-3">Positions</h2>
          {positions.length === 0 ? (
            <EmptyState icon="📊" title="No open positions"
              hint="Place an order to open your first position — collaborators see fills live." />
          ) : (
            <table className="w-full text-sm">
              <thead className="text-muted text-xs">
                <tr className="text-left">
                  <th className="pb-2">Asset</th><th className="pb-2 text-right">Qty</th>
                  <th className="pb-2 text-right">Avg price</th><th className="pb-2 text-right">Realized P&L</th>
                </tr>
              </thead>
              <tbody className="font-mono">
                {positions.map((p) => (
                  <tr key={p.asset_id} className="border-t border-border">
                    <td className="py-2">{symOf(p.asset_id)}</td>
                    <td className="py-2 text-right">{Number(p.qty)}</td>
                    <td className="py-2 text-right">${Number(p.avg_entry_price).toFixed(2)}</td>
                    <td className={`py-2 text-right ${Number(p.realized_pnl) >= 0 ? "text-up" : "text-down"}`}>
                      ${Number(p.realized_pnl).toFixed(2)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <div className="card p-5">
        <h2 className="text-sm font-medium mb-3">Shared ledger</h2>
        <table className="w-full text-sm">
          <thead className="text-muted text-xs">
            <tr className="text-left">
              <th className="pb-2">Type</th><th className="pb-2">Note</th>
              <th className="pb-2 text-right">Amount</th><th className="pb-2 text-right">Balance</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {ledger.map((e) => (
              <tr key={e.id} className="border-t border-border">
                <td className="py-2">{e.entry_type}</td>
                <td className="py-2 text-slate-400">{e.note}</td>
                <td className={`py-2 text-right ${Number(e.amount) >= 0 ? "text-up" : "text-down"}`}>
                  {Number(e.amount) >= 0 ? "+" : ""}{Number(e.amount).toFixed(2)}
                </td>
                <td className="py-2 text-right">${Number(e.balance_after).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function Page() {
  return <Guard><PortfolioDetail /></Guard>;
}
