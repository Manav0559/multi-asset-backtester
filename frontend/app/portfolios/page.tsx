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

function PortfoliosInner() {
  const [portfolios, setPortfolios] = useState<Portfolio[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  const [name, setName] = useState("");
  const [cash, setCash] = useState("100000");
  const toast = useToast();

  function load() {
    api<Portfolio[]>("/portfolios").then(setPortfolios);
  }
  useEffect(load, []);

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
        <button className="btn-primary" onClick={() => setShowCreate((s) => !s)}>
          + New portfolio
        </button>
      </div>

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
