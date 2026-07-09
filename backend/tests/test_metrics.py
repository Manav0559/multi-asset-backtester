"""Prometheus /metrics endpoint + instrumentation."""


def test_metrics_endpoint_exposes_request_metrics(client):
    # Generate a request we can assert on, then scrape.
    assert client.get("/health").status_code == 200
    res = client.get("/metrics")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain")
    body = res.text
    # Counter + histogram from the request middleware, labeled by route template.
    assert "http_requests_total" in body
    assert 'route="/health"' in body
    assert "http_request_duration_seconds_bucket" in body
    # The worker's backtest histogram is registered app-wide (same module),
    # so its declaration is visible even before any backtest runs.
    assert "backtest_duration_seconds" in body


def test_metrics_uses_route_template_not_raw_path(client, unique):
    """Path params must be folded into their template so cardinality is bounded."""
    client.post("/auth/register", json={
        "email": unique["email"], "password": unique["password"],
        "username": unique["username"],
    })
    login = client.post("/auth/login", json={
        "email": unique["email"], "password": unique["password"],
    }).json()
    headers = {"Authorization": f"Bearer {login['access_token']}"}

    fake_id = "00000000-0000-0000-0000-000000000000"
    client.get(f"/backtests/{fake_id}", headers=headers)  # 404, but matched route

    body = client.get("/metrics").text
    assert 'route="/backtests/{backtest_id}"' in body
    assert fake_id not in body


def test_ws_and_pool_fabric_metrics_exposed(client):
    """The fan-out fabric is observable: client gauge moves with a real WS
    connection; pool gauges exist with a sane configured size."""
    import re
    import uuid as _uuid

    from app.core.security import create_access_token
    from app.db.session import SessionLocal
    from app.models import User

    def gauge(body: str, name: str) -> float:
        m = re.search(rf"^{name} ([0-9.e+-]+)$", body, re.M)
        assert m, f"{name} not exposed"
        return float(m.group(1))

    with SessionLocal() as db:
        u = User(email=f"mt_{_uuid.uuid4().hex[:8]}@x.com",
                 username=f"mt_{_uuid.uuid4().hex[:8]}", hashed_password="x")
        db.add(u); db.commit(); db.refresh(u)
        uid = u.id
    token = create_access_token(uid)
    try:
        baseline = gauge(client.get("/metrics").text, "ws_connected_clients")
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.receive_text()  # connected
            body = client.get("/metrics").text
            assert gauge(body, "ws_connected_clients") == baseline + 1
            assert gauge(body, "db_pool_size") > 0
            assert gauge(body, "db_pool_checked_out") >= 0
        # paired dec on disconnect
        assert gauge(client.get("/metrics").text, "ws_connected_clients") == baseline
    finally:
        with SessionLocal() as db:
            db.query(User).filter_by(id=uid).delete()
            db.commit()


def test_backlog_gauges_sampled(client):
    """The relay's piggybacked sampler sets queue-depth + outbox-pending."""
    from prometheus_client import REGISTRY

    from app.backtest.tasks import _sample_ops_gauges

    _sample_ops_gauges()
    q = REGISTRY.get_sample_value("celery_queue_depth")
    p = REGISTRY.get_sample_value("outbox_pending_events")
    assert q is not None and q >= 0
    assert p is not None and p >= 0
