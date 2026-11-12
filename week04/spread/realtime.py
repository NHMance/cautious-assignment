"""국내 주간 KB 호가: 수신 스레드는 최신 호가만 보관하고 주문은 보내지 않습니다.

명세: https://apidev.futures.co.kr/api (국내 시세호가 KB).
exch_tm, bid_pric_1, ask_pric_1, bid_qty_1, ask_qty_1을 사용합니다.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import json
from math import isfinite
from queue import SimpleQueue, Empty
import threading

from websockets.asyncio.client import connect

from backend.nh_api.catalog import STREAM_PATH
from .strategy import KST, Quote


class StreamError(RuntimeError):
    pass


def subscription(symbol, action="A"):
    return {"header": {"api_ver": "1", "action": action},
            "body": {"action_code": "KB", "action_sym": symbol}}


def parse_quote(message, received=None):
    """KB 이외는 무시하며, 불완전한 KB 메시지는 오류로 처리합니다."""
    received = received or datetime.now(KST)
    header, body = message.get("header", {}), message.get("body", message)
    if not isinstance(header, dict) or not isinstance(body, dict):
        return None
    code = header.get("action_code") or body.get("action_code") or body.get("nght_tp")
    if code != "KB":
        return None
    symbol = str(body.get("sym") or header.get("action_sym") or "").strip()
    if not symbol:
        raise ValueError("KB 종목코드 누락")
    if header.get("action_sym") and str(header["action_sym"]).strip() != symbol:
        raise ValueError("KB 구독 종목과 응답 종목 불일치")
    tm = str(body.get("exch_tm") or "").strip()
    if len(tm) != 6 or not tm.isdigit():
        raise ValueError("KB 거래소 시각 누락/오류")
    ts = datetime.strptime(received.strftime("%Y%m%d") + tm, "%Y%m%d%H%M%S").replace(tzinfo=KST)
    bid, ask = float(body["bid_pric_1"]), float(body["ask_pric_1"])
    bq, aq = float(body["bid_qty_1"]), float(body["ask_qty_1"])
    if not all(isfinite(v) and v >= 0 for v in (bid, ask, bq, aq)) or not bq.is_integer() or not aq.is_integer():
        raise ValueError("KB 가격·수량 오류")
    return Quote(symbol, ts, bid, ask, int(bq), int(aq))


@dataclass(frozen=True)
class QuotePair:
    version: int
    near: Quote
    far: Quote
    near_received: datetime
    far_received: datetime


class QuoteFeed:
    """같은 연결에서 받은 두 월물의 최신 호가를 조건변수로 전달합니다.

    주문 확인 중 밀린 호가를 뒤늦게 재생하지 않고 가장 최근 쌍을 사용합니다.
    연결이 끊기면 캐시를 비워 재연결 전후의 호가가 섞이지 않게 합니다.
    """
    def __init__(self, client, symbols, *, connector=connect, reconnect_delays=(1, 2, 5, 10, 30)):
        if client.session.config.default_environment != "paper" or client.session.is_replay:
            raise ValueError("KB 구독에는 국내 모의 세션이 필요합니다")
        if client.session.base_url("paper").rstrip("/") != "https://apidemo.futures.co.kr":
            raise ValueError("모의 웹소켓 서버 주소 불일치")
        self.client, self.symbols = client, tuple(symbols)
        if len(self.symbols) != 2 or len(set(self.symbols)) != 2:
            raise ValueError("서로 다른 두 월물이 필요합니다")
        self.connector, self.reconnect_delays = connector, reconnect_delays
        self.events = SimpleQueue()
        self._condition = threading.Condition()
        self._quotes = {}
        self._version = 0
        self._thread = None
        self._loop = None
        self._stop = None
        self._stopping = threading.Event()
        self.fatal = False

    def _invalidate(self):
        with self._condition:
            self._quotes.clear()
            self._version += 1
            self._condition.notify_all()

    def _data(self, message):
        received = datetime.now(KST)
        try:
            quote = parse_quote(message, received)
        except (ValueError, TypeError, KeyError):
            self._invalidate()
            self.events.put(("WS_STATUS", {"status": "invalid_quote"}))
            return
        if quote is None or quote.symbol not in self.symbols:
            return
        with self._condition:
            previous = self._quotes.get(quote.symbol)
            if previous and quote.ts < previous[0].ts:
                return
            self._quotes[quote.symbol] = (quote, received)
            self._version += 1
            self._condition.notify_all()

    def latest(self, *, after=None, timeout=0):
        with self._condition:
            if after is not None:
                self._condition.wait_for(lambda: self._version != after or self.fatal, timeout)
            if len(self._quotes) != 2:
                return None
            n, f = (self._quotes[s] for s in self.symbols)
            return QuotePair(self._version, n[0], f[0], n[1], f[1])

    def drain_events(self):
        while True:
            try:
                yield self.events.get_nowait()
            except Empty:
                return

    async def _message(self, ws, raw, *, initializing=False):
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            raise StreamError("웹소켓 JSON 응답 오류") from None
        if not isinstance(msg, dict):
            raise StreamError("웹소켓 응답 형식 오류")
        if msg.get("code") == "pingpong":
            await ws.send("")
            return False
        if msg.get("code") == "INIT" and msg.get("msg") == "INIT_SUCCESS":
            return True
        header = msg.get("header") or {}
        if msg.get("rt_cd") == "RT_FAIL" or str(header.get("result", "")).lower() == "fail":
            # 원문·접속 URL에는 인증 정보가 포함될 수 있어 기록하지 않습니다.
            raise StreamError("웹소켓 구독/세션 거절")
        if initializing:
            raise StreamError("웹소켓 초기화 실패")
        if msg.get("rt_cd") == "RT_INIT" or str(header.get("result", "")).lower() == "success":
            self.events.put(("WS_STATUS", {"status": "subscribed"}))
        else:
            self._data(msg)
        return False

    async def _session(self):
        def credentials():
            token = self.client.session.tokens.token("domestic", "paper")
            key = self.client.session.tokens.websocket_key("domestic", "paper")
            return key, token.access_token

        key, token = await asyncio.to_thread(credentials)
        url = f"wss://apidemo.futures.co.kr{STREAM_PATH}?access_key={key}"
        async with self.connector(url, ping_interval=None, open_timeout=10, close_timeout=2,
                                  max_size=2**22) as ws:
            self._invalidate()
            self.events.put(("WS_STATUS", {"status": "connected"}))
            await ws.send(json.dumps({"header": {"api_ver": "1", "action": "0"},
                                      "body": {"action_code": "session_init", "session_token": token}}))
            async with asyncio.timeout(10):
                while not await self._message(ws, await ws.recv(), initializing=True):
                    pass
            for symbol in self.symbols:
                await ws.send(json.dumps(subscription(symbol)))
            receiver = asyncio.create_task(self._receive(ws))
            stopper = asyncio.create_task(self._stop.wait())
            try:
                done, _ = await asyncio.wait({receiver, stopper}, return_when=asyncio.FIRST_COMPLETED)
                if receiver in done:
                    receiver.result()
                if self._stop.is_set():
                    for symbol in self.symbols:
                        await ws.send(json.dumps(subscription(symbol, "D")))
            finally:
                receiver.cancel()
                stopper.cancel()
                await asyncio.gather(receiver, stopper, return_exceptions=True)

    async def _receive(self, ws):
        while not self._stop.is_set():
            await self._message(ws, await asyncio.wait_for(ws.recv(), timeout=60))

    async def _run(self):
        attempts = 0
        while not self._stop.is_set():
            session = asyncio.create_task(self._session())
            stopper = asyncio.create_task(self._stop.wait())
            try:
                done, _ = await asyncio.wait({session, stopper}, return_when=asyncio.FIRST_COMPLETED)
                if stopper in done:
                    # 구독 해제를 마칠 시간을 주되, 초기화/접속 대기 중이면 취소합니다.
                    try:
                        await asyncio.wait_for(session, timeout=3)
                    except Exception:
                        pass
                    return
                await session
            except Exception as exc:
                self._invalidate()
                attempts += 1
                self.events.put(("WS_STATUS", {"status": "disconnected", "error_kind": type(exc).__name__}))
                # 인증/구독 거절은 무한히 같은 요청을 반복하지 않습니다.
                if isinstance(exc, StreamError):
                    self.fatal = True
                    self.events.put(("WS_STATUS", {"status": "fatal", "error_kind": type(exc).__name__}))
                    return
            finally:
                session.cancel()
                stopper.cancel()
                await asyncio.gather(session, stopper, return_exceptions=True)
            if not self._stop.is_set():
                delay = self.reconnect_delays[min(max(attempts - 1, 0), len(self.reconnect_delays) - 1)]
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

    def start(self):
        def run():
            async def main():
                self._loop = asyncio.get_running_loop()
                self._stop = asyncio.Event()
                if self._stopping.is_set():
                    self._stop.set()
                await self._run()
            asyncio.run(main())
        self._thread = threading.Thread(target=run, name="nh-domestic-quotes", daemon=True)
        self._thread.start()

    def stop(self):
        self._stopping.set()
        if self._loop and self._stop and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._stop.set)
            except RuntimeError:
                pass  # 종료 확인 직후 수신 스레드가 먼저 끝난 경우입니다.
        if self._thread:
            self._thread.join(timeout=15)
        self._invalidate()
