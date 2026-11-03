"""NH선물 실시간(웹소켓) — 해외 시세체결 `FA`·시세호가 `FB`·주문/체결 `O1`.

절차는 개발자센터 「소켓 연결 및 세션 초기화 안내」와 해외 실시간 명세(FA·O1)를 따릅니다.

1. 접근 토큰 발급 → `GET /permission/v1/web-socket-key` 로 웹소켓 키(`access_key`) 발급.
   **웹소켓 키는 접속 한 번에 한 번만 씁니다.** 다시 접속할 때마다 새로 받습니다.
2. `wss://apidemo.futures.co.kr/trade/ws-stream?access_key=...` 에 접속.
3. 세션 초기화 — `{"header":{"api_ver":"1","action":"0"},"body":{"action_code":"session_init","session_token":<access_token>}}`
   → `{"code":"INIT","msg":"INIT_SUCCESS"}` 를 받은 뒤에야 구독을 보냅니다.
4. 구독 — `{"header":{"api_ver":"1","action":"A"},"body":{"action_code":"FA","action_sym":"MKUX26"}}`
   (`action` A 구독 / D 해제, 주문/체결 `O1` 은 종목 없이). 동시 구독은 FA·FB·O1 합쳐 20건까지.
5. 받는 메시지는 셋입니다.
   - `{"code":"pingpong"}` → **빈 문자열을 보내** 답합니다. 안 하면 서버가 끊습니다.
   - 구독 결과 — `rt_cd` 가 `RT_INIT`(성공)/`RT_FAIL`(실패). 안내 예제에는 `header.result == "success"` 모양도 있어 둘 다 봅니다.
   - 실시간 데이터 — `header` 와 `body` 가 함께 옵니다. FA 의 값은 `last_pric`·`exec_qty`·`ko_exec_tm`·
     `exch_tm`·`ask_best_pric`·`bid_best_pric` 등(명세의 출력 칸).

명세에 응답 예시가 비어 있어, **값이 body 안에 오는지 바깥에 오는지는 실제 메시지로 확인해야 합니다**
(`scripts/ws_check.py` 가 원문을 남깁니다). 그래서 `field()` 는 body 를 먼저 보고 없으면 바깥을 봅니다.

끊기면 1·2·5·10·30초 간격으로 다시 접속하고 구독을 다시 겁니다. 일정 시간(기본 60초) 아무 메시지가
없으면 끊긴 것으로 보고 다시 접속합니다 — 서버의 pingpong 이 그보다 자주 옵니다.

봇 루프는 동기 코드라 `RealtimeFeed` 가 별도 스레드에서 이벤트 루프를 돌리고 마지막 체결만 들고 있습니다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from typing import Any

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

import trader  # noqa: F401  (week02 경로 등록)
from backend.auth.auth_functions import AuthError
from backend.nh_api.catalog import STREAM_PATH, STREAM_SUBSCRIPTION_LIMIT
from bot.clock import KST  # week02
from bot.market import number  # week02

logger = logging.getLogger(__name__)

API_VER = "1"
SUBSCRIBE, UNSUBSCRIBE = "A", "D"
FA, FB, O1 = "FA", "FB", "O1"

INIT_OK = ("INIT", "INIT_SUCCESS")
PINGPONG = "pingpong"
RT_INIT, RT_FAIL = "RT_INIT", "RT_FAIL"

RECONNECT_DELAYS = (1, 2, 5, 10, 30)
INIT_TIMEOUT_SEC = 10
IDLE_TIMEOUT_SEC = 60


class StreamError(Exception):
    """세션 초기화 실패처럼 접속을 이어 갈 수 없는 경우."""


@dataclass(frozen=True)
class Subscription:
    action_code: str
    action_sym: str | None = None

    def message(self, action: str = SUBSCRIBE) -> dict[str, Any]:
        body: dict[str, Any] = {"action_code": self.action_code}
        if self.action_sym:
            body["action_sym"] = self.action_sym
        return {"header": {"api_ver": API_VER, "action": action}, "body": body}


def init_message(access_token: str) -> dict[str, Any]:
    return {"header": {"api_ver": API_VER, "action": "0"},
            "body": {"action_code": "session_init", "session_token": access_token}}


# ── 메시지 해석 ─────────────────────────────────────────────

def field(msg: dict[str, Any], name: str) -> Any:
    """값을 body 에서 먼저, 없으면 바깥에서 찾습니다."""
    body = msg.get("body")
    if isinstance(body, dict) and name in body:
        return body[name]
    return msg.get(name)


def kind_of(msg: dict[str, Any]) -> str:
    """`pingpong` / `init` / `ack` / `fail` / `data` / `other`."""
    code = str(msg.get("code") or "")
    if code == PINGPONG:
        return "pingpong"
    if (code, str(msg.get("msg") or "")) == INIT_OK:
        return "init"
    rt_cd = str(msg.get("rt_cd") or "")
    header = msg.get("header") if isinstance(msg.get("header"), dict) else None
    if rt_cd == RT_FAIL or (header and str(header.get("result") or "").lower() == "fail"):
        return "fail"
    if rt_cd == RT_INIT or (header and str(header.get("result") or "").lower() == "success"):
        return "ack"
    if header is not None:
        return "data"
    return "other"


def action_code_of(msg: dict[str, Any]) -> str:
    """데이터 메시지가 어느 채널의 것인지. header·body 어느 쪽에 와도 찾습니다."""
    for place in (msg.get("header"), msg.get("body"), msg):
        if isinstance(place, dict):
            for key in ("action_code", "tr_cd", "tr_id"):
                if place.get(key):
                    return str(place[key])
    # 채널 표시가 없으면 칸으로 짐작합니다.
    if field(msg, "last_pric") is not None:
        return FA
    if field(msg, "ord_no") is not None:
        return O1
    if field(msg, "ask_pric_1") is not None:
        return FB
    return ""


@dataclass(frozen=True)
class Tick:
    """FA 체결 한 건. `received` 는 이 컴퓨터가 받은 시각(KST)."""

    received: datetime
    sym: str
    price: float
    qty: float | None
    ko_exec_tm: str
    exch_tm: str
    bid: float | None
    ask: float | None

    def to_dict(self) -> dict[str, Any]:
        return {"received": self.received.isoformat(timespec="milliseconds"), "sym": self.sym, "price": self.price,
                "qty": self.qty, "ko_exec_tm": self.ko_exec_tm, "exch_tm": self.exch_tm,
                "bid": self.bid, "ask": self.ask}


def parse_tick(msg: dict[str, Any], *, received: datetime | None = None) -> Tick | None:
    price = number(field(msg, "last_pric"))
    if price is None or price <= 0:
        return None
    return Tick(
        received=received or datetime.now(tz=KST),
        sym=str(field(msg, "sym") or field(msg, "action_sym") or "").strip(),
        price=price,
        qty=number(field(msg, "exec_qty")),
        ko_exec_tm=str(field(msg, "ko_exec_tm") or "").strip(),
        exch_tm=str(field(msg, "exch_tm") or "").strip(),
        bid=number(field(msg, "bid_best_pric")),
        ask=number(field(msg, "ask_best_pric")),
    )


# ── 접속 ────────────────────────────────────────────────────

# 접속할 때마다 (웹소켓 키, 접근 토큰) 을 새로 얻는 함수. 동기 함수(HTTP 호출)라 스레드에서 부릅니다.
Credentials = Callable[[], tuple[str, str]]


def session_credentials(session: Any, *, market: str = "overseas", environment: str = "paper") -> Credentials:
    """week02 backend 세션의 토큰 관리자로 키를 받습니다. 토큰은 만료가 가까우면 관리자가 새로 받습니다."""

    def get() -> tuple[str, str]:
        token = session.tokens.token(market, environment)
        return session.tokens.websocket_key(market, environment), token.access_token

    return get


@dataclass
class NHStream:
    ws_url: str                                         # wss://apidemo.futures.co.kr
    credentials: Credentials
    subscriptions: list[Subscription]
    on_data: Callable[[dict[str, Any]], None]
    on_status: Callable[[str, dict[str, Any]], None] = lambda _kind, _detail: None
    reconnect_delays: tuple[float, ...] = RECONNECT_DELAYS
    idle_timeout: float = IDLE_TIMEOUT_SEC
    connect: Callable[..., Any] = ws_connect
    max_attempts: int | None = None                     # None 이면 멈출 때까지 다시 접속
    connections: int = dc_field(default=0, init=False)

    def __post_init__(self) -> None:
        if len(self.subscriptions) > STREAM_SUBSCRIPTION_LIMIT:
            raise ValueError(f"동시 구독은 {STREAM_SUBSCRIPTION_LIMIT}건까지입니다: {len(self.subscriptions)}건")

    async def run(self, stop: asyncio.Event) -> None:
        failures = 0
        attempts = 0
        while not stop.is_set():
            attempts += 1
            try:
                await self._session(stop)
                failures = 0
            except (OSError, ConnectionClosed, StreamError, asyncio.TimeoutError) as exc:
                self.on_status("disconnected", {"error": f"{type(exc).__name__}: {exc}", "attempt": attempts})
                failures += 1
            except AuthError as exc:
                # 키·시크릿이 틀려 거부된 것(EAU0xx)은 다시 해도 같습니다. 네트워크 실패만 다시 시도합니다.
                self.on_status("error", {"error": f"{type(exc).__name__}: {exc}", "attempt": attempts})
                if "거부" in str(exc):
                    self.on_status("fatal", {"error": "인증이 거부되어 다시 접속하지 않습니다"})
                    return
                failures += 1
            except Exception as exc:  # noqa: BLE001 — 기록하고 다시 시도합니다.
                self.on_status("error", {"error": f"{type(exc).__name__}: {exc}", "attempt": attempts})
                failures += 1
            if stop.is_set() or (self.max_attempts is not None and attempts >= self.max_attempts):
                return
            delay = self.reconnect_delays[min(max(failures - 1, 0), len(self.reconnect_delays) - 1)]
            self.on_status("reconnect_wait", {"seconds": delay})
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _session(self, stop: asyncio.Event) -> None:
        access_key, access_token = await asyncio.to_thread(self.credentials)
        url = f"{self.ws_url.rstrip('/')}{STREAM_PATH}?access_key={access_key}"
        # 서버가 앱 수준 pingpong 을 쓰므로 웹소켓 프로토콜 ping 은 끕니다.
        async with self.connect(url, ping_interval=None, max_size=2**22) as ws:
            self.connections += 1
            self.on_status("connected", {"connection": self.connections})
            await ws.send(json.dumps(init_message(access_token)))
            await self._wait_init(ws)
            for sub in self.subscriptions:
                await ws.send(json.dumps(sub.message()))
                self.on_status("subscribe_sent", {"action_code": sub.action_code, "action_sym": sub.action_sym})

            stopper = asyncio.ensure_future(stop.wait())
            try:
                while not stop.is_set():
                    recv = asyncio.ensure_future(ws.recv())
                    done, _ = await asyncio.wait({recv, stopper}, timeout=self.idle_timeout,
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if stopper in done:
                        recv.cancel()
                        break
                    if recv not in done:
                        recv.cancel()
                        raise asyncio.TimeoutError(f"{self.idle_timeout:g}초 동안 메시지가 없습니다")
                    await self._handle(ws, recv.result())
                # 수신 처리 중에 멈춤 신호가 와도 여기로 옵니다. 정상 종료면 구독을 풀고 나갑니다.
                await self._unsubscribe(ws)
            finally:
                stopper.cancel()

    async def _wait_init(self, ws: Any) -> None:
        deadline = asyncio.get_running_loop().time() + INIT_TIMEOUT_SEC
        while True:
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                raise StreamError("세션 초기화 응답(INIT_SUCCESS)을 받지 못했습니다")
            raw = await asyncio.wait_for(ws.recv(), timeout=left)
            msg = _loads(raw)
            kind = kind_of(msg) if msg is not None else "other"
            if kind == "init":
                self.on_status("init", msg)
                return
            if kind == "pingpong":
                await ws.send("")
                continue
            raise StreamError(f"세션 초기화 실패: {raw!r}")

    async def _handle(self, ws: Any, raw: Any) -> None:
        msg = _loads(raw)
        if msg is None:
            self.on_status("non_json", {"raw": str(raw)[:200]})
            return
        kind = kind_of(msg)
        if kind == "pingpong":
            await ws.send("")
        elif kind == "data":
            self.on_data(msg)
        else:
            self.on_status(kind, msg)

    async def _unsubscribe(self, ws: Any) -> None:
        for sub in self.subscriptions:
            try:
                await ws.send(json.dumps(sub.message(UNSUBSCRIBE)))
            except ConnectionClosed:
                return


def _loads(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


# ── 동기 봇 루프용 ──────────────────────────────────────────

class RealtimeFeed:
    """백그라운드 스레드에서 `NHStream` 을 돌리고 마지막 체결을 들고 있습니다.

        feed = RealtimeFeed(stream_factory)   # on_data·on_status 를 받아 NHStream 을 만드는 함수
        feed.start(); feed.latest(); feed.stop()

    `listeners` 는 데이터 메시지마다 부릅니다(체결 기록, 주문/체결 알림 처리 등). 스레드에서 불리므로 짧게 둡니다.
    """

    def __init__(self, make_stream: Callable[[Callable[[dict[str, Any]], None],
                                              Callable[[str, dict[str, Any]], None]], NHStream],
                 *, listeners: Iterable[Callable[[str, dict[str, Any]], None]] = (),
                 on_status: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self._make = make_stream
        self._listeners = list(listeners)
        self._status = on_status or (lambda kind, detail: logger.info("ws %s %s", kind, detail))
        self._lock = threading.Lock()
        self._latest: Tick | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self.ready = threading.Event()   # 첫 INIT_SUCCESS

    def latest(self) -> Tick | None:
        with self._lock:
            return self._latest

    def _on_data(self, msg: dict[str, Any]) -> None:
        code = action_code_of(msg)
        if code == FA:
            tick = parse_tick(msg)
            if tick is not None:
                with self._lock:
                    self._latest = tick
        for listener in self._listeners:
            try:
                listener(code, msg)
            except Exception:  # noqa: BLE001 — 듣는 쪽 오류로 수신을 멈추지 않습니다
                logger.exception("실시간 처리 오류")

    def _on_status(self, kind: str, detail: dict[str, Any]) -> None:
        if kind == "init":
            self.ready.set()
        self._status(kind, detail)

    def start(self) -> None:
        def main() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._stop = asyncio.Event()
            stream = self._make(self._on_data, self._on_status)
            self._loop.run_until_complete(stream.run(self._stop))
            self._loop.close()

        self._thread = threading.Thread(target=main, name="nh-realtime", daemon=True)
        self._thread.start()

    def alive(self) -> bool:
        """수신 스레드가 도는 중인지. 인증 거부처럼 다시 접속하지 않는 오류면 False."""
        return self._thread is not None and self._thread.is_alive()

    def stop(self, timeout: float = 5.0) -> None:
        if self._loop and self._stop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread:
            self._thread.join(timeout)


class TickRecorder:
    """FA 체결을 `results/ticks/ticks_<종목>_YYYYMMDD.jsonl` 에 한 줄씩 남깁니다.

    B 가 A 의 기록을 따라 읽으며 손절·익절을 다시 판정할 때 쓰는 가격 원본입니다. `RealtimeFeed` 의 listener 로 겁니다.
    """

    def __init__(self, root: Any, symbol: str) -> None:
        self.root = root
        self.symbol = symbol
        self._lock = threading.Lock()

    def path(self, day: datetime) -> Any:
        return self.root / f"ticks_{self.symbol}_{day:%Y%m%d}.jsonl"

    def __call__(self, code: str, msg: dict[str, Any]) -> None:
        if code != FA:
            return
        tick = parse_tick(msg)
        if tick is None:
            return
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.path(tick.received).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(tick.to_dict(), ensure_ascii=False) + "\n")
