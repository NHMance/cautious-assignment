"""실행 진입점.

    uv run python -m bot.main                     # 모의계좌 실주문 (설정 파일 default_environment: paper)
    uv run python -m bot.main --dry-run           # 조회만 실제, 주문은 기록만
    uv run python -m bot.main --replay-date 20261028  # 리플레이: 그날 실제 봉을 API 로 받아 가상 시계로 (주문 없음)
    uv run python -m bot.main --bot-test              # 봇 테스트: 목업 응답 + 가상 시계로 흐름만 점검 (서버·인증 없음)

루프
    세션 생성 → RUN_START → 잔고/미체결 확인으로 상태 복구
    15:20 전까지:
        5분 경계 + BAR_FETCH_DELAY_SEC 마다 확정봉 갱신 → strategy → (진입 신호면) risk → broker
        보유 중이면 PRICE_POLL_SEC 마다 손절·익절 감시
        15:15 이후 보유 중이면 강제 청산
    RUN_END
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import traceback
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any

from bot import risk, settings
from bot.clock import KST, RealClock, SimClock, at, floor_to
from bot.journal import RESULTS_DIR, Journal
from bot.strategy import LONG, SHORT, Action, State, Strategy

STOP, TAKE, FORCE = "STOP", "TAKE", "FORCE"

# RUN_END.stop_reason — 봇이 멈춘 까닭.
STOP_REASONS = {
    "DEADLINE": "15:20 정상 종료",
    "CTRL_C": "Ctrl+C 로 중단",
    "SIGTERM": "종료 신호(kill)로 중단",
    "SIGHUP": "터미널 창이 닫혀 중단",
    "CRASH": "예상하지 못한 오류로 중단",
}
# 이 신호가 오면 기록을 남기고 멈춥니다. 그냥 두면 RUN_END 없이 죽어 화면에 「진행 중」으로 남습니다.
_STOP_SIGNALS = tuple(s for s in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None)) if s)


class StopSignal(BaseException):
    """종료 신호. `Exception` 이 아니어야 루프 안의 「기록하고 이어 가기」에 삼켜지지 않습니다."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


def _raise_stop(signum: int, _frame: Any) -> None:
    raise StopSignal(signal.Signals(signum).name)


@dataclass
class Trade:
    side: str
    qty: int
    entry_price: float
    tick: float
    stop: float
    take: float
    entry_order: str | None
    entry_ts: str
    restored: bool = False

    @classmethod
    def open(cls, side: str, qty: int, price: float, order_no: str | None, ts: str, *, restored: bool = False) -> Trade:
        tick = settings.tick_size(price)
        sign = 1 if side == LONG else -1
        return cls(
            side=side,
            qty=qty,
            entry_price=price,
            tick=tick,
            stop=price - sign * settings.STOP_TICKS * tick,
            take=price + sign * settings.TAKE_TICKS * tick,
            entry_order=order_no,
            entry_ts=ts,
            restored=restored,
        )

    def trigger(self, price: float) -> str | None:
        if self.side == LONG:
            return STOP if price <= self.stop else TAKE if price >= self.take else None
        return STOP if price >= self.stop else TAKE if price <= self.take else None


class Bot:
    def __init__(self, *, symbol: str, market: Any, broker: Any, journal: Journal, clock: Any,
                 environment: str, day: date | None = None) -> None:
        self.symbol = symbol
        self.market = market
        self.broker = broker
        self.journal = journal
        self.clock = clock
        self.environment = environment
        self.day = day or clock.now().date()
        self.strategy = Strategy(self.day)
        self.journal.state = lambda: self.strategy.state.value
        self.trade: Trade | None = None
        self.exit_reason: str | None = None
        self.exit_attempts = 0

        self.force_start = at(self.day, settings.FORCE_CLOSE_START)
        self.deadline = at(self.day, settings.FORCE_CLOSE_DEADLINE)

    # ── 실행 ────────────────────────────────────────────────

    def run(self) -> None:
        self.journal.write("RUN_START", environment=self.environment, dry_run=self.broker.dry_run,
                           settings=settings.snapshot())
        interrupted = False
        stop_reason, stop_detail = "DEADLINE", None
        previous = self._catch_stop_signals()
        try:
            self.recover()
            next_fetch = self.clock.now()
            while (now := self.clock.now()) < self.deadline:
                if now >= next_fetch:
                    self.on_bar_tick(now)
                    next_fetch = self._next_fetch(self.clock.now())
                if self.trade is None and self.strategy.holding:
                    held = self._safe_position()
                    if held is not None:
                        self._restore_trade(held)
                if self.trade is not None:
                    self.watch_exit(self.clock.now())
                now = self.clock.now()
                if self.trade is not None:
                    wait = settings.PRICE_POLL_SEC
                else:
                    wait = min(max((next_fetch - now).total_seconds(), 0.5), (self.deadline - now).total_seconds())
                self.clock.sleep(max(wait, 0.0))
        except KeyboardInterrupt:
            interrupted = True
            stop_reason, stop_detail = "CTRL_C", "사용자가 Ctrl+C 를 눌렀습니다"
        except StopSignal as exc:
            interrupted = True
            stop_reason, stop_detail = exc.name, f"{exc.name} 신호를 받았습니다"
        except Exception as exc:
            # 루프 안에서 기록하고 이어 가는 오류(API 등)가 아닌 것. 기록을 남기고 그대로 밖으로 내보냅니다.
            stop_reason, stop_detail = "CRASH", f"{type(exc).__name__}: {exc}"
            self._safe_write("ERROR", api="bot", code="CRASH", message=stop_detail,
                             traceback=traceback.format_exc(limit=6))
            raise
        finally:
            self._restore_stop_signals(previous)
            if stop_reason != "DEADLINE":
                held = self._safe_position()
                msg = f"보유 중: {held.side} {held.qty}계약 — 자동 청산하지 않습니다. 직접 확인하세요!" if held else "보유 포지션 없음"
                self._say(f"\n[중단] {STOP_REASONS[stop_reason]} ({stop_detail}). {msg}")
            if stop_reason == "DEADLINE" and self.trade is not None:
                self.journal.write("ERROR", api="bot", code="POSITION_LEFT",
                                   message="15:20 까지 청산을 확인하지 못했습니다", trade=asdict(self.trade))
            held = self._safe_position()
            self._safe_write("RUN_END", environment=self.environment, dry_run=self.broker.dry_run,
                             interrupted=interrupted, stop_reason=stop_reason, stop_detail=stop_detail,
                             entries=self.strategy.entries,
                             position=asdict(held) if held else None, done_reason=self.strategy.done_reason,
                             H=self.strategy.high, L=self.strategy.low, settings=settings.snapshot())

    # ── 멈출 때 ─────────────────────────────────────────────

    @staticmethod
    def _catch_stop_signals() -> dict[int, Any]:
        """SIGTERM·SIGHUP 을 StopSignal 로 바꿉니다. 돌려놓을 수 있게 전의 처리기를 돌려줍니다."""
        previous: dict[int, Any] = {}
        for sig in _STOP_SIGNALS:
            try:
                previous[sig] = signal.signal(sig, _raise_stop)
            except ValueError:  # 주 스레드가 아니면 신호를 받을 수 없습니다
                break
        return previous

    @staticmethod
    def _restore_stop_signals(previous: dict[int, Any]) -> None:
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    def _safe_write(self, type_: str, **fields: Any) -> None:
        """멈추는 길에서 쓰는 기록. 터미널이 닫혀 화면 출력이 실패해도 파일 기록은 남기고 넘어갑니다."""
        try:
            self.journal.write(type_, **fields)
        except OSError:
            pass

    @staticmethod
    def _say(text: str) -> None:
        try:
            print(text, flush=True)
        except OSError:  # 터미널이 이미 닫혔습니다
            pass

    def _next_fetch(self, now: datetime) -> datetime:
        delay = timedelta(seconds=settings.BAR_FETCH_DELAY_SEC)
        candidate = floor_to(now, settings.BAR_MINUTES) + delay
        if candidate <= now:
            candidate += timedelta(minutes=settings.BAR_MINUTES)
        return candidate

    # ── 복구 ────────────────────────────────────────────────

    def recover(self) -> None:
        """재시작: 오늘 진입 횟수는 journal 에서, 보유는 open_interest 에서 되살립니다."""
        earlier = self.journal.today()
        entries = sum(1 for r in earlier if r.get("type") == "FILL" and r.get("role") == "ENTRY"
                      and not r.get("partial"))
        self.strategy.entries = entries

        held = self._safe_position()
        try:
            pending = self.broker.pending_count()
        except Exception as exc:  # noqa: BLE001 — 기록하고 진행
            self.journal.error("domestic_order-history", exc, note="시작 시 미체결 확인")
            pending = None
        if held is not None:
            self.strategy.restore_position(held.side)
            self._restore_trade(held)
        self.journal.write("RECOVER", entries_today=self.strategy.entries,
                           position=asdict(held) if held else None, pending_orders=pending,
                           trade=asdict(self.trade) if self.trade else None)

    def _restore_trade(self, held: Any) -> None:
        """잔고 기준으로 손절·익절가를 다시 세웁니다. 기준 가격을 못 얻으면 다음 루프에서 다시 시도합니다."""
        price = held.avg_price or self._safe_price()
        if price:
            self.trade = Trade.open(held.side, held.qty, price, None, self.clock.now().isoformat(), restored=True)

    # ── 봉 ──────────────────────────────────────────────────

    def on_bar_tick(self, now: datetime) -> None:
        try:
            bars = self.market.get_bars_5m(self.day, now)
        except Exception as exc:  # noqa: BLE001
            self.journal.error("domestic_chart-minute", exc)
            return
        decision = self.strategy.on_bars(bars, now)
        for event in decision.events:
            self.journal.write(event.pop("type"), **event)
        if decision.action is not Action.NONE:
            self.enter(LONG if decision.action is Action.ENTER_LONG else SHORT)

    # ── 진입 ────────────────────────────────────────────────

    def enter(self, side: str) -> None:
        try:
            price = self.market.get_price()
            held = self.broker.position()
            pending = self.broker.pending_count()
            orderable = self.broker.orderable_qty(side, price)
        except Exception as exc:  # noqa: BLE001
            self.journal.error(getattr(exc, "api_id", "risk_inputs"), exc, side=side)
            self._entry_failed()
            return

        x = risk.RiskInput(
            price=price,
            tick=settings.tick_size(price),
            orderable_qty=orderable,
            position_qty=held.qty if held else 0,
            pending_orders=pending,
            entries_today=self.strategy.entries,
        )
        failures = risk.evaluate(x)
        if failures:
            for failure in failures:
                self.journal.write("RISK_BLOCK", side=side, price=price, **failure)
            self._entry_failed()
            return

        order_no = self.broker.submit(side, settings.MAX_QTY, role="ENTRY")
        fill = None
        if order_no:
            fill = self.broker.wait_fill(order_no, side=side, qty=settings.MAX_QTY, ref_price=price, role="ENTRY")

        held = self._safe_position()
        if fill is None:
            if held is not None:
                # 체결 확인은 못 했지만 잔고가 생겼습니다. 잔고 기준으로 보유 상태로 이어 갑니다.
                self.journal.write("ERROR", api="bot", code="FILL_UNCONFIRMED_BUT_HELD",
                                   message="체결 확인 실패, 잔고에는 포지션 있음 — 잔고 평균가로 관리", position=asdict(held))
                self._adopt(held, order_no)
            else:
                self._entry_failed()
            return

        if held is None or held.side != side:
            self.journal.write("ERROR", api="domestic_open-interest", code="POSITION_MISMATCH",
                               message="체결 후 잔고 교차 확인 불일치", fill=asdict(fill),
                               position=asdict(held) if held else None)
        self.trade = Trade.open(side, fill.qty, fill.price, order_no, self.clock.now().isoformat())
        self.strategy.on_entry_filled(side)
        self.journal.write("ENTRY_OPEN", trade=asdict(self.trade),
                           position=asdict(held) if held else None)

    def _adopt(self, held: Any, order_no: str | None) -> None:
        price = held.avg_price or self._safe_price() or 0.0
        self.trade = Trade.open(held.side, held.qty, price, order_no, self.clock.now().isoformat(), restored=True)
        self.strategy.on_entry_filled(held.side)
        self.journal.write("ENTRY_OPEN", trade=asdict(self.trade), position=asdict(held))

    def _entry_failed(self) -> None:
        for event in self.strategy.on_entry_failed(self.clock.now()):
            self.journal.write(event.pop("type"), **event)

    # ── 청산 ────────────────────────────────────────────────

    def watch_exit(self, now: datetime) -> None:
        trade = self.trade
        if trade is None:
            return
        if now >= self.force_start and self.exit_reason != FORCE:
            self.exit_reason = FORCE
            self.exit_attempts = 0
        price = self._safe_price()
        if self.exit_reason is None and price is not None:
            self.exit_reason = trade.trigger(price)
        if self.exit_reason is not None:
            self.exit(self.exit_reason, price)

    def exit(self, reason: str, trigger_price: float | None) -> None:
        trade = self.trade
        assert trade is not None
        self.exit_attempts += 1
        self.journal.write("EXIT_TRIGGER", reason=reason, trigger_price=trigger_price, stop=trade.stop,
                           take=trade.take, entry_price=trade.entry_price, attempt=self.exit_attempts)
        self.strategy.on_exit_started()

        try:
            held = self.broker.position()
        except Exception as exc:  # noqa: BLE001
            self.journal.error("domestic_open-interest", exc, note="청산 전 잔고 확인")
            self.strategy.on_exit_failed()
            return
        if held is None:
            self.journal.write("ERROR", api="domestic_open-interest", code="ALREADY_FLAT",
                               message="청산하려 했으나 잔고가 없습니다", trade=asdict(trade))
            self._closed(trade, reason, None, None)
            return

        side = LONG if held.side == SHORT else SHORT
        order_no = self.broker.submit(side, held.qty, role="EXIT")
        fill = None
        if order_no:
            fill = self.broker.wait_fill(order_no, side=side, qty=held.qty, ref_price=trigger_price, role="EXIT")
        if fill is None:
            self.strategy.on_exit_failed()
            return

        after = self._safe_position()
        if after is not None:
            self.journal.write("ERROR", api="domestic_open-interest", code="NOT_FLAT",
                               message="청산 체결 후에도 잔고가 남았습니다", position=asdict(after))
            self.strategy.on_exit_failed()
            return
        self._closed(trade, reason, fill.price, order_no)

    def _closed(self, trade: Trade, reason: str, exit_price: float | None, order_no: str | None) -> None:
        sign = 1 if trade.side == LONG else -1
        record: dict[str, Any] = {
            "side": trade.side,
            "qty": trade.qty,
            "reason": reason,
            "entry_price": trade.entry_price,
            "exit_price": exit_price,
            "stop": trade.stop,
            "take": trade.take,
            "tick": trade.tick,
            "entry_order": trade.entry_order,
            "exit_order": order_no,
            "entry_ts": trade.entry_ts,
            "restored": trade.restored,
        }
        if exit_price is not None:
            diff = (exit_price - trade.entry_price) * sign
            gross = diff * settings.MULTIPLIER * trade.qty
            record.update(
                ticks=round(diff / trade.tick, 2),
                pnl_krw=round(gross),
                pnl_net_krw=round(gross - settings.EST_ROUNDTRIP_COST),
            )
            planned = {STOP: trade.stop, TAKE: trade.take}.get(reason)
            if planned is not None:
                # 계획 가격 대비 실제 체결가. 음수면 불리하게 체결(슬리피지).
                record["slippage_ticks"] = round((exit_price - planned) * sign / trade.tick, 2)
        self.journal.write("TRADE_CLOSED", **record)
        self.trade = None
        self.exit_reason = None
        self.exit_attempts = 0
        for event in self.strategy.on_closed(self.clock.now()):
            self.journal.write(event.pop("type"), **event)

    # ── 보조 ────────────────────────────────────────────────

    def _safe_price(self) -> float | None:
        try:
            return self.market.get_price()
        except Exception as exc:  # noqa: BLE001
            self.journal.error("domestic_price", exc)
            return None

    def _safe_position(self) -> Any:
        try:
            return self.broker.position()
        except Exception as exc:  # noqa: BLE001
            self.journal.error("domestic_open-interest", exc)
            return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="장 초반 가격 돌파 재확인 전략 봇")
    parser.add_argument("--dry-run", action="store_true", help="주문 API 를 부르지 않고 기록만")
    parser.add_argument("--bot-test", action="store_true",
                        help="봇 테스트: 목업 응답(tests/fixtures/nh) + 가상 시계로 흐름만 점검. NH_REPLAY=1 과 같음")
    parser.add_argument("--symbol", default=None, help="종목코드 (기본: settings.SYMBOL)")
    parser.add_argument("--replay-date", default=None, metavar="YYYYMMDD",
                        help="리플레이: 그날 봉을 모의 서버에서 받아 가상 시계로 돌림. 주문 없음(DRY_RUN)")
    args = parser.parse_args(argv)

    replay_day: date | None = None
    if args.replay_date:
        if args.bot_test:
            print("--replay-date 와 --bot-test 는 함께 쓸 수 없습니다.")
            return 2
        try:
            replay_day = datetime.strptime(args.replay_date, "%Y%m%d").date()
        except ValueError:
            print(f"--replay-date 는 YYYYMMDD 형식입니다: {args.replay_date!r}")
            return 2
        from bot.backtest import replay_day_problem

        problem = replay_day_problem(replay_day, RealClock().now())
        if problem:
            print(f"리플레이할 수 없습니다: {problem}")
            return 2

    # 봇 테스트는 참고 저장소 backend 의 재생 모드(NH_REPLAY=1)를 그대로 씁니다.
    if args.bot_test:
        os.environ["NH_REPLAY"] = "1"

    # 환경변수를 정한 뒤에 불러옵니다.
    from backend.nh_api import runtime
    from backend.nh_api.session import create_session, replay_enabled
    from bot.broker import Broker
    from bot.market import Market

    bot_test = replay_enabled()
    dry_run = args.dry_run or settings.DRY_RUN
    symbol = args.symbol or (settings.BOT_TEST_SYMBOL if bot_test else settings.SYMBOL)
    if not symbol:
        print("settings.SYMBOL 이 비어 있습니다. scripts/find_symbol.py 로 종목코드를 찾아 넣거나 --symbol 을 주세요.")
        return 2

    if bot_test:
        session = create_session()
    else:
        from bot import credentials

        from backend.config import ConfigError

        try:
            config, source = credentials.load()
        except ConfigError as exc:
            print(exc)
            return 2
        print(f"인증 정보: {source}")
        session = create_session(config=config)
    client = runtime.use(session)
    if bot_test:
        # 호출 제한기는 실제 시간으로 기다립니다. 봇 테스트는 서버로 가지 않고 가상 시계로 하루를 돌므로,
        # 보유 중 2초마다 현재가를 보면 대기만 수십 분이 쌓입니다. 봇 테스트에서만 대기를 건너뜁니다.
        from backend.nh_api.rate_limit import RateLimiter

        client._limiter = RateLimiter(sleep=lambda _seconds: None)
    environment = "bot-test" if bot_test else session.config.default_environment
    if not bot_test and environment != "paper":
        print(f"기본 환경이 '{environment}' 입니다. 이 봇은 모의(paper)에서만 돌립니다. 설정 파일을 확인하세요.")
        return 2
    if bot_test and not dry_run:
        print("봇 테스트에서는 주문을 재생할 수 없어 --dry-run 으로 돕니다.")
        dry_run = True

    if replay_day is not None:
        from bot.backtest import REPLAY_DIR, HistoricalMarket, SimBroker

        clock = SimClock(datetime(replay_day.year, replay_day.month, replay_day.day, 8, 55, tzinfo=KST))
        journal = Journal(symbol, clock=clock, root=REPLAY_DIR)
        try:
            market: Any = HistoricalMarket(symbol, replay_day, clock)
        except Exception as exc:
            session.close()
            print(f"{replay_day} 봉을 받지 못했습니다: {exc}")
            return 2
        if not market.bars_5m or not market.bars_1m:
            session.close()
            print(f"{replay_day} {symbol} 봉이 없습니다(5분봉 {len(market.bars_5m)}개, 1분봉 {len(market.bars_1m)}개). "
                  "공휴일이거나, 그날 거래되지 않은 종목코드일 수 있습니다. 전에 남긴 기록은 그대로 둡니다.")
            return 2
        last = market.bars_5m[-1]
        if last.end < at(replay_day, settings.FORCE_CLOSE_DEADLINE):
            print(f"주의: 5분봉이 {last.end:%H:%M} 까지만 있습니다. 그 뒤로는 새 봉 없이 돕니다.")
        # 봉을 받은 뒤에야 지웁니다. 다시 돌리면 지난 실행의 진입 횟수를 되살려 버리므로 새로 씁니다.
        journal.path().unlink(missing_ok=True)
        print(f"봉 데이터: 5분봉 {len(market.bars_5m)}개, 1분봉 {len(market.bars_1m)}개 → {market.path}")
        bot = Bot(symbol=symbol, market=market, broker=SimBroker(symbol, journal, clock),
                  journal=journal, clock=clock, environment="replay", day=replay_day)
        try:
            bot.run()
        finally:
            session.close()
        print(f"기록: {journal.path()}")
        return 0

    if bot_test:
        # 「같은 기록이 없어 다른 날 기록으로 답합니다」 경고가 봉 조회마다 찍혀 기록을 가립니다.
        logging.getLogger("backend.nh_api.replay").setLevel(logging.ERROR)
        day = datetime.strptime(settings.BOT_TEST_DATE, "%Y%m%d").date()
        clock: Any = SimClock(datetime(day.year, day.month, day.day, 8, 55, tzinfo=KST))
    else:
        clock = RealClock()
        day = clock.now().date()

    # 봇 테스트·DRY_RUN 기록은 모의계좌 실주문 기록(results/)과 섞이지 않게 따로 둡니다.
    # 같은 파일에 섞이면 실주문 봇이 재시작 복구로 DRY 체결까지 오늘 진입 횟수로 셉니다.
    if bot_test:
        root = RESULTS_DIR / "bot-test"
    elif dry_run:
        root = RESULTS_DIR / "dry-run"
    else:
        root = RESULTS_DIR
    journal = Journal(symbol, clock=clock, root=root)
    if bot_test:
        # 리플레이처럼 매번 새로 씁니다. 이어 쓰면 지난 실행의 진입 횟수를 되살려 버립니다.
        journal.path().unlink(missing_ok=True)
    bot = Bot(symbol=symbol, market=Market(symbol), broker=Broker(symbol, journal, clock, dry_run=dry_run),
              journal=journal, clock=clock, environment=environment, day=day)
    try:
        bot.run()
    finally:
        session.close()
    print(f"기록: {journal.path()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
