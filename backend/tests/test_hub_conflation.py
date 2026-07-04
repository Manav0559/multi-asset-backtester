"""Slow-consumer safety: the per-connection sender conflates market data and
bounds must-deliver frames.

DoD: a burst of N ticks for one symbol leaves at most ONE buffered frame for
that channel; portfolio events are never conflated (all queued); a client that
can't drain must-deliver frames overflows (→ disconnect) rather than ballooning
memory.
"""
from app.streaming.hub import _MUST_DELIVER_MAX, _Sender, _is_conflatable


class _FakeWS:
    async def send_text(self, frame):  # never called in these sync tests
        pass


def test_tick_burst_conflates_to_one_per_channel():
    s = _Sender(_FakeWS())
    for i in range(200):
        s.offer("tick:BINANCE:BTCUSDT", f'{{"p":{i}}}')
    # 200 ticks on one channel -> exactly one buffered (the latest).
    assert s.pending_conflated() == 1


def test_multiple_symbols_keep_one_each():
    s = _Sender(_FakeWS())
    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        for i in range(50):
            s.offer(f"depth:BINANCE:{sym}", f'{{"n":{i}}}')
    assert s.pending_conflated() == 3  # one per symbol, not 150


def test_portfolio_events_are_must_deliver_not_conflated():
    s = _Sender(_FakeWS())
    assert not _is_conflatable("portfolio:abc")
    for i in range(5):
        s.offer("portfolio:abc", f'{{"seq":{i}}}')
    # All 5 queued (conflation dict untouched), so none are dropped.
    assert s.pending_conflated() == 0
    assert len(s._must) == 5


def test_must_deliver_overflow_flags_disconnect():
    s = _Sender(_FakeWS())
    for i in range(_MUST_DELIVER_MAX + 50):
        s.offer("portfolio:abc", f'{{"seq":{i}}}')
    assert s.overflowed is True                 # slow client on must-deliver
    assert len(s._must) == _MUST_DELIVER_MAX     # queue never grew past the cap
