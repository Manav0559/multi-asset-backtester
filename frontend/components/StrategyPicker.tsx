"use client";

/**
 * E3 — searchable, categorized, keyboard-navigable strategy picker.
 *
 * Entirely registry-driven (the frontend still holds NO hardcoded strategy
 * list): categories come from each entry's `category` field; CATEGORY_META is
 * presentation-only and unknown categories fall back to their raw id, so new
 * backend strategies/categories appear here on deploy with zero UI edits.
 *
 * Keyboard: ArrowDown/Enter/Space open; ArrowUp/Down move the highlight over
 * the FILTERED list; Enter selects; Escape closes; typing filters live.
 */
import { useEffect, useMemo, useRef, useState } from "react";

export type StrategyEntry = {
  key: string;
  kind: "single" | "portfolio";
  category: string;
  description: string;
  defaults: Record<string, number | string | null>;
};

const CATEGORY_META: Record<string, { label: string; badge: string }> = {
  trend: { label: "Trend following", badge: "bg-accent/15 text-accent" },
  mean_reversion: { label: "Mean reversion", badge: "bg-purple-500/15 text-purple-300" },
  arbitrage: { label: "Arbitrage · stat-arb", badge: "bg-amber-500/15 text-amber-300" },
  baseline: { label: "Baselines", badge: "bg-slate-500/20 text-slate-300" },
  ml: { label: "Machine learning", badge: "bg-emerald-500/15 text-emerald-300" },
  custom: { label: "Bring your own code", badge: "bg-pink-500/15 text-pink-300" },
};
const CATEGORY_ORDER = ["trend", "mean_reversion", "arbitrage", "baseline", "ml", "custom"];

export function categoryBadge(category: string): string {
  return CATEGORY_META[category]?.badge ?? "bg-slate-600/20 text-slate-300";
}

export default function StrategyPicker({ strategies, value, onChange }: {
  strategies: StrategyEntry[];
  value: string;
  onChange: (key: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [highlight, setHighlight] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const selected = strategies.find((s) => s.key === value);

  // Filter, then group in a stable category order; keep a flat list for
  // arrow-key navigation across group boundaries.
  const { groups, flat } = useMemo(() => {
    const q = query.trim().toLowerCase();
    const hit = (s: StrategyEntry) =>
      !q || s.key.toLowerCase().includes(q) || s.description.toLowerCase().includes(q) ||
      s.category.toLowerCase().includes(q);
    const filtered = strategies.filter(hit);
    const cats = [
      ...CATEGORY_ORDER.filter((c) => filtered.some((s) => s.category === c)),
      ...[...new Set(filtered.map((s) => s.category))].filter((c) => !CATEGORY_ORDER.includes(c)),
    ];
    const groups = cats.map((c) => ({
      id: c,
      label: CATEGORY_META[c]?.label ?? c,
      items: filtered.filter((s) => s.category === c),
    }));
    return { groups, flat: groups.flatMap((g) => g.items) };
  }, [strategies, query]);

  // Reset the highlight to the selected entry (or the top) whenever the
  // dropdown opens or the filter changes.
  useEffect(() => {
    const idx = flat.findIndex((s) => s.key === value);
    setHighlight(idx >= 0 ? idx : 0);
  }, [open, flat, value]);

  useEffect(() => {
    if (open) searchRef.current?.focus();
  }, [open]);

  // Close on outside click.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  // Keep the highlighted option scrolled into view.
  useEffect(() => {
    if (!open) return;
    listRef.current?.querySelector(`[data-idx="${highlight}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [highlight, open]);

  function choose(key: string) {
    onChange(key);
    setOpen(false);
    setQuery("");
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (!open) {
      if (["ArrowDown", "Enter", " "].includes(e.key)) {
        e.preventDefault();
        setOpen(true);
      }
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlight((h) => Math.min(h + 1, flat.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlight((h) => Math.max(h - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (flat[highlight]) choose(flat[highlight].key);
    } else if (e.key === "Escape") {
      e.preventDefault();
      setOpen(false);
      setQuery("");
    }
  }

  return (
    <div ref={rootRef} className="relative" onKeyDown={onKeyDown}>
      <button
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        data-testid="strategy-picker"
        onClick={() => setOpen((o) => !o)}
        className="input flex items-center justify-between gap-2 text-left w-full"
      >
        <span className="flex items-center gap-2 min-w-0">
          <span className="font-mono truncate">{selected?.key ?? "Pick a strategy"}</span>
          {selected && (
            <span className={`px-1.5 py-0.5 rounded text-[10px] shrink-0 ${categoryBadge(selected.category)}`}>
              {CATEGORY_META[selected.category]?.label ?? selected.category}
            </span>
          )}
        </span>
        <span className="text-muted text-xs shrink-0">▾</span>
      </button>

      {open && (
        <div className="absolute z-50 mt-2 w-[26rem] max-w-[90vw] card p-0 overflow-hidden shadow-glass-lift">
          <div className="p-2 border-b border-border">
            <input
              ref={searchRef}
              className="input !py-1.5 text-sm"
              placeholder={`Search ${strategies.length} strategies…`}
              value={query}
              data-testid="strategy-search"
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
          <div ref={listRef} role="listbox" aria-label="Strategies"
               className="max-h-80 overflow-y-auto py-1">
            {flat.length === 0 && (
              <p className="text-muted text-xs text-center py-6">No strategies match “{query}”</p>
            )}
            {groups.map((g) => (
              <div key={g.id}>
                <div className="px-3 pt-2 pb-1 flex items-baseline gap-2">
                  <span className="text-[10px] uppercase tracking-wider text-muted">{g.label}</span>
                  {g.id === "ml" && (
                    <span className="text-[10px] text-emerald-300/70">
                      walk-forward out-of-sample · calibrated · vs logistic baseline
                    </span>
                  )}
                </div>
                {g.items.map((s) => {
                  const idx = flat.indexOf(s);
                  return (
                    <div
                      key={s.key}
                      role="option"
                      aria-selected={s.key === value}
                      data-idx={idx}
                      onMouseEnter={() => setHighlight(idx)}
                      onClick={() => choose(s.key)}
                      className={`px-3 py-1.5 cursor-pointer flex items-baseline gap-2 ${
                        idx === highlight ? "bg-accent/10" : ""
                      } ${s.key === value ? "border-l-2 border-accent" : "border-l-2 border-transparent"}`}
                    >
                      <span className="font-mono text-sm shrink-0">{s.key}</span>
                      {s.kind === "portfolio" && (
                        <span className="px-1 rounded bg-indigo-500/15 text-indigo-300 text-[10px] shrink-0">
                          multi-asset
                        </span>
                      )}
                      <span className="text-xs text-muted truncate">{s.description}</span>
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

/** Honesty warning shown whenever an ML strategy is selected. The numbers are
 * already honest server-side (purged CV, calibration, trial-counted Deflated
 * Sharpe); this makes the *interpretation* honest too. */
export function MlHonestyNote() {
  return (
    <div data-testid="ml-honesty" className="rounded-lg border border-emerald-500/25
         bg-emerald-500/[0.07] px-3 py-2 text-xs leading-relaxed text-emerald-100/90">
      <span className="font-medium text-emerald-300">ML backtest honesty: </span>
      trained walk-forward with purged + embargoed CV (no leakage), probabilities
      isotonic-calibrated per fold, and always compared against a plain logistic
      baseline. The Deflated Sharpe counts every trial ever run on this family +
      asset — so a high raw Sharpe with a low deflated one means overfitting, not
      alpha. Expect modest numbers; distrust spectacular ones.
    </div>
  );
}
