"use client";

/**
 * Type-ahead asset combobox — replaces the giant scrolling <select>. Filter by
 * symbol, keyboard-navigable, grouped by market, with an honest "No such
 * instrument" empty state. Purely presentational: the caller owns the asset
 * list and the selected id.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Asset, groupAssets } from "@/lib/assets";

export default function AssetPicker({ assets, value, onChange, placeholder }: {
  assets: Asset[];
  value: number | null;
  onChange: (id: number) => void;
  placeholder?: string;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [highlight, setHighlight] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const selected = assets.find((a) => a.id === value);

  const { groups, flat } = useMemo(() => {
    const q = query.trim().toLowerCase();
    const hit = (a: Asset) => !q || a.symbol.toLowerCase().includes(q);
    const filtered = assets.filter(hit);
    const groups = groupAssets(filtered);
    return { groups, flat: groups.flatMap((g) => g.items) };
  }, [assets, query]);

  useEffect(() => {
    const idx = flat.findIndex((a) => a.id === value);
    setHighlight(idx >= 0 ? idx : 0);
  }, [open, flat, value]);

  useEffect(() => { if (open) inputRef.current?.focus(); }, [open]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  useEffect(() => {
    if (open) listRef.current?.querySelector(`[data-idx="${highlight}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [highlight, open]);

  function choose(id: number) {
    onChange(id);
    setOpen(false);
    setQuery("");
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (!open) {
      if (["ArrowDown", "Enter", " "].includes(e.key)) { e.preventDefault(); setOpen(true); }
      return;
    }
    if (e.key === "ArrowDown") { e.preventDefault(); setHighlight((h) => Math.min(h + 1, flat.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setHighlight((h) => Math.max(h - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); if (flat[highlight]) choose(flat[highlight].id); }
    else if (e.key === "Escape") { e.preventDefault(); setOpen(false); setQuery(""); }
  }

  return (
    <div ref={rootRef} className="relative" onKeyDown={onKeyDown}>
      <button type="button" data-testid="asset-picker" aria-haspopup="listbox" aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="input flex items-center justify-between gap-2 text-left w-full">
        <span className="font-mono truncate">{selected?.symbol ?? placeholder ?? "Pick an instrument"}</span>
        <span className="text-muted text-xs shrink-0">▾</span>
      </button>

      {open && (
        <div className="absolute z-50 mt-2 w-72 max-w-[90vw] card p-0 overflow-hidden
                        shadow-glass-lift !bg-[#0d1322]/[0.97]">
          <div className="p-2 border-b border-border">
            <input ref={inputRef} className="input !py-1.5 text-sm" data-testid="asset-search"
              placeholder="Type a symbol…" value={query}
              onChange={(e) => setQuery(e.target.value)} />
          </div>
          <div ref={listRef} role="listbox" className="max-h-72 overflow-y-auto py-1">
            {flat.length === 0 && (
              <p className="text-muted text-xs text-center py-6" data-testid="asset-empty">
                No such instrument{query ? ` — “${query}”` : ""}
              </p>
            )}
            {groups.map((g) => (
              <div key={g.label}>
                <div className="px-3 pt-2 pb-1 text-[10px] uppercase tracking-wider text-muted">
                  {g.label}
                </div>
                {g.items.map((a) => {
                  const idx = flat.indexOf(a);
                  return (
                    <div key={a.id} role="option" aria-selected={a.id === value} data-idx={idx}
                      onMouseEnter={() => setHighlight(idx)} onClick={() => choose(a.id)}
                      className={`px-3 py-1.5 cursor-pointer flex items-baseline gap-2 ${
                        idx === highlight ? "bg-accent/10" : ""
                      } ${a.id === value ? "border-l-2 border-accent" : "border-l-2 border-transparent"}`}>
                      <span className="font-mono text-sm">{a.symbol}</span>
                      <span className="text-[11px] text-muted">{a.exchange}</span>
                    </div>
                  );
                })}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
