/**
 * Captures the five README screenshots against a seeded stack (./scripts/demo.sh).
 *
 *   cd frontend && node scripts/capture-screenshots.mjs
 *
 * Env: FRONTEND_URL (default http://localhost:3005), BACKEND_URL (:8000),
 * GRAFANA_URL (:3001). Uses the installed system Chrome headlessly — no
 * browser download.
 */
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const FRONT = process.env.FRONTEND_URL ?? "http://localhost:3005";
const BACK = process.env.BACKEND_URL ?? "http://localhost:8000";
const GRAFANA = process.env.GRAFANA_URL ?? "http://localhost:3001";
const OUT = new URL("../../screenshots/", import.meta.url).pathname;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function login() {
  const res = await fetch(`${BACK}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: "alice@demo.backtester.dev", password: "demo-pass-123" }),
  });
  if (!res.ok) throw new Error(`login failed ${res.status} — did you run ./scripts/demo.sh?`);
  return res.json();
}

const main = async () => {
  mkdirSync(OUT, { recursive: true });
  const tokens = await login();

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
  }, [tokens.access_token, tokens.refresh_token]);

  // 01 — dashboard
  await page.goto(`${FRONT}/dashboard`, { waitUntil: "networkidle" });
  await page.waitForSelector(".recharts-surface");
  await sleep(1200); // entrance animations settle
  await page.screenshot({ path: `${OUT}01-dashboard.png` });
  console.log("01-dashboard.png");

  // 02 — asset chart with three indicator overlays (AAPL = asset 1)
  await page.goto(`${FRONT}/assets/1`, { waitUntil: "networkidle" });
  await page.waitForSelector("canvas"); // lightweight-charts
  const picker = page.locator("select").nth(1); // [timeframe, indicator picker]
  for (const ind of ["ema", "bbands", "rsi"]) {
    await picker.selectOption(ind);
    await sleep(1500); // overlay fetch + redraw
  }
  await sleep(1000);
  await page.screenshot({ path: `${OUT}02-chart.png` });
  console.log("02-chart.png");

  // 03 — BYOC editor with a live validation error
  await page.goto(`${FRONT}/backtests`, { waitUntil: "networkidle" });
  const strategySelect = page.locator("select").first();
  await strategySelect.selectOption("custom_code");
  await page.waitForSelector("textarea");
  const code = await page.locator("textarea").inputValue();
  await page.locator("textarea").fill(`import os\n${code}`);
  await page.getByRole("button", { name: "Validate code" }).click();
  await page.waitForSelector("text=imports are not allowed");
  await sleep(400);
  await page.screenshot({ path: `${OUT}03-byoc.png` });
  console.log("03-byoc.png");

  // 04 — leaderboard with the 24h window selected. NOTE: the page polls SWR
  // every 5s, so "networkidle" never fires — wait on selectors instead.
  await page.goto(`${FRONT}/leaderboard`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".card");
  await page.getByRole("button", { name: "24H" }).click().catch(() => {});
  await sleep(1200);
  await page.screenshot({ path: `${OUT}04-leaderboard.png` });
  console.log("04-leaderboard.png");

  // 06 — Compete: head-to-head competition (SWR polls; wait on selectors)
  await page.goto(`${FRONT}/compete`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("h1");
  await sleep(2500); // active-competition card fetches its head-to-head
  await page.screenshot({ path: `${OUT}06-compete.png` });
  console.log("06-compete.png");

  // 05 — Grafana board (anonymous Viewer is enabled in compose)
  await page.setViewportSize({ width: 1600, height: 1000 });
  // (same networkidle caveat: the board auto-refreshes every 10s)
  await page.goto(`${GRAFANA}/d/backtester-main?from=now-1h&to=now`, { waitUntil: "domcontentloaded" });
  await sleep(6000); // panels query + render
  await page.screenshot({ path: `${OUT}05-grafana.png` });
  console.log("05-grafana.png");

  await browser.close();
  console.log(`✔ screenshots written to ${OUT}`);
};

main().catch((e) => { console.error(e); process.exit(1); });
