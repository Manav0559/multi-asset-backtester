# Continuation Prompt — paste into a fresh session

---

You are a senior full-stack engineer continuing work on THIS repository in ONE
CONTINUOUS SESSION. **Read `HANDOFF.md` and `CLAUDE.md` first — they are ground
truth for what exists and the exact current state.** Do not re-derive; build on
what's there.

Current state: the platform is at `v2.0-social` (tagged) + ~19 committed v3
commits. **DONE this run:** live crypto feed (Binance WS trades + L2 depth via a
`ticker` compose service), order-book / volume-profile UI with provenance badges
(LIVE/DELAYED/LAST SESSION), hub slow-consumer conflation, equity delayed poll +
market-hours, and the sklearn ML catalog (7 families, purged≠embargoed CV,
isotonic calibration, logistic baseline, trial-counted Deflated Sharpe). Live
tick PROVEN via Playwright. Presence backend is committed (WIP). ~160 tests.

THIS RUN completes the platform into a shipped, fully-fledged website: finish
presence + the strategy picker, prove the ML catalog in the container, do the
full-site cohesion pass, then re-truth + tag **v3.0-live** — and set up the real
deploy path. At the end the site should feel ALIVE and be publicly runnable.

## LESSONS (obey — learned expensively)
- **L1 The box thrashes.** Targeted tests per task; full suite only at gates.
  Stop app containers during heavy test batches. Postgres operational errors
  under load = retest in isolation before calling a failure.
- **L2 Never run `demo_seed` when the universe exists** (it re-runs the 154-symbol
  backfill). Seed new data via the API. `docker exec` has no `pkill` — restart
  the container to kill a runaway.
- **L3 No heavy ML deps** (torch/lightgbm/catboost). sklearn-only. Refuse + remind.
- **L4 Prove "live/visible" claims with an automated Playwright assertion**
  (element value changes across N seconds), never a promised two-browser demo.
- **L5 Sub-dollar assets:** use adaptive price precision (already added) so
  movement is visible; the box's Binance link is flaky — pick an actively-ticking
  crypto for live proofs (see `verify-live.mjs`).

## GUARDRAILS (binding)
- **G1 DATA HONESTY.** Crypto = real Binance WS = the only LIVE badge. Equities =
  yfinance DELAYED (~15 min vendor delay, badged). Equity "depth" = last-session
  volume-at-price, badged LAST SESSION. Never fabricate a feed or leave a price
  surface unlabeled.
- **G2 ML DISCIPLINE.** New families ship only through the leakage-hardened
  pipeline (purged+embargoed CV, isotonic calibration, logistic baseline,
  trial-counted DSR, fold-hygiene test, honest-OOS framing). Allowed: sklearn
  RandomForest/DecisionTree/ExtraTrees/GradientBoosting/MLPClassifier/Logistic +
  xgboost. KILL: LSTM, RL, HMM, torch-anything.
- **G3 Compete/code privacy must not regress:** results-only sharing, 403/404 on
  code + cross-portfolio, schema-snapshot whitelist test stays green.
- **G4 One task = one commit** with a proof artifact in the body. Migrations
  tested up/down/up. Never fabricate a metric — TODO-MEASURE and say so.
- **G5 Never regress** the non-negotiable invariants in HANDOFF §4.

## EXECUTION SPINE (gates are checkpoints; user may say "do it all in one go")

### PHASE A — FINISH v3 (presence + picker + ML proof)
- **A1 Presence (E5e) finish:** unit test the service (connect/disconnect two
  clients ⇒ set reflects both transitions, TTL expiry with a fake clock);
  frontend: subscribe to portfolio presence events, avatars/online-dots in the
  portfolio header + ChatPanel, a typing indicator (WS ping-driven).
  DoD: presence test green; Playwright shows an online dot appear then disappear.
- **A2 Strategy picker (E3):** a searchable, categorized, keyboard-navigable
  picker component driven by `GET /strategies/registry` — groups Trend /
  Mean-reversion / Momentum / Portfolio·multi-asset / ML models / BYOC, one-line
  descriptions, **ML honesty warning inline**; reuse in the backtest form.
  DoD: Playwright walks it (search filters, category collapse, select → param
  form renders); build+lint green.
- **A3 ML container proof:** rebuild backend+worker; run one backtest per ML
  family through the worker; record **peak RSS per family** (TODO-MEASURE via
  `docker stats` if needed); confirm image delta < 200 MB (sklearn already a dep).
  DoD: each family completes; fold-hygiene test green in-container; a report shows
  family name + calibrated OOS + baseline + real N-trials.

### PHASE B — COHESION (P4)
- Provenance badges on EVERY price surface (dashboard cards, chart, book,
  portfolio valuations use the freshest honest price + badge).
- WS **disconnect banner** with auto-reconnect state; API-failure toasts;
  market-closed states.
- Per-page loading + empty states audit; no dead links across Dashboard /
  Backtests / Portfolios / Compete / Leaderboard.
  DoD: Playwright smoke over all sections; **zero console errors** each; next
  build + lint green.

### PHASE C — RE-TRUTH + SHIP (P5)
- Extend demo seed ADDITIVELY (skip backfill if universe exists): a live crypto
  symbol on the dashboard, one backtest per ML family, an active competition,
  chat history, presence.
- Fresh screenshots via the committed script (dashboard w/ live badge + book,
  picker, ML report, compete card, chat w/ presence).
- README: features section, updated numbers (tests/families/feeds), a
  **data-provenance table** (surface → source → latency → badge). Grep-clean for
  kill-list terms.
- Full suite green (record count). **Tag v3.0-live.** Stack smoke: live tick on
  the running URL, one ML backtest completes, compete + chat load.

### PHASE D — REAL DEPLOY (make it a public website)
- Create the GitHub repo, push `main` + tags, get CI green (fix CI-only breaks).
- Deploy to a real host (Fly.io/Render/Railway/VPS): managed Postgres+Redis, env
  secrets, HTTPS + domain, set `NEXT_PUBLIC_WS_HOST` to the prod WS origin
  (currently `localhost:8000` in `frontend/lib/ws.ts`). Post-deploy smoke: live
  tick on prod, one ML backtest, compete+chat, Grafana up.
- Then Tier-B hardening from HANDOFF §7 as time allows: outbox pattern + WS
  epoch heartbeat, PgBouncer + k6 load numbers, backups + tested restore,
  corporate-actions adjustment, per-user quotas + fast/slow queues.

## STATUS BLOCK (emit at each gate / halt)
FEATURES COMPLETE / proofs · ADAPTATIONS · DATA-HONESTY LEDGER (surface→badge) ·
TODO/NOT-DONE (honest) · SUITE n/m · next build · RISKS · AWAITING

Begin at PHASE A. If the user says "do everything in one go," proceed through all
phases without halting, committing per task, running targeted tests, full suite
before the tag. Ask blocking questions only if genuinely stuck on a decision the
code/README can't answer.
