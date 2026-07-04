/**
 * Phase-1 DoD proof: the crypto live-price element changes across a ~12s window
 * and the order-book renders rows. Run against a stack with the `ticker`
 * service connected to Binance.
 *
 *   cd frontend && node scripts/verify-live.mjs
 *
 * Exits non-zero if the price didn't move or the book never rendered.
 */
import { chromium } from "playwright";

const FRONT = process.env.FRONTEND_URL ?? "http://localhost:3005";
const BACK = process.env.BACKEND_URL ?? "http://localhost:8000";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function login() {
  const r = await fetch(`${BACK}/auth/login`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: "alice@demo.backtester.dev", password: "demo-pass-123" }),
  });
  if (!r.ok) throw new Error(`login failed ${r.status} — run ./scripts/demo.sh`);
  return r.json();
}

async function activeCryptoAssetId(token) {
  // Pick a crypto that is actually ticking right now (the box's Binance link is
  // flaky, so not every symbol updates every second). Falls back to BTC.
  const h = { Authorization: `Bearer ${token}` };
  const assets = await (await fetch(`${BACK}/assets`, { headers: h })).json();
  const cryptos = assets.filter((a) => a.asset_class === "crypto");
  for (let attempt = 0; attempt < 6; attempt++) {
    for (const a of cryptos) {
      const snap = await (await fetch(`${BACK}/market/${a.id}/snapshot`, { headers: h })).json();
      if (snap.tick?.price) return { id: a.id, symbol: a.symbol };
    }
    await sleep(2000); // wait for the flaky feed to deliver something
  }
  const btc = cryptos.find((a) => a.symbol === "BTCUSDT") ?? cryptos[0];
  return { id: btc.id, symbol: btc.symbol };
}

const main = async () => {
  const tokens = await login();
  const { id: assetId, symbol } = await activeCryptoAssetId(tokens.access_token);
  console.log(`active crypto = ${symbol} (id ${assetId})`);

  const browser = await chromium.launch({ channel: "chrome", headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto(`${FRONT}/login`, { waitUntil: "networkidle" });
  await page.evaluate(([a, r]) => {
    localStorage.setItem("bt_access", a); localStorage.setItem("bt_refresh", r);
  }, [tokens.access_token, tokens.refresh_token]);

  await page.goto(`${FRONT}/assets/${assetId}`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-testid="live-price"]');

  // Poll the price for up to 30s and record every distinct value seen — the
  // box's Binance link is flaky, so we wait for a change rather than assuming
  // one lands in a fixed 10s slot.
  const read = async () =>
    (await page.locator('[data-testid="live-price"]').innerText()).trim();
  const seen = new Set();
  let bookRendered = false;
  for (let t = 0; t < 30; t += 2) {
    const v = await read();
    if (v && v !== "…") seen.add(v);
    if (await page.locator('[data-testid="orderbook"]').count() > 0) bookRendered = true;
    if (seen.size >= 2 && bookRendered) break;
    await sleep(2000);
  }
  console.log(`distinct prices seen: ${seen.size} (${[...seen].slice(0, 3).join(", ")})`);
  console.log(`order book rendered = ${bookRendered}`);
  await browser.close();

  if (seen.size < 2) {
    console.error("FAIL: live price never changed (flaky Binance link? check ticker logs)");
    process.exit(1);
  }
  if (!bookRendered) {
    console.error("FAIL: order book did not render");
    process.exit(1);
  }
  console.log("✔ live price moved and order book rendered");
};

main().catch((e) => { console.error(e); process.exit(1); });
