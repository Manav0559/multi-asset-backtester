import { execSync, spawn } from "node:child_process";
import { chromium } from "playwright";

const FRONT = "http://localhost:3010";
const BACK = "http://localhost:8000";
const PID = "70aa3a1a-eed0-43b7-a4e5-94c1ae9c96f8"; // The Syndicate (alice+bob)
const ADA = 253; // actively-ticking crypto
const BACKEND_DIR = "/Users/manavsingla/Desktop/Backtester/backend";

async function login(email) {
  const r = await fetch(`${BACK}/auth/login`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password: "demo-pass-123" }),
  });
  return r.json();
}

const alice = await login("alice@demo.backtester.dev");
const bob = await login("bob@demo.backtester.dev");

// Ensure the portfolio has a position + >=2 equity points so the valuation
// surfaces (equity chart, positions table) actually render. Idempotent-ish:
// a tiny ADA buy; alice may be viewer, fall back to bob.
async function placeOrder(tok) {
  const r = await fetch(`${BACK}/portfolios/${PID}/orders`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${tok}` },
    body: JSON.stringify({ asset_id: ADA, side: "buy", qty: "5" }),
  });
  return r.status;
}
let st = await placeOrder(alice.access_token);
if (st === 403 || st === 404) st = await placeOrder(bob.access_token);
console.log("(setup) seed order status:", st);

const browser = await chromium.launch({ channel: "chrome", headless: true });
const ctx = await browser.newContext();
await ctx.addInitScript(([a, rf]) => {
  localStorage.setItem("bt_access", a);
  localStorage.setItem("bt_refresh", rf);
}, [alice.access_token, alice.refresh_token]);
const page = await ctx.newPage();

let bounceWindow = false;
const errors = [];
const NETWORK_NOISE = /Failed to fetch|Failed to load resource|ERR_CONNECTION|ERR_INTERNET|WebSocket|NetworkError|Load failed/i;
page.on("console", (m) => {
  if (m.type() !== "error") return;
  const txt = `${m.text()} @ ${m.location().url}`;
  if (bounceWindow && NETWORK_NOISE.test(txt)) return; // we killed the server on purpose
  errors.push(txt);
});
page.on("pageerror", (e) => {
  if (bounceWindow && NETWORK_NOISE.test(e.message)) return;
  errors.push(`pageerror: ${e.message}`);
});

const results = {};
const body = () => page.evaluate(() => document.body.innerText);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitFor(fn, timeoutMs = 15000) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    if (await fn()) return true;
    await sleep(300);
  }
  return false;
}
const waitText = (s, t = 15000) => waitFor(async () => (await body()).includes(s), t);

// ---- 1. Dashboard -----------------------------------------------------------
await page.goto(`${FRONT}/dashboard`, { waitUntil: "domcontentloaded", timeout: 60000 });
results["dash: tabs render"] = await waitText("NASDAQ-100");
results["dash: price card carries provenance badge"] =
  await waitFor(async () => /LIVE|DELAYED ~15m|LAST SESSION/.test(await body()), 30000);
await page.click('button:has-text("Crypto")');
await waitText("ADAUSDT");
await page.click('button:has-text("ADAUSDT")');
results["dash: crypto shows LIVE badge"] =
  await waitFor(async () => (await body()).includes("LIVE"), 15000);

// ---- 2. Asset page ----------------------------------------------------------
await page.goto(`${FRONT}/assets/${ADA}`, { waitUntil: "domcontentloaded", timeout: 60000 });
results["asset: page + LIVE badge"] = await waitText("LIVE", 15000);
results["asset: order book renders"] =
  await waitFor(async () => /Order book|Bids|Asks/i.test(await body()), 10000);

// ---- 3. Backtests -----------------------------------------------------------
await page.goto(`${FRONT}/backtests`, { waitUntil: "domcontentloaded", timeout: 60000 });
await page.locator('[data-testid="strategy-picker"]').waitFor({ timeout: 20000 });
results["backtests: picker present"] = true;
results["backtests: ML results visible"] = await waitText("ml_xgboost", 20000);

// ---- 4. Portfolio: presence, valuation badges, chat, REAL WS bounce ---------
await page.goto(`${FRONT}/portfolios/${PID}`, { waitUntil: "domcontentloaded", timeout: 60000 });
await waitText("The Syndicate");
results["portfolio: presence roster"] = await waitText("1 online", 12000);
results["portfolio: valuation badges"] = await waitText("MARKED AT LAST CLOSE", 30000);
results["portfolio: positions honesty chip"] = await waitText("AVG ENTRY", 8000);
results["portfolio: chat panel"] = (await body()).includes("Team chat");

// Kill the backend -> every hub socket drops -> banner. Restart -> it clears.
bounceWindow = true;
// -sTCP:LISTEN: plain `lsof -ti :8000` also matches CLIENTS of the port
// (next dev proxy sockets!) — killing those took the frontend down with it.
execSync("lsof -ti :8000 -sTCP:LISTEN | xargs kill -9 2>/dev/null || true", { shell: "/bin/zsh" });
results["ws banner appears when link drops"] =
  await waitFor(async () => (await page.locator('[data-testid="ws-banner"]').count()) > 0, 15000);

const uv = spawn(`${BACKEND_DIR}/.venv/bin/uvicorn`, ["app.main:app", "--port", "8000"],
  { cwd: BACKEND_DIR, detached: true, stdio: "ignore" });
uv.unref();
await waitFor(async () => {
  try { return (await fetch(`${BACK}/health`)).ok; } catch { return false; }
}, 30000);
results["ws banner clears after reconnect"] =
  await waitFor(async () => (await page.locator('[data-testid="ws-banner"]').count()) === 0, 20000);
await sleep(1500); // let in-flight failed fetches settle before unmuting
bounceWindow = false;

// ---- 5. Leaderboard + Compete ------------------------------------------------
await page.goto(`${FRONT}/leaderboard`, { waitUntil: "domcontentloaded", timeout: 60000 });
results["leaderboard: renders + badge"] =
  (await waitText("Leaderboard")) && (await waitText("MARKED AT LAST CLOSE", 8000));
await page.goto(`${FRONT}/compete`, { waitUntil: "domcontentloaded", timeout: 60000 });
results["compete: renders + badge"] =
  (await waitText("Compete")) && (await waitText("MARKED AT LAST CLOSE", 8000));

let pass = true;
for (const [k, v] of Object.entries(results)) {
  console.log(`${v ? "✓" : "✗"} ${k}`);
  if (!v) pass = false;
}
console.log("\nconsole/page errors (bounce-window network noise excluded):", errors.length);
for (const e of errors) console.log("  -", e);
await browser.close();
console.log("\nRESULT:", pass && errors.length === 0 ? "PASS ✅" : "FAIL ❌");
process.exit(pass && errors.length === 0 ? 0 : 1);
