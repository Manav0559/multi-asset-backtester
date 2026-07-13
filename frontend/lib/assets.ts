// Shared asset grouping: every asset picker in the app shows the universe
// bucketed by index/venue, never one flat list.

export type Asset = {
  id: number;
  symbol: string;
  exchange: string;
  asset_class: string;
  currency?: string;
};

export type AssetGroup = { label: string; items: Asset[] };

const GROUPS: { match: (a: Asset) => boolean; label: string }[] = [
  { match: (a) => a.asset_class === "us_equity", label: "NASDAQ-100 · US equities" },
  { match: (a) => a.asset_class === "in_equity" || a.asset_class === "in_index", label: "NIFTY 50 · NSE" },
  { match: (a) => a.asset_class === "crypto", label: "Crypto · Binance" },
  { match: (a) => a.asset_class === "commodity", label: "Commodities" },
];

// The three market tabs the UI navigates by. `match` doubles as the filter
// for market-scoped pickers (backtest form) and the dashboard sections.
export const MARKETS = [
  { key: "nasdaq", label: "NASDAQ-100", sub: "US equities",
    match: (a: Asset) => a.asset_class === "us_equity" },
  { key: "nifty", label: "NIFTY 50", sub: "NSE · India",
    match: (a: Asset) => a.asset_class === "in_equity" || a.asset_class === "in_index" },
  { key: "crypto", label: "Crypto", sub: "Binance · 24/7",
    match: (a: Asset) => a.asset_class === "crypto" },
] as const;

export type MarketKey = (typeof MARKETS)[number]["key"];

export function assetsOfMarket(assets: Asset[], market: MarketKey): Asset[] {
  const m = MARKETS.find((x) => x.key === market)!;
  return assets.filter(m.match).sort((a, b) => a.symbol.localeCompare(b.symbol));
}

// Default selection per market: the liveliest symbol, not the alphabetical
// first (crypto's A-first pick was ADAUSDT — sparse trades made "LIVE" look
// frozen; BTC visibly ticks).
const PREFERRED_DEFAULT: Record<MarketKey, string[]> = {
  nasdaq: ["AAPL", "NVDA", "MSFT"],
  nifty: ["RELIANCE", "TCS"],
  crypto: ["BTCUSDT", "ETHUSDT"],
};

export function defaultAssetOf(assets: Asset[], market: MarketKey): Asset | undefined {
  const scoped = assetsOfMarket(assets, market);
  for (const sym of PREFERRED_DEFAULT[market] ?? []) {
    const hit = scoped.find((a) => a.symbol === sym);
    if (hit) return hit;
  }
  return scoped[0];
}

export function groupAssets(assets: Asset[]): AssetGroup[] {
  const buckets: AssetGroup[] = GROUPS.map((g) => ({ label: g.label, items: [] }));
  const other: AssetGroup = { label: "Other", items: [] };
  for (const a of assets) {
    (GROUPS.find((g) => g.match(a)) !== undefined
      ? buckets[GROUPS.findIndex((g) => g.match(a))]
      : other
    ).items.push(a);
  }
  for (const b of buckets) b.items.sort((x, y) => x.symbol.localeCompare(y.symbol));
  other.items.sort((x, y) => x.symbol.localeCompare(y.symbol));
  return [...buckets, other].filter((g) => g.items.length > 0);
}
