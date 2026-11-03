"""웹소켓 클라이언트 — 로컬 가짜 서버로 접속 절차를 점검합니다(NH 서버·인증 없음).

가짜 서버는 개발자센터 안내대로 답합니다: session_init → INIT_SUCCESS, 구독 → RT_INIT,
중간에 pingpong(빈 문자열 답을 기다림), FA 데이터. 첫 접속은 데이터 두 건 뒤 끊어 재접속도 봅니다.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from websockets.asyncio.server import serve

from trader.realtime import (
    FA,
    O1,
    NHStream,
    RealtimeFeed,
    Subscription,
    TickRecorder,
    action_code_of,
    kind_of,
    parse_tick,
)


def fa(price: str, tm: str) -> dict:
    return {"header": {"action_code": "FA"},
            "body": {"sym": "MKUX26", "last_pric": price, "exec_qty": "1", "ko_exec_tm": tm,
                     "bid_best_pric": "0.7475", "ask_best_pric": "0.7476"}}


class FakeNH:
    def __init__(self) -> None:
        self.received: list[tuple[int, str]] = []   # (접속 번호, 원문)
        self.keys: list[str] = []
        self.connection = 0

    async def handler(self, ws) -> None:  # noqa: ANN001
        self.connection += 1
        n = self.connection
        self.keys.append(ws.request.path.split("access_key=")[-1])
        init = json.loads(await ws.recv())
        self.received.append((n, json.dumps(init)))
        assert init["body"] == {"action_code": "session_init", "session_token": "TOKEN"}
        await ws.send(json.dumps({"code": "INIT", "msg": "INIT_SUCCESS"}))
        sub = json.loads(await ws.recv())
        self.received.append((n, json.dumps(sub)))
        await ws.send(json.dumps({"rt_cd": "RT_INIT", "action_code": sub["body"]["action_code"]}))
        await ws.send(json.dumps({"code": "pingpong"}))
        pong = await ws.recv()
        self.received.append((n, pong))
        await ws.send(json.dumps(fa("0.7476", "220001")))
        await ws.send(json.dumps(fa("0.7477", "220002")))
        if n == 1:
            await ws.close()      # 끊김 → 클라이언트가 새 키로 다시 접속해야 함
            return
        async for raw in ws:      # 구독 해제(D) 까지 받습니다
            self.received.append((n, raw))


def test_message_kinds() -> None:
    assert kind_of({"code": "pingpong"}) == "pingpong"
    assert kind_of({"code": "INIT", "msg": "INIT_SUCCESS"}) == "init"
    assert kind_of({"rt_cd": "RT_INIT"}) == "ack"
    assert kind_of({"rt_cd": "RT_FAIL"}) == "fail"
    assert kind_of({"header": {"result": "success"}}) == "ack"
    assert kind_of(fa("0.7476", "220001")) == "data"
    assert action_code_of(fa("0.7476", "220001")) == FA
    # 채널 표시가 없고 값이 바깥에 와도 읽습니다.
    flat = {"header": {}, "last_pric": "0.7480", "sym": "MKUX26"}
    assert action_code_of(flat) == FA and parse_tick(flat).price == 0.748
    assert action_code_of({"header": {}, "body": {"ord_no": "1"}}) == O1


def test_subscription_message() -> None:
    assert Subscription(FA, "MKUX26").message() == {
        "header": {"api_ver": "1", "action": "A"}, "body": {"action_code": "FA", "action_sym": "MKUX26"}}
    assert Subscription(O1).message("D")["body"] == {"action_code": "O1"}


def test_stream_init_subscribe_pingpong_reconnect(tmp_path: Path) -> None:
    server = FakeNH()
    issued = iter(f"KEY{i}" for i in range(10))
    statuses: list[str] = []

    async def scenario() -> list[dict]:
        data: list[dict] = []
        async with serve(server.handler, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            stop = asyncio.Event()
            stream = NHStream(f"ws://127.0.0.1:{port}", lambda: (next(issued), "TOKEN"), [Subscription(FA, "MKUX26")],
                              on_data=lambda m: (data.append(m), len(data) >= 4 and stop.set()),
                              on_status=lambda k, _d: statuses.append(k), reconnect_delays=(0.05,))
            await asyncio.wait_for(stream.run(stop), timeout=10)
            await asyncio.sleep(0.1)
        return data

    data = asyncio.run(scenario())
    assert [parse_tick(m).price for m in data] == [0.7476, 0.7477, 0.7476, 0.7477]
    assert server.keys == ["KEY0", "KEY1"]               # 접속마다 새 웹소켓 키
    assert (1, "") in server.received and (2, "") in server.received   # pingpong 에 빈 문자열로 답함
    resub = [json.loads(r) for n, r in server.received if n == 2 and r.startswith("{")]
    assert resub[1]["header"]["action"] == "A"           # 다시 접속하면 다시 구독
    assert resub[-1]["header"]["action"] == "D"          # 멈출 때 구독 해제
    assert statuses.count("init") == 2 and "ack" in statuses and "disconnected" in statuses


def test_feed_thread_and_recorder(tmp_path: Path) -> None:
    server = FakeNH()
    recorder = TickRecorder(tmp_path, "MKUX26")

    async def serve_forever(ready: asyncio.Event, port_box: list[int], stop: asyncio.Event) -> None:
        async with serve(server.handler, "127.0.0.1", 0) as srv:
            port_box.append(srv.sockets[0].getsockname()[1])
            ready.set()
            await stop.wait()

    import threading

    loop = asyncio.new_event_loop()
    ready, stop, port_box = asyncio.Event(), asyncio.Event(), []
    t = threading.Thread(target=lambda: loop.run_until_complete(serve_forever(ready, port_box, stop)), daemon=True)
    t.start()
    while not port_box:
        time.sleep(0.01)

    feed = RealtimeFeed(lambda d, s: NHStream(f"ws://127.0.0.1:{port_box[0]}", lambda: ("K", "TOKEN"),
                                              [Subscription(FA, "MKUX26")], d, s, reconnect_delays=(0.05,)),
                        listeners=[recorder])
    feed.start()
    assert feed.ready.wait(5)
    deadline = time.time() + 5
    while time.time() < deadline and len(list(tmp_path.glob("*.jsonl"))) == 0:
        time.sleep(0.02)
    time.sleep(0.3)
    feed.stop()
    loop.call_soon_threadsafe(stop.set)
    t.join(5)

    assert feed.latest() is not None and feed.latest().price in (0.7476, 0.7477)
    lines = next(tmp_path.glob("ticks_MKUX26_*.jsonl")).read_text().splitlines()
    assert len(lines) >= 2 and json.loads(lines[0])["price"] == 0.7476
