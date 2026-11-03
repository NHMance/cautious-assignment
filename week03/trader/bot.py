"""전략 A 실행 루프 — 모의계좌에 실제로 주문합니다(또는 DRY_RUN·리플레이·봇 테스트).

    세션 시간 확인 → RUN_START → 잔고/journal 로 상태 복구 → 장 시작까지 대기
    장 마감 전까지:
        5분 경계 + BAR_FETCH_DELAY_SEC 마다 확정봉 갱신 → strategy
            반대 교차면 청산, 진입 신호면 리스크 확인 → 시장가 주문 → 체결가로 손절·익절가
        보유 중이면 PRICE_POLL_SEC 마다 손절·익절 감시 (실시간 체결 우선, 없으면 REST 현재가)
        마감 30분 전부터 보유 중이면 강제 청산
    RUN_END

B 는 이 파일의 journal 을 읽어 다시 계산합니다(`trader/recompute.py`). 그래서 봉마다 지표를 남기고,
진입·청산 기록에 신호 봉 시각(`signal_bar`)을 붙입니다.

거래 한 건을 지키는 장치 (주문 → 체결 → 잔고가 어긋나도 포지션을 놓지 않게)
- **잔고 맞춤**: 봉마다, 관리 중인 거래가 없는데 잔고에 포지션이 있으면(`ORPHAN_POSITION`) 진입 1회로 세고
  잔고 평균가·현재 ATR 로 손절·익절가를 세워 관리합니다. 체결 확인 실패·응답 없는 주문이 늦게 체결된 경우입니다.
- **중복 청산 방지**: 청산 주문 전에 미체결을 봅니다. 앞선 주문이 남아 있으면 새로 내지 않고 기다립니다
  (`EXIT_WAIT_PENDING`). 같은 청산이 두 번 체결돼 반대 포지션이 생기는 것을 막습니다. `EXIT_PENDING_WAIT_ATTEMPTS`
  번 기다려도 남아 있으면 미체결을 취소하고(`EXIT_CANCEL_PENDING`) 다음 시도에서 잔고부터 다시 봅니다.
- **루프 격리**: 한 바퀴에서 예상 못한 예외가 나도 기록(`LOOP_ERROR`)하고 이어 갑니다. 보유 중 감시가 멈추지 않게 합니다.
  연속 `MAX_CONSECUTIVE_LOOP_ERRORS` 번이면 멈추고(`CRASH`) 보유를 경고합니다.
- **상태 먼저, 기록은 그다음**: 청산이 확인되면 기록 실패와 상관없이 메모리 상태를 FLAT 으로 되돌립니다.
  손익을 모르는 청산(`ALREADY_FLAT`)은 손절가로 끝났다고 보고 일일 손실 한도에 넣습니다(보수적).
"""
from __future__ import annotations

import traceback
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

import trader  # noqa: F401  (week02 경로 등록)
from bot.clock import floor_to  # week02
from bot.journal import Journal  # week02
from bot.main import STOP_REASONS as _W2_REASONS, Bot as _W2Bot, StopSignal  # week02 — 종료 신호 처리 재사용
from trader import settings
from trader.strategy import FORCE, LONG, REVERSE, SHORT, STOP, TAKE, Action, Session, Strategy, Trade, levels

STOP_REASONS = {**_W2_REASONS, "DEADLINE": "장 마감 정상 종료", "NOT_TODAY": "오늘 장이 이미 끝났습니다"}


class TraderBot:
    variant = "A"

    def __init__(self, *, symbol: str, market: Any, broker: Any, journal: Journal, clock: Any,
                 session: Session, environment: str, extra_start: dict[str, Any] | None = None) -> None:
        self.symbol = symbol
        self.market = market
        self.broker = broker
        self.journal = journal
        self.clock = clock
        self.session = session
        self.environment = environment
        self.extra_start = extra_start or {}
        self.strategy = Strategy(self.variant, session)
        self.journal.state = lambda: self.strategy.state
        self.trade: Trade | None = None
        self.exit_reason: str | None = None
        self.exit_attempts = 0
        self._pending_restore: Any = None
        self._orphan = False
        self._exit_pending_waits = 0
        self._last_atr: float | None = None

    # ── 실행 ────────────────────────────────────────────────

    def run(self) -> str:
        self.journal.write("RUN_START", strategy=self.variant, environment=self.environment,
                           dry_run=self.broker.dry_run, session=self.session.to_dict(),
                           settings=settings.snapshot(), **self.extra_start)
        stop_reason, stop_detail = "DEADLINE", None
        previous = _W2Bot._catch_stop_signals()
        try:
            if self.clock.now() >= self.session.close:
                stop_reason, stop_detail = "NOT_TODAY", f"장 마감 {self.session.close:%m-%d %H:%M} 이 지났습니다"
                return stop_reason
            self.recover()
            self._wait_open()
            next_fetch = self.clock.now()
            errors = 0
            while self.clock.now() < self.session.close:
                try:
                    next_fetch = self._step(next_fetch)
                    errors = 0
                except Exception as exc:  # noqa: BLE001  — 한 바퀴의 예외로 보유 감시를 멈추지 않습니다
                    errors += 1
                    self._safe_write("ERROR", api="bot", code="LOOP_ERROR", message=f"{type(exc).__name__}: {exc}",
                                     consecutive=errors, traceback=traceback.format_exc(limit=6),
                                     trade=asdict(self.trade) if self.trade else None)
                    if errors >= settings.MAX_CONSECUTIVE_LOOP_ERRORS:
                        raise
                    next_fetch = max(next_fetch, self.clock.now())
                    self.clock.sleep(settings.PRICE_POLL_SEC)
        except KeyboardInterrupt:
            stop_reason, stop_detail = "CTRL_C", "사용자가 Ctrl+C 를 눌렀습니다"
        except StopSignal as exc:
            stop_reason, stop_detail = exc.name, f"{exc.name} 신호를 받았습니다"
        except Exception as exc:
            stop_reason, stop_detail = "CRASH", f"{type(exc).__name__}: {exc}"
            self._safe_write("ERROR", api="bot", code="CRASH", message=stop_detail,
                             traceback=traceback.format_exc(limit=6))
            raise
        finally:
            _W2Bot._restore_stop_signals(previous)
            held = self._safe_position()
            if stop_reason not in ("DEADLINE", "NOT_TODAY"):
                msg = f"보유 중: {held.side} {held.qty}계약 — 자동 청산하지 않습니다. 직접 확인하세요!" if held else "보유 포지션 없음"
                _say(f"\n[중단] {STOP_REASONS.get(stop_reason, stop_reason)} ({stop_detail}). {msg}")
            if stop_reason == "DEADLINE" and self.trade is not None:
                self._safe_write("ERROR", api="bot", code="POSITION_LEFT",
                                 message="장 마감까지 청산을 확인하지 못했습니다", trade=asdict(self.trade))
            self._safe_write("RUN_END", strategy=self.variant, environment=self.environment,
                             dry_run=self.broker.dry_run, stop_reason=stop_reason,
                             stop_reason_text=STOP_REASONS.get(stop_reason, stop_reason), stop_detail=stop_detail,
                             interrupted=stop_reason not in ("DEADLINE", "NOT_TODAY"),
                             entries=self.strategy.entries, position=asdict(held) if held else None)
        return stop_reason

    def _step(self, next_fetch: datetime) -> datetime:
        """루프 한 바퀴: 봉 갱신 → 보유 감시 → 대기. 다음 봉 조회 시각을 돌려줍니다."""
        now = self.clock.now()
        if now >= next_fetch:
            # 다음 조회 시각을 먼저 정해 둡니다. 봉 처리에서 예외가 나도 같은 봉을 매 바퀴 다시 조회하지 않게.
            next_fetch = self._next_fetch(now)
            self.on_bar_tick(now)
        if self.trade is not None:
            self.watch_exit(self.clock.now())
        now = self.clock.now()
        if self.trade is not None:
            wait = settings.PRICE_POLL_SEC
        else:
            wait = min(max((next_fetch - now).total_seconds(), 0.5), (self.session.close - now).total_seconds())
        self.clock.sleep(max(wait, 0.0))
        return next_fetch

    def _wait_open(self) -> None:
        now = self.clock.now()
        if now >= self.session.open:
            return
        self.journal.write("WAIT_OPEN", open=self.session.open.isoformat(),
                           wait_min=round((self.session.open - now).total_seconds() / 60, 1))
        while (now := self.clock.now()) < self.session.open:
            self.clock.sleep(min(30.0, (self.session.open - now).total_seconds()))

    def _next_fetch(self, now: datetime) -> datetime:
        candidate = floor_to(now, settings.BAR_MINUTES) + timedelta(seconds=settings.BAR_FETCH_DELAY_SEC)
        if candidate <= now:
            candidate += timedelta(minutes=settings.BAR_MINUTES)
        return candidate

    # ── 복구 ────────────────────────────────────────────────

    def recover(self) -> None:
        """재시작: 오늘 진입 횟수와 손절·익절가는 journal 에서, 보유는 잔고에서 되살립니다."""
        earlier = self.journal.today()
        self.strategy.entries = count_entries(earlier)
        self.strategy.realized_pnl = sum(pnl_for_limit(r) for r in earlier if r.get("type") == "TRADE_CLOSED")
        held = self._safe_position()
        try:
            pending = self.broker.pending_count()
        except Exception as exc:  # noqa: BLE001
            self.journal.error("overseas_order-history", exc, note="시작 시 미체결 확인")
            pending = None
        if held is not None:
            self.strategy.restore_position(held.side)
            opened = [r for r in earlier if r.get("type") == "ENTRY_OPEN"]
            last = opened[-1].get("trade") if opened else None
            if isinstance(last, dict) and last.get("side") == held.side:
                self.trade = Trade(**{**last, "restored": True})
            else:
                # 손절·익절가를 모릅니다. 첫 봉 조회 때 그 시점 ATR 과 잔고 평균가로 다시 세웁니다.
                self._pending_restore = held
        self.journal.write("RECOVER", entries_today=self.strategy.entries, realized_pnl=self.strategy.realized_pnl,
                           position=asdict(held) if held else None, pending_orders=pending,
                           trade=asdict(self.trade) if self.trade else None)

    # ── 봉 ──────────────────────────────────────────────────

    def on_bar_tick(self, now: datetime) -> None:
        try:
            bars = self.market.get_bars(self.session, now)
        except Exception as exc:  # noqa: BLE001
            self.journal.error("overseas_chart-minute", exc)
            return
        decision = self.strategy.on_bars(bars, now)
        for event in decision.events:
            self.journal.write(event.pop("type"), **event)
            if "atr" in event and event.get("bar"):
                self._last_atr = event["atr"]
        self._reconcile()
        if self._pending_restore is not None and self._last_atr:
            self._restore_from(self._pending_restore)
        if decision.action is Action.EXIT and self.trade is not None:
            self.exit_reason = REVERSE
            self.exit_attempts = 0
            self.exit(REVERSE, self._safe_price())
        elif decision.action in (Action.ENTER_LONG, Action.ENTER_SHORT) and decision.row is not None:
            self.enter(LONG if decision.action is Action.ENTER_LONG else SHORT, decision.row)

    def _reconcile(self) -> None:
        """관리 중인 거래가 없는데 잔고에 포지션이 있으면 거둬서 관리합니다(체결 확인 실패·늦은 체결)."""
        if self.trade is not None or self._pending_restore is not None:
            return
        try:
            held = self.broker.position()
        except Exception as exc:  # noqa: BLE001
            self.journal.error("overseas_open-interest", exc, note="잔고 맞춤")
            return
        if held is None:
            return
        self.journal.write("ERROR", api="bot", code="ORPHAN_POSITION", position=asdict(held),
                           message="관리 중인 거래 없이 잔고에 포지션이 있습니다 — 진입 1회로 세고 손절·익절가를 세워 관리")
        self.strategy.on_entry_filled(held.side)
        self._orphan = True
        self._pending_restore = held

    def _restore_from(self, held: Any) -> None:
        price = held.avg_price or self._safe_price()
        lv = levels(held.side, price, self._last_atr) if price and self._last_atr else None
        if lv is None:
            return
        orphan, self._orphan = self._orphan, False
        self.trade = Trade.from_levels(lv, held.qty, None, self.clock.now().isoformat(), restored=True)
        self._pending_restore = None
        self.journal.write("ENTRY_OPEN", trade=asdict(self.trade), position=asdict(held), orphan=orphan,
                           entry_counted=orphan,
                           note=("잔고 맞춤" if orphan else "재시작") + ": 잔고 평균가와 현재 ATR 로 손절·익절가를 다시 세움")

    # ── 진입 ────────────────────────────────────────────────

    def enter(self, side: str, row: Any) -> None:
        if self.clock.now() >= self.session.entry_cutoff:
            self.journal.write("SKIP", side=side, reason="PAST_ENTRY_CUTOFF")
            return
        try:
            price = self.market.get_price()
            held = self.broker.position()
            pending = self.broker.pending_count()
            orderable = self.broker.orderable_qty(side, price)
        except Exception as exc:  # noqa: BLE001
            self.journal.error("risk_inputs", exc, side=side)
            return
        failures = []
        if held is not None:
            failures.append({"check": "NO_POSITION", "value": asdict(held)})
        if pending:
            failures.append({"check": "NO_PENDING", "value": pending})
        if orderable is None or orderable < settings.MAX_QTY:
            failures.append({"check": "ORDERABLE", "value": orderable, "limit": settings.MAX_QTY})
        if self.strategy.entries >= settings.MAX_ENTRIES_PER_DAY:
            failures.append({"check": "ENTRIES", "value": self.strategy.entries})
        if self.strategy.loss_limit_hit:
            failures.append({"check": "DAILY_LOSS_LIMIT", "value": round(self.strategy.realized_pnl, 2),
                             "limit": -settings.DAILY_LOSS_LIMIT_USD})
        if failures:
            for failure in failures:
                self.journal.write("RISK_BLOCK", side=side, price=price, **failure)
            return

        signal_bar = row.bar.start.isoformat()
        order_no = self.broker.submit(side, settings.MAX_QTY, role="ENTRY")
        fill = self.broker.wait_fill(order_no, side=side, qty=settings.MAX_QTY, ref_price=price,
                                     role="ENTRY") if order_no else None
        if fill is None:
            held = self._safe_position()
            if held is None:
                return
            self.journal.write("ERROR", api="bot", code="FILL_UNCONFIRMED_BUT_HELD",
                               message="체결 확인 실패, 잔고에는 포지션 있음 — 잔고 평균가로 관리", position=asdict(held))
            fill_price = held.avg_price or price
            qty = held.qty
        else:
            fill_price, qty = fill.price, fill.qty
        lv = levels(side, fill_price, row.atr)
        if lv is None:  # 신호 때 확인했으므로 오지 않는 자리지만, 보유를 두고 손을 놓지 않습니다.
            self.journal.write("ERROR", api="bot", code="LEVELS_UNAVAILABLE", message="손절·익절가를 세우지 못해 바로 청산")
            self.trade = Trade(side, qty, fill_price, fill_price, fill_price, 0, 0, row.atr or 0.0, order_no,
                               self.clock.now().isoformat(), signal_bar)
            self.strategy.on_entry_filled(side)
            self.journal.write("ENTRY_OPEN", trade=asdict(self.trade), signal_bar=signal_bar, entry_counted=True,
                               note="손절·익절가 없음 → 바로 청산")
            self.exit_reason = FORCE
            self.exit_attempts = 0
            self.exit(FORCE, fill_price)
            return
        self.trade = Trade.from_levels(lv, qty, order_no, self.clock.now().isoformat(), signal_bar=signal_bar)
        self.strategy.on_entry_filled(side)
        self.journal.write("ENTRY_OPEN", trade=asdict(self.trade), signal_bar=signal_bar, entry_counted=True,
                           fill_source="assumed" if self.broker.dry_run else "actual",
                           # 주문 직전 현재가 대비 체결가. 음수면 불리하게 체결.
                           entry_slippage_ticks=round((price - fill_price) * (1 if side == LONG else -1) / settings.TICK, 2))

    # ── 청산 ────────────────────────────────────────────────

    def watch_exit(self, now: datetime) -> None:
        trade = self.trade
        if trade is None:
            return
        if now >= self.session.force_close and self.exit_reason != FORCE:
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
        self.journal.write("EXIT_TRIGGER", reason=reason, trigger_price=trigger_price, stop=trade.stop, take=trade.take,
                           entry_price=trade.entry_price, attempt=self.exit_attempts,
                           price_source=getattr(self.market, "last_price_source", None))
        try:
            held = self.broker.position()
        except Exception as exc:  # noqa: BLE001
            self.journal.error("overseas_open-interest", exc, note="청산 전 잔고 확인")
            return
        if held is None:
            self.journal.write("ERROR", api="overseas_open-interest", code="ALREADY_FLAT",
                               message="청산하려 했으나 잔고가 없습니다", trade=asdict(trade))
            self._closed(trade, reason, None, None)
            return
        if held.side != trade.side or held.qty != trade.qty:
            self.journal.write("ERROR", api="bot", code="POSITION_MISMATCH", position=asdict(held), trade=asdict(trade),
                               message="잔고와 관리 중인 거래가 다릅니다 — 잔고 기준으로 청산")
        try:
            pending = self.broker.pending_count()
        except Exception as exc:  # noqa: BLE001
            self.journal.error("overseas_order-history", exc, note="청산 전 미체결 확인")
            return
        if pending:
            # 앞서 낸 주문(응답 없음·취소 실패)이 남아 있습니다. 새로 내면 두 번 체결될 수 있어 잠시 기다리고,
            # 그래도 남아 있으면 취소한 뒤 다음 시도에서 새로 냅니다(손으로 넣은 주문 때문에 청산이 막히지 않게).
            self._exit_pending_waits += 1
            if self._exit_pending_waits <= settings.EXIT_PENDING_WAIT_ATTEMPTS:
                self.journal.write("ERROR", api="bot", code="EXIT_WAIT_PENDING", pending=pending,
                                   wait=self._exit_pending_waits, attempt=self.exit_attempts,
                                   message="미체결 주문이 남아 있어 새 청산 주문을 보내지 않고 기다립니다")
            else:
                cancelled = self.broker.cancel_pending()
                self.journal.write("ERROR", api="bot", code="EXIT_CANCEL_PENDING", pending=pending, cancelled=cancelled,
                                   message="미체결이 계속 남아 취소 요청 — 다음 시도에서 잔고 확인 후 청산")
                self._exit_pending_waits = 0
            self._warn_if_stuck(trade)
            return
        self._exit_pending_waits = 0
        side = LONG if held.side == SHORT else SHORT
        order_no = self.broker.submit(side, held.qty, role="EXIT")
        fill = self.broker.wait_fill(order_no, side=side, qty=held.qty, ref_price=trigger_price,
                                     role="EXIT") if order_no else None
        if fill is None:
            self._warn_if_stuck(trade)
            return  # 다음 감시 때 다시 시도합니다(그때 잔고·미체결부터 다시 봅니다)
        after = self._safe_position()
        if after is not None:
            self.journal.write("ERROR", api="overseas_open-interest", code="NOT_FLAT",
                               message="청산 체결 후에도 잔고가 남았습니다", position=asdict(after))
            return
        self._closed(trade, reason, fill.price, order_no)

    def _closed(self, trade: Trade, reason: str, exit_price: float | None, order_no: str | None) -> None:
        record: dict[str, Any] = {
            "side": trade.side, "qty": trade.qty, "reason": reason,
            "entry_price": trade.entry_price, "exit_price": exit_price, "stop": trade.stop, "take": trade.take,
            "stop_ticks": trade.stop_ticks, "take_ticks": trade.take_ticks, "atr": trade.atr,
            "entry_order": trade.entry_order, "exit_order": order_no, "entry_ts": trade.entry_ts,
            "signal_bar": trade.signal_bar, "restored": trade.restored,
            "fill_source": "assumed" if self.broker.dry_run else "actual",
        }
        if exit_price is not None:
            record.update(ticks=trade.ticks(exit_price), pnl_usd=trade.pnl_usd(exit_price))
            planned = {STOP: trade.stop, TAKE: trade.take}.get(reason)
            if planned is not None:
                sign = 1 if trade.side == LONG else -1
                # 계획 가격 대비 실제 체결가. 음수면 불리하게 체결(슬리피지).
                record["slippage_ticks"] = round((exit_price - planned) * sign / settings.TICK, 2)
        else:
            # 청산가를 모릅니다(밖에서 정리됨 등). 손익은 비워 두고, 한도 계산에는 손절가로 끝났다고 봅니다.
            record.update(pnl_usd=None, pnl_for_limit_usd=min(trade.pnl_usd(trade.stop), 0.0),
                          note="청산가 모름 — 일일 손실 한도에는 손절가 기준 손실로 반영")
        try:
            self.journal.write("TRADE_CLOSED", **record)
        finally:
            # 기록이 실패해도 상태는 FLAT 으로 되돌립니다. 그래야 같은 청산을 다시 시도하지 않습니다.
            self.trade = None
            self.exit_reason = None
            self.exit_attempts = 0
            self._exit_pending_waits = 0
            self.strategy.on_closed(pnl_for_limit(record))

    def _warn_if_stuck(self, trade: Trade) -> None:
        n = self.exit_attempts
        if n == settings.EXIT_STUCK_ATTEMPTS or (n > settings.EXIT_STUCK_ATTEMPTS and n % 30 == 0):
            self.journal.write("ERROR", api="bot", code="EXIT_STUCK", attempt=n, trade=asdict(trade),
                               message=f"청산을 {n}번 시도했지만 확인하지 못했습니다 — HTS/앱에서 잔고·미체결을 직접 확인하세요")
            _say(f"\n[경고] 청산 {n}번째 시도 실패 — HTS/앱에서 잔고·미체결을 확인하세요")

    # ── 보조 ────────────────────────────────────────────────

    def _safe_price(self) -> float | None:
        try:
            return self.market.get_price()
        except Exception as exc:  # noqa: BLE001
            self.journal.error("overseas_price", exc)
            return None

    def _safe_position(self) -> Any:
        try:
            return self.broker.position()
        except Exception as exc:  # noqa: BLE001
            self.journal.error("overseas_open-interest", exc)
            return None

    def _safe_write(self, type_: str, **fields: Any) -> None:
        try:
            self.journal.write(type_, **fields)
        except Exception as exc:  # noqa: BLE001  — 기록 실패는 화면에라도 남깁니다
            _say(f"[기록 실패] {type_}: {type(exc).__name__}: {exc}")


def pnl_for_limit(record: dict[str, Any]) -> float:
    """일일 손실 한도에 넣을 손익. 실제 손익이 없으면(청산가 모름) 손절가 기준 손실."""
    pnl = record.get("pnl_usd")
    if pnl is None:
        pnl = record.get("pnl_for_limit_usd")
    return float(pnl or 0.0)


def count_entries(records: list[dict[str, Any]]) -> int:
    """오늘 진입 횟수. `entry_counted` 표시가 있는 ENTRY_OPEN 을 셉니다(부분 체결·잔고 맞춤 포함).
    표시가 없는 예전 기록이면 ENTRY FILL 의 주문번호 수로 셉니다."""
    marked = [r for r in records if r.get("type") == "ENTRY_OPEN" and r.get("entry_counted")]
    if marked or any("entry_counted" in r for r in records if r.get("type") == "ENTRY_OPEN"):
        return len(marked)
    return len({r.get("order_no") for r in records if r.get("type") == "FILL" and r.get("role") == "ENTRY"})


def _say(text: str) -> None:
    try:
        print(text, flush=True)
    except OSError:
        pass
