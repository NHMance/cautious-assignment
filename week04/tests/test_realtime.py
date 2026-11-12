"""가짜 소켓으로 국내 KB 구독·재접속·가격 검증을 확인합니다. 네트워크/주문 없음."""
import asyncio
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from spread.realtime import QuoteFeed, QuotePair, parse_quote
from spread.live import Reference, quote_signal
from spread.strategy import KST, Quote, Settings, quote_problem

T = datetime(2026, 11, 12, 14, tzinfo=KST)


def kb(symbol="N", **values):
    return {"header": {"action_code": "KB", "action_sym": symbol},
            "body": {"sym": symbol, "exch_tm": "140000", "bid_pric_1": "1445.0",
                     "ask_pric_1": "1445.1", "bid_qty_1": "10", "ask_qty_1": "20", **values}}


def client():
    return SimpleNamespace(session=SimpleNamespace(
        config=SimpleNamespace(default_environment="paper"), is_replay=False,
        base_url=lambda env: "https://apidemo.futures.co.kr",
        tokens=SimpleNamespace(token=lambda *args: SimpleNamespace(access_token="SECRET_TOKEN"),
                               websocket_key=lambda *args: "SECRET_KEY")))


def test_kb_parser_uses_level_one_quantities_and_exchange_time():
    quote = parse_quote(kb(ask_qty="9999", bid_qty="9999"), T)
    assert quote == Quote("N", T, 1445, 1445.1, 10, 20)
    assert parse_quote({"header": {"action_code": "KA"}, "body": {}}, T) is None
    for values in ({"bid_pric_1": "nan"}, {"ask_qty_1": ""}, {"exch_tm": ""}, {"bid_qty_1": "1.5"}):
        with pytest.raises((ValueError, TypeError)):
            parse_quote(kb(**values), T)


def test_cache_requires_both_symbols_and_clears_on_disconnect_or_bad_message():
    feed = QuoteFeed(client(), ("N", "F"))
    feed._data(kb())
    assert feed.latest() is None
    feed._data(kb("F"))
    assert feed.latest().near.symbol == "N"
    feed._invalidate()
    feed._data(kb("N"))
    assert feed.latest() is None
    feed._data(kb("F"))
    assert feed.latest() is not None
    feed._data(kb("N", bid_qty_1=""))
    assert feed.latest() is None


def test_domestic_session_init_ping_subscribe_and_unsubscribe_without_network():
    sent = []

    class Socket:
        def __init__(self):
            self.messages = asyncio.Queue()
            for msg in ({"code": "pingpong"}, {"code": "INIT", "msg": "INIT_SUCCESS"},
                        {"rt_cd": "RT_INIT"}, {"code": "pingpong"}, kb(), kb("F")):
                self.messages.put_nowait(json.dumps(msg))

        async def send(self, text):
            sent.append(text)

        async def recv(self):
            return await self.messages.get()

    class Connection:
        async def __aenter__(self):
            return Socket()

        async def __aexit__(self, *args):
            pass

    def connect(url, **kwargs):
        assert url == "wss://apidemo.futures.co.kr/trade/ws-stream?access_key=SECRET_KEY"
        return Connection()

    async def scenario():
        feed = QuoteFeed(client(), ("N", "F"), connector=connect)
        feed._stop = asyncio.Event()
        original = feed._data

        def data(message):
            original(message)
            if feed.latest():
                feed._stop.set()

        feed._data = data
        await asyncio.wait_for(feed._session(), timeout=1)
        return feed

    feed = asyncio.run(scenario())
    messages = [json.loads(s) for s in sent if s]
    assert messages[0]["body"]["session_token"] == "SECRET_TOKEN"
    assert [(m["header"]["action"], m["body"].get("action_sym")) for m in messages[1:]] == [
        ("A", "N"), ("A", "F"), ("D", "N"), ("D", "F")]
    assert sent.count("") == 2
    assert feed.latest() is not None
    assert "SECRET" not in str(list(feed.drain_events()))


def test_reconnect_issues_new_key_and_does_not_reuse_old_far_quote():
    keys, connections = [], []
    api = client()
    api.session.tokens.websocket_key = lambda *args: f"KEY{len(keys)}"

    class Connection:
        def __init__(self, number):
            self.number = number
            self.queue = asyncio.Queue()
            for msg in ({"code": "INIT", "msg": "INIT_SUCCESS"}, kb("F") if number == 1 else kb("N")):
                self.queue.put_nowait(json.dumps(msg))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send(self, text):
            pass

        async def recv(self):
            if self.queue.empty():
                if self.number == 1:
                    raise OSError("disconnect")
                assert feed.latest() is None
                self.queue.put_nowait(json.dumps(kb("F")))
            return await self.queue.get()

    def connect(url, **kwargs):
        keys.append(url.split("access_key=")[1])
        connections.append(Connection(len(keys)))
        return connections[-1]

    async def scenario():
        nonlocal feed
        feed = QuoteFeed(api, ("N", "F"), connector=connect, reconnect_delays=(0,))
        feed._stop = asyncio.Event()
        original = feed._data

        def data(message):
            original(message)
            if len(keys) == 2 and feed.latest():
                feed._stop.set()

        feed._data = data
        await asyncio.wait_for(feed._run(), timeout=1)

    feed = None
    asyncio.run(scenario())
    assert keys == ["KEY0", "KEY1"]
    assert feed.latest() is not None


def test_live_signal_uses_quote_instead_of_waiting_for_bar_signal():
    reference = Reference(T, 24, 1, 0.4)
    n = Quote("N", T, 1445, 1445.1)
    f = Quote("F", T, 1446.8, 1446.9)
    signal, reason, stats = quote_signal(reference, n, f, T + timedelta(seconds=1), Settings())
    assert reason == "SIGNAL"
    assert signal.direction == -1
    assert stats["z"] == pytest.approx(2)
    assert signal.ts == T + timedelta(seconds=1)
    assert quote_signal(reference, n, f, T + timedelta(minutes=5), Settings())[1] == "STALE_REFERENCE"
    assert quote_problem(n, f, T + timedelta(seconds=11), Settings()) == "STALE_QUOTE"


@pytest.mark.parametrize("reference,reason", [
    (None, "REFERENCE_PENDING"),
    (Reference(T, 23, 1, 0.4), "WARMUP"),
    (Reference(T, 24, 1, 0), "ZERO_STD"),
    (Reference(T - timedelta(minutes=5), 24, 1, 0.4), "STALE_REFERENCE"),
])
def test_missing_or_old_reference_blocks_entry(reference, reason):
    n = Quote("N", T, 1445, 1445.1)
    f = Quote("F", T, 1446.8, 1446.9)
    assert quote_signal(reference, n, f, T, Settings())[1] == reason


def test_reference_keeps_last_24_contiguous_completed_bars(monkeypatch):
    import spread.live as live
    from spread.strategy import Bar

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return T

    monkeypatch.setattr(live, "datetime", Clock)

    def fetch(api, symbol, day, journal):
        # 마지막 하나는 진행 중이므로 통계에 포함되면 안 됩니다.
        return [Bar(T - timedelta(minutes=5 * (24 - i)), 1445,
                    1445 + (1 if symbol == "N" else 1.6 if i % 2 == 0 else 2.4), 1)
                for i in range(25)]

    monkeypatch.setattr(live, "fetch_bars", fetch)
    source = live.BarReference(None, ("N", "F"), Settings())
    reference = source.refresh(T)
    assert reference.end == T
    assert reference.count == 24
    assert reference.mean == pytest.approx(1)
    assert reference.std == pytest.approx(0.4)


def test_slow_reference_fetch_does_not_block_quote_updates(monkeypatch):
    import threading
    import spread.live as live
    from spread.strategy import Bar
    entered, release = threading.Event(), threading.Event()

    def fetch(*args):
        entered.set()
        assert release.wait(1)
        return []

    monkeypatch.setattr(live, "fetch_bars", fetch)
    source = live.BarReference(None, ("N", "F"), Settings())
    feed = QuoteFeed(client(), ("N", "F"))
    source.start()
    try:
        assert entered.wait(1)
        feed._data(kb("N"))
        feed._data(kb("F"))
        assert feed.latest() is not None
        assert source.latest() is None
    finally:
        release.set()
        source.stop()


def test_recheck_after_account_query_blocks_reverted_price_without_order(tmp_path, monkeypatch):
    import spread.broker as module
    from spread.broker import PairExecutor, SimBroker
    from spread.journal import Journal
    from spread.strategy import Signal

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return T

    monkeypatch.setattr(module, "datetime", Clock)
    journal = Journal(tmp_path, "test")
    broker = SimBroker(("N", "F"), journal)
    engine = PairExecutor(broker, "N", "F", journal, Settings())
    n = Quote("N", T, 1445, 1445)
    initial = Quote("F", T, 1446.8, 1446.8)
    reverted = Quote("F", T, 1446, 1446)
    signal = Signal(T, -1, 1, 0.4, 2, 1.8)
    assert not engine.open(signal, n, initial, refresh_quotes=lambda: (n, reverted))
    assert engine.entries == 0 and broker.positions == {"N": 0, "F": 0}
    rows = [json.loads(s) for s in journal.path.read_text().splitlines()]
    assert any(r.get("reason") == "REVERTED_BEFORE_ENTRY" for r in rows)
    assert not any(r["type"] == "FILL" for r in rows)


def test_paper_uses_websocket_quotes_and_closes_on_operator_stop(tmp_path, monkeypatch):
    import signal
    import main
    import spread.broker as broker_module
    from spread.broker import SimBroker

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return T

    monkeypatch.setattr(main, "datetime", Clock)
    monkeypatch.setattr(broker_module, "datetime", Clock)
    monkeypatch.setattr(main, "ROOT", tmp_path)
    monkeypatch.setattr(main, "verify_prices", lambda *args: None)
    monkeypatch.setattr(main, "quote", lambda *args: pytest.fail("paper에서 REST 호가를 조회했습니다"))
    monkeypatch.setattr(main, "create_client", lambda *args: SimpleNamespace(session=SimpleNamespace(close=lambda: None)))
    brokers, feeds = [], []

    def broker(client, symbols, journal):
        result = SimBroker(symbols, journal)
        brokers.append(result)
        return result

    class Feed:
        fatal = False

        def __init__(self, *args):
            self.calls = 0
            self.stopped = False
            feeds.append(self)

        def start(self):
            pass

        def stop(self):
            self.stopped = True

        def drain_events(self):
            return []

        def latest(self, **kwargs):
            self.calls += 1
            spread = 1.8 if self.calls <= 2 else 1
            if self.calls == 4:
                signal.raise_signal(signal.SIGINT)
            return QuotePair(self.calls, Quote("N", T, 1445, 1445),
                             Quote("F", T, 1445 + spread, 1445 + spread), T, T)

    class References:
        def __init__(self, *args):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def drain_events(self):
            return []

        def latest(self):
            return Reference(T, 24, 1, 0.4)

    monkeypatch.setattr(main, "PaperBroker", broker)
    monkeypatch.setattr(main, "QuoteFeed", Feed)
    monkeypatch.setattr(main, "BarReference", References)
    main.run_paper({"near": {"series": "N"}, "far": {"series": "F"}}, Settings(), tmp_path / "journal")
    rows = [json.loads(s) for s in next((tmp_path / "journal").glob("journal_*.jsonl")).read_text().splitlines()]
    assert sum(r["type"] == "PAIR_OPEN" for r in rows) == 1
    assert sum(r["type"] == "PAIR_CLOSED" for r in rows) == 1
    assert rows[-1]["type"] == "RUN_END" and rows[-1]["flat"]
    assert brokers[0].positions == {"N": 0, "F": 0}
    assert feeds[0].stopped
