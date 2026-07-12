"""Scale-out proof: 2 hub replicas behind a round-robin LB, no sticky sessions.

Three assertions, all through the LB on :8000:
  1. REST round-robins: /health returns >= 2 distinct `node` hostnames.
  2. Two WS clients land on DIFFERENT replicas (retrying connects until the
     `connected` frames report distinct nodes — Docker DNS shuffles, so a
     couple of attempts suffice).
  3. An order placed via the LB (handled by whichever replica) produces a
     portfolio event received by BOTH sockets — cross-replica fan-out rides
     Redis, which is the whole no-sticky-sessions claim.

Run after: docker compose -f docker-compose.yml -f docker-compose.scale.yml
           --profile app up -d backend lb
"""
import json
import sys
import time
import urllib.request

from websockets.sync.client import connect as ws_connect

BASE = "http://localhost:8000"


def api(path, payload=None, token=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})},
        method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def main() -> int:
    # -- 1. REST round-robin -------------------------------------------------
    nodes = {api("/health")["node"] for _ in range(12)}
    print(f"REST /health nodes seen via LB: {sorted(nodes)}")
    assert len(nodes) >= 2, "LB did not round-robin across replicas"

    tok = api("/auth/login", {"email": "alice@demo.backtester.dev",
                              "password": "demo-pass-123"})["access_token"]
    pid = next(p["id"] for p in api("/portfolios", token=tok)
               if p["name"] == "The Syndicate")
    ada = next(a["id"] for a in api("/assets", token=tok)
               if a["symbol"] == "ADAUSDT")
    chan = f"portfolio:{pid}"

    # -- 2. two sockets on two different replicas ----------------------------
    def open_ws():
        ws = ws_connect(f"ws://localhost:8000/ws?token={tok}")
        hello = json.loads(ws.recv())
        assert hello["type"] == "connected"
        ws.send(json.dumps({"action": "subscribe", "channels": [chan]}))
        while json.loads((ack := ws.recv()))["type"] != "subscribed":
            ack = None
        return ws, hello["node"]

    ws_a, node_a = open_ws()
    ws_b, node_b = None, node_a
    for attempt in range(12):
        ws_b, node_b = open_ws()
        if node_b != node_a:
            break
        ws_b.close()
    print(f"socket A on {node_a}, socket B on {node_b}")
    assert node_b != node_a, "could not land sockets on distinct replicas"
    time.sleep(0.5)  # let both hubs' psubscribes settle

    # -- 3. cross-replica fan-out --------------------------------------------
    fill = api(f"/portfolios/{pid}/orders", {"asset_id": ada, "side": "buy",
                                             "qty": "1"}, token=tok)
    assert fill["status"] == "filled", f"seed order not filled: {fill}"

    def wait_order(ws, deadline_s=15):
        end = time.time() + deadline_s
        while time.time() < end:
            frame = json.loads(ws.recv(timeout=max(0.1, end - time.time())))
            if (frame.get("type") == "message" and frame.get("channel") == chan
                    and frame["data"].get("type") == "order"
                    and frame["data"].get("order_id") == fill["order_id"]):
                return True
        return False

    got_a, got_b = wait_order(ws_a), wait_order(ws_b)
    print(f"order event delivered: replica {node_a}={got_a}, {node_b}={got_b}")
    ws_a.close(); ws_b.close()
    assert got_a and got_b, "cross-replica fan-out failed"

    print("\nPASS: round-robin REST, sockets on distinct replicas, one fill "
          "delivered to BOTH replicas' sockets — no sticky sessions anywhere.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
