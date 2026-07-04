"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import Guard from "@/components/Guard";
import { EmptyState } from "@/components/ui";
import { useToast } from "@/components/ToastProvider";
import { api } from "@/lib/api";

type Portfolio = {
  id: string; name: string; cash_balance: string; initial_cash: string;
  version: number; base_currency: string;
};
type PendingInvite = {
  token: string; portfolio_name: string; inviter_username: string; role: string;
};

function PortfoliosInner() {
  const [portfolios, setPortfolios] = useState<Portfolio[]>([]);
  const [invites, setInvites] = useState<PendingInvite[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  const [name, setName] = useState("");
  const [cash, setCash] = useState("100000");
  const toast = useToast();

  function load() {
    api<Portfolio[]>("/portfolios").then(setPortfolios);
    api<PendingInvite[]>("/portfolios/invites/pending").then(setInvites).catch(() => {});
  }
  useEffect(load, []);

  async function respondInvite(token: string, action: "accept" | "decline", pname: string) {
    try {
      await api(`/portfolios/invites/${action}`, { method: "POST", body: JSON.stringify({ token }) });
      toast.success(action === "accept" ? `Joined ${pname}` : "Invite declined");
      load();
    } catch (e: any) { toast.error(e.message); }
  }

  async function create(e: React.FormEvent) {
    e.preventDefault();
    try {
      await api("/portfolios", {
        method: "POST",
        body: JSON.stringify({ name, initial_cash: Number(cash) }),
      });
      toast.success(`Portfolio “${name}” created`);
      setShowCreate(false);
      setName("");
      load();
    } catch (e: any) {
      toast.error(e.message || "Could not create portfolio");
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Portfolios</h1>
          <p className="text-sm text-muted">Shared multiplayer paper-trading accounts</p>
        </div>
        <button className="btn-primary" data-create onClick={() => setShowCreate((s) => !s)}>
          + New portfolio
        </button>
      </div>

      {invites.length > 0 && (
        <div className="card p-5 space-y-2 border-accent/30">
          <h2 className="text-sm font-medium text-accent">Portfolio invites</h2>
          {invites.map((inv) => (
            <div key={inv.token} className="flex flex-wrap items-center justify-between gap-2 text-sm">
              <span>
                <span className="font-mono">{inv.inviter_username}</span> invited you to{" "}
                <span className="font-medium">{inv.portfolio_name}</span>{" "}
                <span className="text-muted text-xs">as {inv.role}</span>
              </span>
              <div className="flex gap-2">
                <button onClick={() => respondInvite(inv.token, "accept", inv.portfolio_name)}
                  className="btn-primary !py-1 text-xs">Accept</button>
                <button onClick={() => respondInvite(inv.token, "decline", inv.portfolio_name)}
                  className="text-xs text-muted hover:text-down px-2">Decline</button>
              </div>
            </div>
          ))}
        </div>
      )}

      {showCreate && (
        <form onSubmit={create} className="card p-5 flex flex-wrap items-end gap-4">
          <div className="flex-1 min-w-[180px]">
            <label className="label">Name</label>
            <input className="input" value={name} required
              onChange={(e) => setName(e.target.value)} placeholder="Team Alpha" />
          </div>
          <div className="w-48">
            <label className="label">Initial cash</label>
            <input className="input" type="number" value={cash} min={1}
              onChange={(e) => setCash(e.target.value)} />
          </div>
          <button className="btn-primary">Create</button>
        </form>
      )}

      <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-4">
        {portfolios.map((p) => {
          const pnl = Number(p.cash_balance) - Number(p.initial_cash);
          return (
            <Link key={p.id} href={`/portfolios/${p.id}`}
              className="card p-5 hover:border-accent transition-colors">
              <div className="flex items-center justify-between mb-3">
                <h3 className="font-medium">{p.name}</h3>
                <span className="text-xs text-muted">v{p.version}</span>
              </div>
              <p className="text-2xl font-mono font-semibold">
                ${Number(p.cash_balance).toLocaleString()}
              </p>
              <p className={`text-sm mt-1 ${pnl >= 0 ? "text-up" : "text-down"}`}>
                {pnl >= 0 ? "+" : ""}${pnl.toLocaleString()} cash vs start
              </p>
            </Link>
          );
        })}
        {portfolios.length === 0 && (
          <div className="card md:col-span-2 lg:col-span-3">
            <EmptyState icon="◈" title="No portfolios yet"
              hint="Create one to start paper trading — invite collaborators to share the same cash balance." />
          </div>
        )}
      </div>
    </div>
  );
}

export default function PortfoliosPage() {
  return <Guard><PortfoliosInner /></Guard>;
}
