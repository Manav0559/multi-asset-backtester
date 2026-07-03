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
