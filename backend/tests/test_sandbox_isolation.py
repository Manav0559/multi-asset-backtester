"""BYOC sandbox isolation: user code runs in a separate, limited process.

Pinned here:
  - a runaway loop (in generate() or in the class body) is killed by the
    wall-clock timeout instead of hanging the caller;
  - file and network I/O is rejected at every layer: the AST gate, the
    restricted pd/np namespaces, and the child's runtime audit hook;
  - the web process's environment (DATABASE_URL, JWT_SECRET, ...) never
    reaches the child;
  - a legitimate strategy still produces the same weights as in-process;
  - CUSTOM_STRATEGIES_ENABLED=false switches the feature off at the API.
"""
import time
import uuid

import numpy as np
import pandas as pd
import pytest

from app.backtest import sandbox
from app.backtest.sandbox import (
    DEFAULT_TEMPLATE,
    SAFE_NP,
    SAFE_PD,
    SandboxDisabledError,
    SandboxError,
    build_custom_strategy,
    check_custom_strategy,
    probe_sandbox,
    run_custom_strategy,
    run_custom_strategy_isolated,
    validate_code,
)
from app.core.config import settings


def _ohlcv(n: int = 300) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, n)))
    idx = pd.date_range("2022-01-01", periods=n, freq="D", tz="UTC")
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": 1e6}, index=idx)


def _cls(body: str) -> str:
    """Wrap method/class-body lines into a CustomStrategy subclass."""
    return "class S(CustomStrategy):\n" + body


# ----------------------------------------------------------- legit path --
def test_isolated_template_matches_in_process():
    data = _ohlcv()
    isolated, class_name = run_custom_strategy_isolated(DEFAULT_TEMPLATE, None, data)
    in_process = run_custom_strategy(build_custom_strategy(DEFAULT_TEMPLATE), data)
    assert class_name == "MyStrategy"
    assert isolated.index.equals(data.index)
    pd.testing.assert_series_equal(isolated, in_process, check_names=False)
    assert (isolated != 0).any()


def test_isolated_vectorized_params_and_clipping():
    src = _cls("    params = {'k': 5.0}\n"
               "    def generate(self, data):\n"
               "        return pd.Series(self.params['k'], index=data.index)\n")
    weights, _ = run_custom_strategy_isolated(src, {"k": -3.0}, _ohlcv(40))
    assert (weights == -1.0).all()          # clipped to the weight contract


def test_check_returns_class_name_and_surfaces_user_errors():
    assert check_custom_strategy(DEFAULT_TEMPLATE) == "MyStrategy"
    with pytest.raises(SandboxError, match="exactly one class"):
        check_custom_strategy("x = 1")
    with pytest.raises(SandboxError, match="user bug"):
        run_custom_strategy_isolated(
            _cls("    def next(self, i, bar):\n        raise ValueError('user bug')\n"),
            None, _ohlcv(30))


# ------------------------------------------------------------- timeouts --
def test_infinite_loop_in_generate_is_killed(monkeypatch):
    monkeypatch.setattr(settings, "CUSTOM_STRATEGY_TIMEOUT_S", 3.0)
    src = _cls("    def generate(self, data):\n"
               "        while True:\n"
               "            pass\n")
    t0 = time.monotonic()
    with pytest.raises(SandboxError, match="time limit"):
        run_custom_strategy_isolated(src, None, _ohlcv(30))
    assert time.monotonic() - t0 < 10        # killed, not left to spin


def test_infinite_loop_in_class_body_is_killed_at_validation(monkeypatch):
    monkeypatch.setattr(settings, "CUSTOM_STRATEGY_VALIDATE_TIMEOUT_S", 3.0)
    src = _cls("    while True:\n        pass\n")
    t0 = time.monotonic()
    with pytest.raises(SandboxError, match="time limit"):
        check_custom_strategy(src)
    assert time.monotonic() - t0 < 10


# ------------------------------------------------------------------ I/O --
@pytest.mark.parametrize("expr", [
    "pd.read_csv('/etc/hosts')",
    "pd.read_pickle('x.pkl')",
    "pd.read_json('http://127.0.0.1:9/x')",
    "self.data.to_csv('out.csv')",
    "self.data['close'].to_pickle('out.pkl')",
    "np.load('x.npy')",
    "np.save('x.npy', np.zeros(1))",
    "np.fromfile('/etc/hosts')",
    "np.zeros(3).tofile('x.bin')",
    "np.genfromtxt('/etc/hosts')",
    "self.data.eval('close * 2')",
    "pd.io",
])
def test_io_entry_points_rejected_before_execution(expr):
    src = _cls(f"    def generate(self, data):\n        x = {expr}\n"
               f"        return pd.Series(0.0, index=data.index)\n")
    errors = validate_code(src)
    assert any("not available in the sandbox" in e for e in errors), errors
    with pytest.raises(SandboxError, match="not available in the sandbox"):
        run_custom_strategy_isolated(src, None, _ohlcv(10))


def test_restricted_namespaces_have_no_io_functions():
    for name in ("read_csv", "read_pickle", "read_parquet", "read_sql", "read_html",
                 "eval", "io", "api", "plotting", "ExcelWriter", "HDFStore"):
        assert not hasattr(SAFE_PD, name), name
    for name in ("load", "save", "savez", "savetxt", "loadtxt", "genfromtxt",
                 "fromfile", "fromregex", "memmap", "lib", "ctypeslib", "testing"):
        assert not hasattr(SAFE_NP, name), name
    # the everyday numeric surface is still there
    for name in ("Series", "DataFrame", "isna", "concat", "Timestamp", "date_range"):
        assert hasattr(SAFE_PD, name), name
    for name in ("where", "sign", "log", "nan", "zeros", "clip", "sqrt", "maximum"):
        assert hasattr(SAFE_NP, name), name
    assert hasattr(SAFE_NP.random, "default_rng") and hasattr(SAFE_NP.linalg, "norm")
    assert not hasattr(SAFE_NP.random, "bit_generator")   # submodules stripped


def test_child_runtime_guards_block_files_network_and_processes():
    """Even if code got past the static gate, the child's audit hook refuses
    the operation. The probe exercises the guards without user code."""
    checks = probe_sandbox()["checks"]
    assert checks == {"read_outside_python": "blocked", "write_file": "blocked",
                      "network": "blocked", "subprocess": "blocked"}


def test_child_has_cpu_and_file_size_limits():
    limits = probe_sandbox()["rlimits"]
    assert 0 < limits["RLIMIT_CPU"] <= int(settings.CUSTOM_STRATEGY_VALIDATE_TIMEOUT_S) + 2
    assert limits["RLIMIT_FSIZE"] == 0


# ------------------------------------------------------------ secrets --
def test_web_process_environment_is_not_visible(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "canary-jwt-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://u:canary@db/x")
    monkeypatch.setenv("POSTGRES_PASSWORD", "canary")
    env_keys = set(probe_sandbox()["env"])
    assert not env_keys & {"JWT_SECRET", "DATABASE_URL", "POSTGRES_PASSWORD"}
    assert env_keys == set(sandbox.child_env("/tmp"))    # exactly the minimal set


def test_user_code_cannot_name_os_or_environ():
    src = _cls("    def generate(self, data):\n"
               "        return pd.Series(len(os.environ), index=data.index)\n")
    with pytest.raises(SandboxError, match="NameError"):
        run_custom_strategy_isolated(src, None, _ohlcv(10))


# ------------------------------------------------------- feature flag --
def test_flag_off_refuses_before_running_anything(monkeypatch):
    monkeypatch.setattr(settings, "CUSTOM_STRATEGIES_ENABLED", False)
    with pytest.raises(SandboxDisabledError):
        check_custom_strategy(DEFAULT_TEMPLATE)
    with pytest.raises(SandboxDisabledError):
        run_custom_strategy_isolated(DEFAULT_TEMPLATE, None, _ohlcv(10))


def _auth(client, unique) -> dict:
    """Register via the `unique` fixture so the session teardown deletes it."""
    r = client.post("/auth/register", json={
        "email": unique["email"], "username": unique["username"],
        "password": unique["password"]})
    assert r.status_code == 201, r.text
    token = client.post("/auth/login", json={
        "email": unique["email"], "password": unique["password"]}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_api_validate_runs_through_sandbox(client, unique):
    h = _auth(client, unique)
    r = client.post("/strategies/validate", headers=h, json={"code": DEFAULT_TEMPLATE})
    assert r.status_code == 200 and r.json() == {
        "ok": True, "errors": [], "class_name": "MyStrategy"}
    bad = _cls("    def generate(self, data):\n        return pd.read_csv('/etc/hosts')\n")
    r = client.post("/strategies/validate", headers=h, json={"code": bad})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert any("read_csv" in e for e in r.json()["errors"])


def test_api_flag_off_hides_and_forbids_custom_code(client, unique, monkeypatch):
    h = _auth(client, unique)
    monkeypatch.setattr(settings, "CUSTOM_STRATEGIES_ENABLED", False)

    reg = client.get("/strategies/registry", headers=h).json()
    assert "custom_code" not in {s["key"] for s in reg["strategies"]}
    assert reg["custom_strategies_enabled"] is False and reg["custom_template"] == ""

    r = client.post("/strategies/validate", headers=h, json={"code": DEFAULT_TEMPLATE})
    assert r.status_code == 403

    sv = client.post("/strategies", headers=h,
                     json={"name": f"sbx {uuid.uuid4().hex[:6]}"}).json()
    r = client.post("/backtests", headers=h, json={
        "strategy_version_id": sv["version_id"], "asset_id": 1,
        "strategy": "custom_code", "code": DEFAULT_TEMPLATE})
    assert r.status_code == 403, r.text


# ------------------------------------------------- runner integration --
@pytest.fixture()
def one_asset_env():
    """One asset with 120 rising daily bars + a strategy row; full teardown."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import delete

    from app.db.session import SessionLocal
    from app.models import (
        Asset, Backtest, BacktestYearlyResult, OhlcvBar, Strategy, StrategyVersion, User,
    )
    from app.models.enums import AssetClass, Timeframe

    with SessionLocal() as db:
        u = User(email=f"sbx_{uuid.uuid4().hex[:8]}@e.com",
                 username=f"sbx_{uuid.uuid4().hex[:8]}", hashed_password="x")
        a = Asset(symbol=f"SBX{uuid.uuid4().hex[:5]}", exchange="X",
                  asset_class=AssetClass.US_EQUITY)
        db.add_all([u, a]); db.commit(); db.refresh(u); db.refresh(a)
        t0 = datetime(2021, 1, 1, tzinfo=timezone.utc)
        db.add_all([OhlcvBar(asset_id=a.id, timeframe=Timeframe.D1,
                             time=t0 + timedelta(days=i), open=100 + i, high=100 + i,
                             low=100 + i, close=100 + i, volume=1e6) for i in range(120)])
        s = Strategy(user_id=u.id, name="sbx"); db.add(s); db.flush()
        sv = StrategyVersion(strategy_id=s.id, version=1, code=""); db.add(sv); db.commit()
        ctx = {"uid": u.id, "aid": a.id, "sid": s.id, "svid": sv.id}
    yield ctx
    with SessionLocal() as db:
        db.execute(delete(BacktestYearlyResult).where(
            BacktestYearlyResult.backtest_id.in_(
                db.query(Backtest.id).filter_by(user_id=ctx["uid"]).subquery().select())))
        db.execute(delete(Backtest).where(Backtest.user_id == ctx["uid"]))
        db.execute(delete(StrategyVersion).where(StrategyVersion.strategy_id == ctx["sid"]))
        db.execute(delete(Strategy).where(Strategy.id == ctx["sid"]))
        db.execute(delete(OhlcvBar).where(OhlcvBar.asset_id == ctx["aid"]))
        db.execute(delete(Asset).where(Asset.id == ctx["aid"]))
        db.execute(delete(User).where(User.id == ctx["uid"]))
        db.commit()


def _run_custom(ctx, code: str):
    from app.backtest.runner import run_and_persist
    from app.db.session import SessionLocal
    from app.models import Backtest
    from app.models.enums import BacktestStatus

    with SessionLocal() as db:
        bt = Backtest(user_id=ctx["uid"], strategy_version_id=ctx["svid"],
                      status=BacktestStatus.QUEUED,
                      config={"asset_id": ctx["aid"], "timeframe": "1d",
                              "strategy": "custom_code", "code": code, "params": {},
                              "initial_capital": 100000, "commission_bps": 0,
                              "slippage_bps": 0})
        db.add(bt); db.commit(); db.refresh(bt); btid = bt.id
    try:
        run_and_persist(btid)
    except Exception:  # noqa: BLE001 — failure is recorded on the row
        pass
    with SessionLocal() as db:
        bt = db.get(Backtest, btid)
        return bt.status, bt.error, bt.diagnostics, bt.total_return_pct


def test_runner_executes_custom_code_in_sandbox(one_asset_env):
    from app.models.enums import BacktestStatus

    code = ("class AlwaysLong(CustomStrategy):\n"
            "    def generate(self, data):\n"
            "        return pd.Series(1.0, index=data.index)\n")
    status, error, diagnostics, ret = _run_custom(one_asset_env, code)
    assert status == BacktestStatus.COMPLETED, error
    assert diagnostics["custom_class"] == "AlwaysLong"
    assert ret is not None and ret > 0              # long a rising series


def test_runner_fails_runaway_custom_code(one_asset_env, monkeypatch):
    from app.models.enums import BacktestStatus

    monkeypatch.setattr(settings, "CUSTOM_STRATEGY_TIMEOUT_S", 3.0)
    code = _cls("    def generate(self, data):\n        while True:\n            pass\n")
    status, error, _, _ = _run_custom(one_asset_env, code)
    assert status == BacktestStatus.FAILED
    assert "time limit" in error
