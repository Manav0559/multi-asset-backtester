"""P2 container proof: one backtest per ML family in the memory-capped worker
container; record per-family peak RSS (VmHWM of the prefork child, reset by
restarting the worker between families). API = local uvicorn :8000 (current
code) sharing the docker Redis broker + DB with the containerized worker."""
import json
import subprocess
import sys
import time
import urllib.request

BACK = "http://localhost:8000"
FAMILIES = [
    "ml_logistic_regression", "ml_decision_tree", "ml_random_forest",
    "ml_extra_trees", "ml_gradient_boosting", "ml_mlp", "ml_xgboost",
]
WORKER = "backtester-worker-1"


def api(path, payload=None, token=None):
    req = urllib.request.Request(
        BACK + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def worker_restart():
    subprocess.run(["docker", "restart", WORKER], check=True, capture_output=True)
    # wait until celery children exist (pool booted)
    for _ in range(60):
        if worker_hwm_kb() > 0:
            time.sleep(2)  # let the pool finish importing
            return
        time.sleep(1)
    raise RuntimeError("worker pool did not come up")


def worker_hwm_kb():
    """Max VmHWM (peak RSS high-water mark, kB) across celery processes."""
    out = subprocess.run(
        ["docker", "exec", WORKER, "sh", "-c", "grep -H VmHWM /proc/[0-9]*/status 2>/dev/null"],
        capture_output=True, text=True).stdout
    vals = [int(line.split()[1]) for line in out.splitlines() if line]
    return max(vals, default=0)


tok = api("/auth/login", {"email": "alice@demo.backtester.dev",
                          "password": "demo-pass-123"})["access_token"]

results = []
for fam in FAMILIES:
    worker_restart()
    baseline_kb = worker_hwm_kb()  # pool import footprint before the task
    sv = api("/strategies", {"name": f"builtin: {fam}", "code": ""}, tok)
    bt = api("/backtests", {
        "strategy_version_id": sv["version_id"], "asset_id": 1,  # AAPL 5y daily
        "timeframe": "1d", "strategy": fam, "params": {},
        "initial_capital": 100000, "commission_bps": 5,
    }, tok)
    bid, status, t0 = bt["id"], "queued", time.time()
    while status in ("queued", "running") and time.time() - t0 < 420:
        time.sleep(3)
        status = api(f"/backtests/{bid}", token=tok)["status"]
    peak_kb = worker_hwm_kb()
    row = api(f"/backtests/{bid}", token=tok)
    results.append({
        "family": fam, "status": status, "secs": round(time.time() - t0, 1),
        "pool_baseline_mb": round(baseline_kb / 1024, 1),
        "peak_rss_mb": round(peak_kb / 1024, 1),
        "sharpe": row.get("sharpe"), "deflated_sharpe": row.get("deflated_sharpe"),
        "return_pct": row.get("total_return_pct"),
    })
    print(f"{fam:26s} {status:9s} {results[-1]['secs']:6.1f}s "
          f"peak={results[-1]['peak_rss_mb']:7.1f}MB "
          f"sharpe={row.get('sharpe')} dsr={row.get('deflated_sharpe')}", flush=True)

print("\nJSON:", json.dumps(results))
ok = all(r["status"] == "completed" for r in results)
cap_mb = 4096
under = all(r["peak_rss_mb"] < cap_mb for r in results)
print(f"\nall completed: {ok}; all peaks under {cap_mb}MB cap: {under}")
sys.exit(0 if ok and under else 1)
