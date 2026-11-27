import asyncio
import json
import sys
from unittest.mock import Mock

import httpx
import pytest
from websockets.asyncio.server import serve

import main
from copper import market
from copper.live import Feed, RestJournal
from backend.nh_api.errors import TransportError


@pytest.mark.parametrize("override, expected", [([], 20), (["--rest-timeout", "2.5"], 2.5)])
def test_paper_cli_sets_rest_client_timeout(monkeypatch, tmp_path, override, expected):
    monkeypatch.setenv("NH_PAPER_OVERSEAS_APP_KEY", "test-key")
    monkeypatch.setenv("NH_PAPER_OVERSEAS_APP_SECRET", "test-secret")
    monkeypatch.setenv("NH_REPLAY", "0")
    monkeypatch.setattr(sys, "argv", ["main.py", "paper", "--bar-stamp", "start",
                                      "--env-file", str(tmp_path / ".env"), *override])
    monkeypatch.setattr(main, "load_thresholds", Mock())

    def paper(root, session, args, thresholds):
        assert session.client.timeout == httpx.Timeout(expected)

    monkeypatch.setattr(main, "paper", paper)
    monkeypatch.setattr(market.runtime, "use", Mock())
    main.main()


def test_rest_transport_error_records_type_without_credentials(tmp_path):
    journal = RestJournal("HGH27", root=tmp_path, echo=False)
    exc = TransportError("quote", "secret in error")
    exc.__cause__ = httpx.ReadTimeout("secret in underlying error")
    journal.error("quote", exc)
    report = (tmp_path / "api-errors.jsonl").read_text()
    assert "secret" not in report
    assert json.loads(report)["kind"] == "ReadTimeout"
    assert json.loads(report)["attempt"] == 1


@pytest.mark.parametrize("abrupt, disconnect_type", [(False, "ConnectionClosedOK"),
                                                    (True, "ConnectionClosedError")])
def test_paper_stream_reconnects_and_unsubscribes_without_logging_secrets(tmp_path, abrupt, disconnect_type):
    journal = RestJournal("HGH27", root=tmp_path, echo=False)

    async def scenario():
        feed = Feed({"SYMBOL": "HGH27"}, Mock(), Mock(), journal)
        assert feed.stream.idle_timeout is None
        connect = feed.stream.connect

        def nh_connect(url, **options):
            assert options["ping_interval"] is None
            return connect(url, **options)

        feed.stream.connect = nh_connect
        feed.stream.reconnect_delays = (0.01,)
        feed.stream.credentials = Mock(side_effect=[
            OSError("secret-access-key"), ("first-key", "test-token"), ("second-key", "test-token"),
        ])
        keys = []
        subscriptions = []
        closed = asyncio.Event()
        quiet = asyncio.Event()
        stop = asyncio.Event()

        async def handler(ws):
            keys.append(ws.request.path.split("access_key=")[-1])
            await ws.recv()
            await ws.send(json.dumps({"code": "INIT", "msg": "INIT_SUCCESS"}))
            for _ in range(2):
                subscriptions.append(json.loads(await ws.recv()))
            await ws.send(json.dumps({"code": "pingpong"}))
            assert await ws.recv() == ""
            if len(keys) == 1:
                if abrupt:
                    ws.transport.abort()
                else:
                    await ws.close()
                return
            quiet.set()
            async for message in ws:
                subscriptions.append(json.loads(message))
            closed.set()

        async with serve(handler, "127.0.0.1", 0) as server:
            feed.stream.ws_url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            task = asyncio.create_task(feed.stream.run(stop))
            try:
                await asyncio.wait_for(quiet.wait(), timeout=3)
                await asyncio.sleep(0.05)
                assert keys == ["first-key", "second-key"]
                assert not task.done()
            finally:
                stop.set()
                await asyncio.wait_for(task, timeout=3)
            await asyncio.wait_for(closed.wait(), timeout=3)

        actions = [(msg["header"]["action"], msg["body"]["action_code"]) for msg in subscriptions]
        assert actions == [("A", "FA"), ("A", "FB")] * 2 + [("D", "FA"), ("D", "FB")]

    asyncio.run(scenario())
    errors = [(row["error"], row["attempt"]) for row in journal.today() if "error" in row]
    assert errors == [("OSError", 1), (disconnect_type, 2)]
    for value in ("secret-access-key", "first-key", "second-key", "test-token"):
        assert value not in journal.path().read_text()
