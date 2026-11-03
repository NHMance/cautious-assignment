"""대시보드 실시간 시세 — NH 웹소켓 `FA`(해외 시세체결)를 받아 브라우저 웹소켓(`/ws/live`)으로 넘깁니다.

    브라우저 ──ws──▶ /ws/live ──▶ LiveHub ──▶ RealtimeFeed(스레드) ──wss──▶ NH 실시간

- **브라우저가 하나라도 붙어 있을 때만 NH 에 접속합니다.** 마지막 브라우저가 떠나고 `IDLE_STOP_SEC` 가 지나면 끊습니다.
  같은 해외 모의 키로 봇 A 도 웹소켓을 쓰므로, 동시 접속을 되도록 짧게 두려는 것입니다.
- 토큰은 week02 토큰 파일(`~/NHFutures/config/tokens.json`)을 봇과 함께 씁니다. 새로 발급하지 않고 있으면 그대로 씁니다.
- 주문·계좌 조회는 하지 않습니다. 처음 한 번 REST 현재가로 실시간 종목코드와 마지막 체결가를 받습니다.
- `DASHBOARD_LIVE=0` 이면 접속하지 않습니다.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

IDLE_STOP_SEC = 30
KEEP_TICKS = 300
QUEUE_SIZE = 1000

Emit = Callable[[dict[str, Any]], None]


class Started:
    """`start` 함수가 돌려주는 것: 멈추는 함수와 처음 알려 줄 정보(종목·REST 현재가)."""

    def __init__(self, stop: Callable[[], None], info: dict[str, Any]) -> None:
        self.stop = stop
        self.info = info


Starter = Callable[[Emit], Started]


def enabled() -> bool:
    return os.environ.get("DASHBOARD_LIVE", "1").strip().lower() not in ("0", "false", "off", "no")


def nh_starter(emit: Emit) -> Started:
    """해외 모의 키로 NH 실시간 FA 구독을 시작합니다. 스레드에서 부릅니다(REST 호출이 있어서)."""
    import trader  # noqa: F401  (week02 경로 등록)
    from backend.nh_api import runtime
    from backend.nh_api.errors import NHApiError
    from backend.nh_api.session import create_session
    from backend.overseas_futureoption import overseas_futureoption_functions as api
    from bot.market import number
    from trader import credentials, settings
    from trader.realtime import FA, NHStream, RealtimeFeed, Subscription, parse_tick, session_credentials

    config, _source = credentials.load()          # 키가 없으면 ConfigError
    session = create_session(config=config)
    runtime.use(session)

    info: dict[str, Any] = {"symbol": settings.SYMBOL, "real_sym": settings.SYMBOL, "rest": None}
    try:
        raw = api.price([(settings.EXCH_CD, settings.SYMBOL)])
        rows = raw.get("Occurs1") if isinstance(raw.get("Occurs1"), list) else []
        row = rows[0] if rows and isinstance(rows[0], dict) else {}
        info["real_sym"] = str(row.get("occurs1_real_sym") or "").strip() or settings.SYMBOL
        info["name"] = str(row.get("occurs1_sym_nm") or "").strip()
        info["rest"] = {"price": number(row.get("occurs1_last_pric")),
                        "time": str(row.get("occurs1_trd_tm") or "").strip()}
    except NHApiError as exc:
        emit({"type": "status", "status": "rest_error", "detail": str(exc)})

    def on_data(code: str, msg: dict[str, Any]) -> None:
        if code != FA:
            return
        tick = parse_tick(msg)
        if tick is not None:
            emit({"type": "tick", **tick.to_dict()})

    def on_status(kind: str, detail: dict[str, Any]) -> None:
        # 실서버는 구독 성공을 {"code": "RT_INIT", ...} 으로 보냅니다(ws_check 원문). kind_of 로는 other 입니다.
        if kind == "other" and str(detail.get("code") or "") == "RT_INIT":
            kind = "ack"
        if kind in ("subscribe_sent", "other", "non_json"):
            return
        status = {"connected": "starting", "init": "connected", "ack": "subscribed"}.get(kind, kind)
        text = detail.get("error") or detail.get("msg") or ""
        emit({"type": "status", "status": status, "detail": str(text)[:300]})

    subs = [Subscription(FA, info["real_sym"])]
    feed = RealtimeFeed(
        lambda data, status: NHStream(credentials.ws_url(), session_credentials(session), subs, data, status),
        listeners=[on_data], on_status=on_status,
    )
    feed.start()

    def stop() -> None:
        feed.stop()
        session.close()

    return Started(stop, info)


class LiveHub:
    """브라우저 연결을 세고, 있으면 NH 실시간을 켜 두고, 받은 체결을 모두에게 나눠 줍니다."""

    def __init__(self, starter: Starter | None = None, *, idle_stop: float = IDLE_STOP_SEC,
                 enabled_: bool | None = None) -> None:
        self._starter = starter or nh_starter
        self._enabled = enabled() if enabled_ is None else enabled_
        self._idle_stop = idle_stop
        self._clients: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started: Started | None = None
        self._starting: asyncio.Task | None = None
        self._stopper: asyncio.TimerHandle | None = None
        self.status = "idle" if self._enabled else "disabled"
        self.detail = "" if self._enabled else "DASHBOARD_LIVE=0"
        self.info: dict[str, Any] = {}
        self.ticks: deque[dict[str, Any]] = deque(maxlen=KEEP_TICKS)

    # ── 브라우저 쪽 ─────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        return {"type": "snapshot", "status": self.status, "detail": self.detail,
                "info": self.info, "ticks": list(self.ticks)}

    async def join(self) -> asyncio.Queue:
        self._loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._clients.add(q)
        if self._stopper is not None:
            self._stopper.cancel()
            self._stopper = None
        if self._enabled and self._started is None and self._starting is None:
            self._starting = asyncio.create_task(self._start())
        return q

    async def leave(self, q: asyncio.Queue) -> None:
        self._clients.discard(q)
        if not self._clients and self._loop is not None and self._stopper is None:
            self._stopper = self._loop.call_later(self._idle_stop, lambda: asyncio.ensure_future(self.stop()))

    @property
    def clients(self) -> int:
        return len(self._clients)

    @property
    def running(self) -> bool:
        return self._started is not None

    # ── NH 쪽 ───────────────────────────────────────────────

    def _emit_threadsafe(self, event: dict[str, Any]) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._publish, event)

    def _publish(self, event: dict[str, Any]) -> None:
        if event.get("type") == "tick":
            self.ticks.append(event)
        elif event.get("type") == "status":
            self.status = event.get("status") or self.status
            self.detail = event.get("detail") or ""
        for q in list(self._clients):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # 느린 브라우저는 몇 건 건너뜁니다

    async def _start(self) -> None:
        self._publish({"type": "status", "status": "starting", "detail": ""})
        try:
            started = await asyncio.to_thread(self._starter, self._emit_threadsafe)
        except Exception as exc:  # noqa: BLE001 — 키 없음 등. 화면에 알리고 다음 접속 때 다시 시도합니다.
            name = type(exc).__name__
            status = "no_key" if name == "ConfigError" else "error"
            self._publish({"type": "status", "status": status, "detail": f"{name}: {exc}"[:300]})
            self._starting = None
            return
        self._starting = None
        if not self._clients:          # 켜는 사이 모두 떠났으면 바로 끕니다
            await asyncio.to_thread(started.stop)
            self._publish({"type": "status", "status": "idle", "detail": ""})
            return
        self._started = started
        self.info = started.info
        self._publish({"type": "info", "info": started.info})

    async def stop(self) -> None:
        self._stopper = None
        if self._clients:
            return
        started, self._started = self._started, None
        if started is not None:
            await asyncio.to_thread(started.stop)
        if self._enabled:
            self._publish({"type": "status", "status": "idle", "detail": ""})

    async def shutdown(self) -> None:
        if self._stopper is not None:
            self._stopper.cancel()
        self._clients.clear()
        await self.stop()
