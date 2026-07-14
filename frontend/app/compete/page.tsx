"use client";

/**
 * Consent-based competitions. No global leaderboard — you challenge a specific
 * user, both pick a portfolio, and each sees only the other's aggregate curve +
 * metrics (never positions/trades). Mirrors the backend's consent contract.
 */
import { useMemo, useState } from "react";
import useSWR from "swr";
import Guard from "@/components/Guard";
import ProvenanceBadge from "@/components/ProvenanceBadge";
import { EmptyState } from "@/components/ui";
import { useToast } from "@/components/ToastProvider";
import { api, fetcher } from "@/lib/api";

type Challenge = {
  id: string; status: string;
  challenger_username: string; opponent_username: string;
  duration_days: number; start_at: string | null; end_at: string | null;
  winner_id: string | null; viewer_is_challenger: boolean;
  challenger_id: string; opponent_id: string;
};
type Metrics = {
  return_pct: number; max_drawdown_pct: number; sharpe: number;
  win_rate: number; n_trades: number; equity: string;
  curve: { t: string; v: number }[];
};
type HeadToHead = { challenge: Challenge; you: Metrics; them: Metrics; frozen: boolean };
type Portfolio = { id: string; name: string };

function countdown(end: string | null): string {
  if (!end) return "";
  const ms = Date.parse(end) - Date.now();
  if (ms <= 0) return "ending…";
  const d = Math.floor(ms / 86400000), h = Math.floor((ms % 86400000) / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  return d > 0 ? `${d}d ${h}h left` : h > 0 ? `${h}h ${m}m left` : `${m}m left`;
}

// Two curves normalized to 100, overlaid — cheap inline SVG.
function DualCurve({ you, them }: { you: number[]; them: number[] }) {
  const all = [...you, ...them, 100];
  const min = Math.min(...all), max = Math.max(...all), span = max - min || 1;
  const W = 320, H = 90, PAD = 4;
  const path = (vals: number[]) =>
    vals.map((v, i) => {
      const x = PAD + (i * (W - 2 * PAD)) / Math.max(vals.length - 1, 1);
      const y = H - PAD - ((v - min) * (H - 2 * PAD)) / span;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
  const baseY = H - PAD - ((100 - min) * (H - 2 * PAD)) / span;
  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} className="overflow-visible">
      <line x1={PAD} y1={baseY} x2={W - PAD} y2={baseY} stroke="#334155"
        strokeDasharray="3 3" strokeWidth="1" />
      {them.length > 1 && <polyline points={path(them)} fill="none" stroke="#f472b6" strokeWidth="1.5" />}
      {you.length > 1 && <polyline points={path(you)} fill="none" stroke="#22d3ee" strokeWidth="2" />}
    </svg>
  );
}

function Metric({ label, mine, theirs, fmt, better }: {
  label: string; mine: number; theirs: number;
  fmt: (n: number) => string; better?: "high" | "low";
}) {
  const win = better === "high" ? mine > theirs : better === "low" ? mine < theirs : false;
  const lose = better === "high" ? mine < theirs : better === "low" ? mine > theirs : false;
  return (
    <tr className="border-t border-border/50">
      <td className={`py-1.5 text-right font-mono ${win ? "text-up" : lose ? "text-down" : ""}`}>{fmt(mine)}</td>
      <td className="py-1.5 text-center text-xs text-muted">{label}</td>
      <td className="py-1.5 text-left font-mono text-slate-400">{fmt(theirs)}</td>
    </tr>
  );
}

function HeadToHeadCard({ challenge }: { challenge: Challenge }) {
  const { data } = useSWR<HeadToHead>(`/challenges/${challenge.id}`, fetcher, {
    refreshInterval: challenge.status === "active" ? 5000 : 0,
  });
  const opp = challenge.viewer_is_challenger ? challenge.opponent_username : challenge.challenger_username;
  const me = challenge.viewer_is_challenger ? challenge.challenger_username : challenge.opponent_username;
  const finished = challenge.status === "finished";
  const iWon = finished && challenge.winner_id ===
    (challenge.viewer_is_challenger ? challenge.challenger_id : challenge.opponent_id);
  const draw = finished && !challenge.winner_id;

  const pct = (n: number) => `${n >= 0 ? "+" : ""}${n.toFixed(2)}%`;
  return (
    <div className="card p-5 space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-sm">
          <span className="font-mono text-accent">{me}</span>
          <span className="text-muted mx-1.5">vs</span>
          <span className="font-mono text-pink-400">{opp}</span>
        </div>
        {finished ? (
          <span className={`text-xs px-2 py-0.5 rounded-full ${
            draw ? "bg-slate-600/30 text-slate-300"
                 : iWon ? "bg-up/20 text-up" : "bg-down/20 text-down"}`}>
            {draw ? "Draw" : iWon ? "🏆 You won" : "Defeated"}
          </span>
        ) : (
          <span className="text-xs text-muted">{countdown(challenge.end_at)}</span>
        )}
      </div>
      {data ? (
        <>
          <DualCurve you={data.you.curve.map((p) => p.v)} them={data.them.curve.map((p) => p.v)} />
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-muted">
                <th className="text-right font-normal text-accent">{me}</th>
                <th></th>
                <th className="text-left font-normal text-pink-400">{opp}</th>
              </tr>
            </thead>
            <tbody>
              <Metric label="return" mine={data.you.return_pct} theirs={data.them.return_pct} fmt={pct} better="high" />
              <Metric label="max DD" mine={data.you.max_drawdown_pct} theirs={data.them.max_drawdown_pct} fmt={pct} better="low" />
              <Metric label="Sharpe" mine={data.you.sharpe} theirs={data.them.sharpe} fmt={(n) => n.toFixed(2)} better="high" />
              <Metric label="win rate" mine={data.you.win_rate} theirs={data.them.win_rate} fmt={(n) => `${n.toFixed(0)}%`} better="high" />
              <Metric label="trades" mine={data.you.n_trades} theirs={data.them.n_trades} fmt={(n) => `${n}`} />
            </tbody>
          </table>
          {finished && <p className="text-[11px] text-muted text-center">Final result — frozen at end of window.</p>}
        </>
      ) : <div className="skeleton h-40" />}
    </div>
  );
}

function CompeteInner() {
  const toast = useToast();
  const { data: challenges, mutate } = useSWR<Challenge[]>("/challenges", fetcher, { refreshInterval: 5000 });
  const { data: portfolios } = useSWR<Portfolio[]>("/portfolios", fetcher);

  const [oppName, setOppName] = useState("");
  const [myPf, setMyPf] = useState("");
  const [days, setDays] = useState(7);
  const [busy, setBusy] = useState(false);

  const { incoming, outgoing, active, finished } = useMemo(() => {
    const c = challenges ?? [];
    return {
      incoming: c.filter((x) => x.status === "pending" && !x.viewer_is_challenger),
      outgoing: c.filter((x) => x.status === "pending" && x.viewer_is_challenger),
      active: c.filter((x) => x.status === "active"),
      finished: c.filter((x) => x.status === "finished"),
    };
  }, [challenges]);

  async function createChallenge(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await api("/challenges", { method: "POST", body: JSON.stringify({
        opponent_username: oppName.trim(), challenger_portfolio_id: myPf, duration_days: days }) });
      toast.success(`Challenge sent to ${oppName}`);
      setOppName(""); mutate();
    } catch (err: any) { toast.error(err.message); }
    finally { setBusy(false); }
  }

  async function act(id: string, path: string, body?: object, msg?: string) {
    try {
      await api(`/challenges/${id}/${path}`, { method: "POST", body: body ? JSON.stringify(body) : undefined });
      if (msg) toast.success(msg);
      mutate();
    } catch (err: any) { toast.error(err.message); }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Compete</h1>
        <p className="text-sm text-muted flex items-center gap-2 flex-wrap">
          Head-to-head, consent-based. You each pick a portfolio and see only the other&apos;s
          performance curve — never their positions or trades.
          <ProvenanceBadge provenance="last_session" label="MARKED AT LAST CLOSE"
            title="Curves replay each portfolio's ledger with positions valued at stored closes — not a live mark" />
        </p>
      </div>

      <form onSubmit={createChallenge} className="card p-5 flex flex-wrap items-end gap-3">
        <div className="w-48">
          <label className="label">Challenge (username)</label>
          <input className="input" value={oppName} required placeholder="bob_demo"
            onChange={(e) => setOppName(e.target.value)} />
        </div>
        <div className="w-48">
          <label className="label">Your portfolio</label>
          <select className="input" value={myPf} required onChange={(e) => setMyPf(e.target.value)}>
            <option value="">Select…</option>
            {(portfolios ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </div>
        <div className="w-40">
          <label className="label">Duration (days)</label>
          <div className="flex gap-2">
            <input type="number" min={1} max={365} className="input" value={days}
              onChange={(e) => setDays(Math.max(1, Math.min(365, Number(e.target.value) || 1)))} />
            <select className="input !w-24" value=""
              onChange={(e) => e.target.value && setDays(Number(e.target.value))}>
              <option value="">quick</option>
              <option value={1}>1d</option><option value={7}>1w</option>
              <option value={14}>2w</option><option value={30}>1mo</option>
            </select>
          </div>
        </div>
        <button className="btn-primary" disabled={busy || !myPf}>Send challenge</button>
      </form>

      {incoming.length > 0 && (
        <section className="space-y-2">
          <h2 className="text-sm font-medium text-muted">Incoming challenges</h2>
          {incoming.map((c) => (
            <IncomingRow key={c.id} c={c} portfolios={portfolios ?? []} act={act} />
          ))}
        </section>
      )}

      {outgoing.length > 0 && (
        <section className="space-y-2">
          <h2 className="text-sm font-medium text-muted">Awaiting response</h2>
          {outgoing.map((c) => (
            <div key={c.id} className="card p-3 flex items-center justify-between text-sm">
              <span>You challenged <span className="font-mono">{c.opponent_username}</span> · {c.duration_days}d</span>
              <button onClick={() => act(c.id, "cancel", undefined, "Challenge cancelled")}
                className="text-xs text-muted hover:text-down">Cancel</button>
            </div>
          ))}
        </section>
      )}

      {active.length > 0 && (
        <section className="space-y-3">
          <h2 className="text-sm font-medium">Active competitions</h2>
          <div className="grid md:grid-cols-2 gap-4">
            {active.map((c) => <HeadToHeadCard key={c.id} challenge={c} />)}
          </div>
        </section>
      )}

      {finished.length > 0 && (
        <section className="space-y-3">
          <h2 className="text-sm font-medium text-muted">Finished</h2>
          <div className="grid md:grid-cols-2 gap-4">
            {finished.map((c) => <HeadToHeadCard key={c.id} challenge={c} />)}
          </div>
        </section>
      )}

      {challenges && challenges.length === 0 && (
        <div className="card">
          <EmptyState icon="⚔" title="No competitions yet"
            hint="Challenge another user above — you'll each pick a portfolio and race over your chosen window." />
        </div>
      )}
    </div>
  );
}

function IncomingRow({ c, portfolios, act }: {
  c: Challenge; portfolios: Portfolio[];
  act: (id: string, path: string, body?: object, msg?: string) => void;
}) {
  const [pf, setPf] = useState("");
  return (
    <div className="card p-3 flex flex-wrap items-center justify-between gap-2 text-sm">
      <span><span className="font-mono">{c.challenger_username}</span> challenged you · {c.duration_days}d</span>
      <div className="flex items-center gap-2">
        <select className="input !py-1 w-40 text-xs" value={pf} onChange={(e) => setPf(e.target.value)}>
          <option value="">Your portfolio…</option>
          {portfolios.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        <button disabled={!pf} onClick={() => act(c.id, "accept", { opponent_portfolio_id: pf }, "Challenge accepted!")}
          className="btn-primary !py-1 text-xs">Accept</button>
        <button onClick={() => act(c.id, "decline", undefined, "Declined")}
          className="text-xs text-muted hover:text-down px-2">Decline</button>
      </div>
    </div>
  );
}

export default function Page() {
  return <Guard><CompeteInner /></Guard>;
}
