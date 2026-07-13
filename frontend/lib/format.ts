// Currency-aware money formatting. Portfolio cash is always USD (the ledger
// base); per-ASSET prices follow the asset's quote currency (NSE → ₹).
// Number stays en-US grouping for consistency across the app.
const SYMBOL: Record<string, string> = { USD: "$", INR: "₹" };

export function ccySymbol(currency?: string): string {
  return SYMBOL[currency ?? "USD"] ?? `${currency} `;
}

export function money(value: number | string | null | undefined,
                      currency?: string, digits?: number): string {
  if (value == null) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  // Sub-unit assets (ADA at ₹/$0.17) need more precision to show movement.
  const d = digits ?? (Math.abs(n) > 0 && Math.abs(n) < 1 ? 5 : 2);
  return `${ccySymbol(currency)}${n.toLocaleString(undefined, {
    minimumFractionDigits: d, maximumFractionDigits: d })}`;
}
