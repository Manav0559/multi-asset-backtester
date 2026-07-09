/**
 * Captures the README screenshots against a seeded stack (./scripts/demo.sh).
 *
 *   cd frontend && node scripts/capture-screenshots.mjs
 *
 * Env: FRONTEND_URL (default http://localhost:3005), BACKEND_URL (:8000),
 * GRAFANA_URL (:3001). Uses the installed system Chrome headlessly — no
 * browser download. NOTE: leaderboard/compete/Grafana poll continuously, so
 * `networkidle` never fires there — wait on selectors with domcontentloaded.
 */
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const FRONT = process.env.FRONTEND_URL ?? "http://localhost:3005";
const BACK = process.env.BACKEND_URL ?? "http://localhost:8000";
const GRAFANA = process.env.GRAFANA_URL ?? "http://localhost:3001";
const OUT = new URL("../../screenshots/", import.meta.url).pathname;
const WS_HOST = BACK.replace(/^https?:\/\//, "");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function login(email) {
  const res = await fetch(`${BACK}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password: "demo-pass-123" }),
  });
  if (!res.ok) throw new Error(`login failed ${res.status} — did you run ./scripts/demo.sh?`);
  return res.json();
}

async function assetIdOf(symbol, token) {
  const res = await fetch(`${BACK}/assets`, { headers: { Authorization: `Bearer ${token}` } });
  const a = (await res.json()).find((x) => x.symbol === symbol);
  if (!a) throw new Error(`${symbol} not in the universe — run the seed first`);
  return a.id;
}

async function syndicateId(token) {
  const res = await fetch(`${BACK}/portfolios`, { headers: { Authorization: `Bearer ${token}` } });
  const p = (await res.json()).find((x) => x.name === "The Syndicate");
  if (!p) throw new Error("The Syndicate missing — run the seed first");
  return p.id;
}

const main = async () => {
  mkdirSync(OUT, { recursive: true });
  const alice = await login("alice@demo.backtester.dev");
  const bob = await login("bob@demo.backtester.dev");
  const ada = await assetIdOf("ADAUSDT", alice.access_token);
  const aapl = await assetIdOf("AAPL", alice.access_token);

  const browser = await chromium.launch({ channel: "chrome", headless: true });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2, // retina-crisp PNGs
  });

  // Establish origin, then inject tokens the way lib/auth.ts stores them.
  await page.goto(`${FRONT}/login`, { waitUntil: "networkidle" });
  await page.evaluate(([a, r]) => {
    localStorage.setItem("bt_access", a);
    localStorage.setItem("bt_refresh", r);
  }, [alice.access_token, alice.refresh_token]);

  // 01 — dashboard on the crypto tab: LIVE-badged streamed price
  await page.goto(`${FRONT}/dashboard`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".recharts-surface", { timeout: 30000 });
  await page.click('button:has-text("Crypto")');
  await page.click('button:has-text("ADAUSDT")');
  await page.waitForSelector("text=LIVE", { timeout: 20000 });
  await sleep(1500); // entrance animations + a couple of ticks
  await page.screenshot({ path: `${OUT}01-dashboard.png` });
  console.log("01-dashboard.png");

  // 02 — asset chart with three indicator overlays (AAPL)
  await page.goto(`${FRONT}/assets/${aapl}`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("canvas", { timeout: 30000 }); // lightweight-charts
  const indPicker = page.locator("select").nth(1); // [timeframe, indicator picker]
  for (const ind of ["ema", "bbands", "rsi"]) {
    await indPicker.selectOption(ind);
    await sleep(1500); // overlay fetch + redraw
  }
  await sleep(1000);
  await page.screenshot({ path: `${OUT}02-chart.png` });
  console.log("02-chart.png");

  // 03 — BYOC editor with a live validation error (picker is a combobox now)
  await page.goto(`${FRONT}/backtests`, { waitUntil: "domcontentloaded" });
  await page.click('[data-testid="strategy-picker"]');
  await page.fill('[data-testid="strategy-search"]', "custom");
  await page.keyboard.press("Enter");
  await page.waitForSelector("textarea");
  const code = await page.locator("textarea").inputValue();
  await page.locator("textarea").fill(`import os\n${code}`);
  await page.getByRole("button", { name: "Validate code" }).click();
  await page.waitForSelector("text=imports are not allowed");
  await sleep(400);
  await page.screenshot({ path: `${OUT}03-byoc.png` });
  console.log("03-byoc.png");

  // 04 — leaderboard with the 24h window (badge: MARKED AT LAST CLOSE)
  await page.goto(`${FRONT}/leaderboard`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".card");
  await page.getByRole("button", { name: "24H" }).click().catch(() => {});
  await sleep(1200);
  await page.screenshot({ path: `${OUT}04-leaderboard.png` });
  console.log("04-leaderboard.png");

  // 06 — Compete: head-to-head competition
  await page.goto(`${FRONT}/compete`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("h1");
  await sleep(2500); // active-competition card fetches its head-to-head
  await page.screenshot({ path: `${OUT}06-compete.png` });
  console.log("06-compete.png");

  // 07 — live order book + trade tape (ADA: sub-dollar precision shows movement)
  await page.goto(`${FRONT}/assets/${ada}`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("text=LIVE", { timeout: 20000 });
  await sleep(4000); // let the ladder + tape fill with real frames
  await page.screenshot({ path: `${OUT}07-orderbook.png` });
  console.log("07-orderbook.png");

  // 08 — strategy picker open: categorized, searchable, ML group w/ OOS note
  await page.goto(`${FRONT}/backtests`, { waitUntil: "domcontentloaded" });
  await page.click('[data-testid="strategy-picker"]');
  await page.waitForSelector('[data-testid="strategy-search"]');
  await sleep(600);
  await page.screenshot({ path: `${OUT}08-picker.png` });
  console.log("08-picker.png");

  // 09 — ML honesty: ml_xgboost selected -> inline warning under the form
  await page.fill('[data-testid="strategy-search"]', "ml_xgboost");
  await page.keyboard.press("Enter");
  await page.waitForSelector('[data-testid="ml-honesty"]');
  await sleep(500);
  await page.screenshot({ path: `${OUT}09-ml-honesty.png` });
  console.log("09-ml-honesty.png");

  // 10 — team chat + presence avatars (+ typing): bob joins over a raw WS
  const pid = await syndicateId(alice.access_token);
  const bobWs = new WebSocket(`ws://${WS_HOST}/ws?token=${bob.access_token}`);
  await new Promise((res) => { bobWs.onopen = res; });
  const typer = setInterval(
    () => bobWs.send(JSON.stringify({ action: "typing", portfolio: pid })), 1000);
  await page.goto(`${FRONT}/portfolios/${pid}`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("text=2 online", { timeout: 20000 });
  await page.waitForSelector("text=bob_demo is typing", { timeout: 15000 });
  await sleep(400);
  await page.screenshot({ path: `${OUT}10-chat-presence.png` });
  console.log("10-chat-presence.png");
  clearInterval(typer);
  bobWs.close();

  // 05 — Grafana board (anonymous Viewer is enabled in compose)
  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.goto(`${GRAFANA}/d/backtester-main?from=now-1h&to=now`, { waitUntil: "domcontentloaded" });
  await sleep(6000); // panels query + render
  await page.screenshot({ path: `${OUT}05-grafana.png` });
  console.log("05-grafana.png");

  await browser.close();
  console.log(`✔ screenshots written to ${OUT}`);
};

main().catch((e) => { console.error(e); process.exit(1); });
