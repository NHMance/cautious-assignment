"""전략 B — A 의 journal 을 읽어 ATR 필터만 다르게 다시 계산합니다. **주문 API 를 부르지 않습니다.**

    A 의 BAR 기록(봉 + 지표)  ──→  같은 Strategy 코드(variant="B")  ──→  B journal

같은 봉, 같은 코드라서 A 와 B 의 차이는 **ATR 필터가 거른 신호와 그 뒤로 달라진 보유 상태**뿐입니다.

체결가
- **A 와 같은 거래**(같은 신호 봉·같은 방향으로 A 가 실제 진입): A 의 실제 체결가·청산을 그대로 씁니다.
  진입 뒤 규칙이 A·B 같으므로 청산도 같습니다. `fill_source="A"`.
- **B 에만 있는 거래**: 체결가를 가정합니다(`fill_source="assumed"`, 슬리피지 0).
  - 진입: 신호 봉 종가
  - 손절·익절: 다음 봉부터 5분봉 고가·저가로 판정. **같은 봉에서 둘 다 닿으면 손절**로 봅니다(보수적).
    봉이 손절가 너머에서 시작하면(갭) 그 봉 시가로 손절합니다. 익절은 갭이어도 익절가로 적습니다.
  - 반대 교차: 그 봉 종가로 청산
  - 강제 청산: 마감 30분 전 시각에 끝나는 봉의 종가

A 가 같은 묶음(같은 시각)으로 남긴 BAR 들을 한 번에 넣어, 행동은 그 묶음의 마지막 봉에서만 냅니다 — A 와 같은 방식입니다.

    실시간: A 의 오늘 journal 을 따라 읽습니다(A 를 껐다 켜도, B 를 껐다 켜도 처음부터 다시 읽어 이어집니다).
    리플레이·봇 테스트: A 의 그날 journal 을 끝까지 읽고 끝납니다.
"""
from __future__ import annotations

import json
import time as _time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import trader  # noqa: F401  (week02 경로 등록)
from bot.clock import KST  # week02
from trader.journal import DayJournal
from trader import settings
from trader.strategy import (FORCE, LONG, REVERSE, SHORT, STOP, TAKE, Action, Bar, Session, Strategy, Trade, levels)


class RecordClock:
    """B 기록의 시각 = 지금 처리 중인 A 기록의 시각."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, moment: datetime) -> None:
        self._now = moment

    def sleep(self, seconds: float) -> None:  # Journal·Strategy 와 같은 모양을 맞추려고만 둡니다
        pass


def _bar(d: dict[str, Any]) -> Bar:
    return Bar(datetime.fromisoformat(d["start"]), datetime.fromisoformat(d["end"]),
               d["open"], d["high"], d["low"], d["close"], d.get("volume", 0))


class Recompute:
    variant = "B"

    def __init__(self, journal: DayJournal, clock: RecordClock, *, source: Path) -> None:
        self.journal = journal
        self.clock = clock
        self.source = source
        self.strategy: Strategy | None = None
        self.session: Session | None = None
        self.bars: dict[datetime, Bar] = {}
        self.batch_ts: str | None = None
        self.batch: list[Bar] = []
        self.trade: Trade | None = None
        self.trade_source: str | None = None       # "A" | "assumed"
        self.pending: dict[str, Any] | None = None  # A 의 진입 결과를 기다리는 B 의 진입 신호
        self.a_open: dict[str, Any] | None = None   # 지금 열린 A 거래(ENTRY_OPEN.trade)
        self.started = False
        self.finished = False
        self.closed: list[dict[str, Any]] = []
        self.journal.state = lambda: self.strategy.state if self.strategy else "WAIT_A"

    # ── 입력 ────────────────────────────────────────────────

    def feed(self, rec: dict[str, Any]) -> None:
        ts = rec.get("ts")
        kind = rec.get("type")
        if kind != "BAR" or ts != self.batch_ts:
            self._flush()
        if ts:
            self.clock.set(datetime.fromisoformat(ts))
        if kind == "RUN_START":
            self._on_start(rec)
        elif kind == "BAR":
            if self.batch_ts != ts:
                self.batch_ts, self.batch = ts, []
            self.batch.append(_bar(rec["bar"]))
        elif kind == "ENTRY_OPEN":
            self._on_a_entry(rec)
        elif kind == "TRADE_CLOSED":
            self._on_a_closed(rec)
        elif kind == "RUN_END":
            self._on_end(rec)

    def _on_start(self, rec: dict[str, Any]) -> None:
        if self.started:
            self.journal.write("SOURCE_RESTART", note="A 가 다시 시작했습니다. 이어서 계산합니다.")
            return
        s = rec.get("session") or {}
        self.session = Session.from_dict(s)
        self.strategy = Strategy(self.variant, self.session)
        self.started = True
        self.journal.write("RUN_START", strategy=self.variant, environment=rec.get("environment"),
                           source=str(self.source), source_dry_run=rec.get("dry_run"),
                           session=self.session.to_dict(), settings=settings.snapshot(),
                           note="A 의 봉·지표로 다시 계산 (주문 없음)")

    # ── 봉 묶음 ─────────────────────────────────────────────

    def _flush(self) -> None:
        if not self.batch or self.strategy is None:
            self.batch, self.batch_ts = [], None
            return
        batch, ts = self.batch, self.batch_ts
        self.batch, self.batch_ts = [], None
        new = [b for b in batch if b.start not in self.bars]
        for b in batch:
            self.bars[b.start] = b
        now = datetime.fromisoformat(ts) if ts else self.clock.now()
        self._resolve_pending(now)
        # B 에만 있는 거래는 새 봉마다 손절·익절·강제 청산을 판정합니다(신호보다 먼저).
        for b in sorted(new, key=lambda x: x.start):
            if self.trade is not None and self.trade_source == "assumed":
                self._check_assumed_exit(b)
        all_bars = sorted(self.bars.values(), key=lambda b: b.start)
        decision = self.strategy.on_bars(all_bars, now)
        for event in decision.events:
            if event["type"] == "BAR":
                continue  # 봉·지표는 A 기록에 이미 있습니다
            self.journal.write(event.pop("type"), **event)
        if decision.action is Action.EXIT and self.trade is not None:
            if self.trade_source == "assumed":
                assert decision.row is not None
                self._close(REVERSE, decision.row.bar.close, decision.row.bar.end)
            # A 와 같은 거래면 A 의 청산 기록을 기다립니다(같은 봉에서 A 도 반대 교차로 청산합니다).
        elif decision.action in (Action.ENTER_LONG, Action.ENTER_SHORT) and decision.row is not None:
            side = LONG if decision.action is Action.ENTER_LONG else SHORT
            self.pending = {"side": side, "row": decision.row, "signal_bar": decision.row.bar.start.isoformat()}

    # ── 진입 ────────────────────────────────────────────────

    def _on_a_entry(self, rec: dict[str, Any]) -> None:
        trade = rec.get("trade") or {}
        self.a_open = trade
        p = self.pending
        if p and trade.get("side") == p["side"] and (rec.get("signal_bar") or trade.get("signal_bar")) == p["signal_bar"]:
            self.pending = None
            self.trade = Trade(**{**trade, "restored": False})
            self.trade_source = "A"
            self.strategy.on_entry_filled(p["side"])  # type: ignore[union-attr]
            self.journal.write("ENTRY_OPEN", trade=asdict(self.trade), signal_bar=p["signal_bar"],
                               fill_source="A", note="A 와 같은 신호 → A 실제 체결 공유")

    def _resolve_pending(self, now: datetime) -> None:
        """A 가 다음 봉 전까지 같은 신호로 들어가지 않았으면 B 혼자 들어간 것으로 봅니다(가정 체결)."""
        p = self.pending
        if p is None:
            return
        self.pending = None
        row = p["row"]
        lv = levels(p["side"], row.bar.close, row.atr)
        if lv is None:
            self.journal.write("SKIP", side=p["side"], reason="stop_below_1tick", bar_start=p["signal_bar"])
            return
        self.trade = Trade.from_levels(lv, settings.MAX_QTY, None, row.bar.end.isoformat(), signal_bar=p["signal_bar"])
        self.trade_source = "assumed"
        self.strategy.on_entry_filled(p["side"])  # type: ignore[union-attr]
        self.journal.write("ENTRY_OPEN", trade=asdict(self.trade), signal_bar=p["signal_bar"], fill_source="assumed",
                           note="A 는 이 신호로 진입하지 않음 → 신호 봉 종가로 가정 체결 (슬리피지 0)")

    # ── 청산 ────────────────────────────────────────────────

    def _check_assumed_exit(self, b: Bar) -> None:
        t = self.trade
        assert t is not None and self.session is not None
        if b.start < datetime.fromisoformat(t.entry_ts):
            return
        long = t.side == LONG
        gap_stop = b.open <= t.stop if long else b.open >= t.stop
        hit_stop = b.low <= t.stop if long else b.high >= t.stop
        hit_take = b.high >= t.take if long else b.low <= t.take
        if gap_stop:
            self._close(STOP, b.open, b.start, note="갭: 봉 시가가 손절가 너머")
        elif hit_stop:
            self._close(STOP, t.stop, b.end, note="봉 고가·저가 판정" + (" (익절도 닿았지만 손절로 봄)" if hit_take else ""))
        elif hit_take:
            self._close(TAKE, t.take, b.end, note="봉 고가·저가 판정")
        elif b.end >= self.session.force_close:
            self._close(FORCE, b.close, b.end, note="마감 30분 전 강제 청산")

    def _on_a_closed(self, rec: dict[str, Any]) -> None:
        self.a_open = None
        if self.trade is None or self.trade_source != "A":
            return
        if rec.get("entry_order") != self.trade.entry_order or rec.get("exit_price") is None:
            return
        self._close(rec.get("reason", "?"), rec["exit_price"], datetime.fromisoformat(rec["ts"]),
                    slippage_ticks=rec.get("slippage_ticks"), note="A 청산 공유")

    def _close(self, reason: str, price: float, when: datetime, *, note: str = "",
               slippage_ticks: float | None = None) -> None:
        t = self.trade
        assert t is not None
        record = {"side": t.side, "qty": t.qty, "reason": reason, "entry_price": t.entry_price, "exit_price": price,
                  "stop": t.stop, "take": t.take, "stop_ticks": t.stop_ticks, "take_ticks": t.take_ticks, "atr": t.atr,
                  "entry_order": t.entry_order, "entry_ts": t.entry_ts, "exit_ts": when.isoformat(),
                  "signal_bar": t.signal_bar, "fill_source": self.trade_source,
                  "ticks": t.ticks(price), "pnl_usd": t.pnl_usd(price), "note": note}
        if slippage_ticks is not None:
            record["slippage_ticks"] = slippage_ticks
        self.closed.append(self.journal.write("TRADE_CLOSED", **record))
        self.trade = None
        self.trade_source = None
        self.strategy.on_closed(record["pnl_usd"])  # type: ignore[union-attr]

    def _on_end(self, rec: dict[str, Any]) -> None:
        self._resolve_pending(self.clock.now())
        if self.trade is not None:
            if self.trade_source == "assumed" and self.bars:
                last = max(self.bars.values(), key=lambda b: b.start)
                self._close(FORCE, last.close, last.end, note="A 종료 시점까지 열려 있어 마지막 봉 종가로 정리")
            else:
                self.journal.write("ERROR", api="recompute", code="SHARED_TRADE_OPEN",
                                   message="A 와 공유한 거래가 A 종료 때 열려 있습니다(A 기록 확인)", trade=asdict(self.trade))
        pnl = round(sum(r.get("pnl_usd") or 0 for r in self.closed), 2)
        self.journal.write("RUN_END", strategy=self.variant, source_stop_reason=rec.get("stop_reason"),
                           entries=self.strategy.entries if self.strategy else 0, trades=len(self.closed),
                           shared=sum(1 for r in self.closed if r.get("fill_source") == "A"),
                           assumed=sum(1 for r in self.closed if r.get("fill_source") == "assumed"),
                           pnl_usd=pnl)
        self.finished = True


# ── 읽기 ────────────────────────────────────────────────────

def read_records(path: Path, *, follow: bool, poll_sec: float = 1.0,
                 stop: Any = None) -> Iterator[dict[str, Any]]:
    """A journal 을 한 줄씩. follow 면 파일이 생기고 자라는 것을 기다립니다."""
    while follow and not path.exists():
        if stop and stop():
            return
        _time.sleep(poll_sec)
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open(encoding="utf-8") as fh:
        buffer = ""
        while True:
            line = fh.readline()
            if line:
                buffer += line
                if not buffer.endswith("\n"):
                    continue  # 쓰는 도중인 줄
                text, buffer = buffer.strip(), ""
                if text:
                    try:
                        yield json.loads(text)
                    except json.JSONDecodeError:
                        continue
                continue
            if not follow or (stop and stop()):
                return
            _time.sleep(poll_sec)


def run(source: Path, out_root: Path, *, follow: bool, fresh: bool = True) -> Recompute:
    """A journal(source) 을 읽어 B journal 을 out_root 에 씁니다."""
    day = datetime.strptime(source.stem.removeprefix("journal_"), "%Y%m%d").replace(tzinfo=KST)
    clock = RecordClock(day)
    journal = DayJournal(settings.SYMBOL, day.date(), clock=clock, root=out_root)
    if fresh:
        journal.path().unlink(missing_ok=True)
    engine = Recompute(journal, clock, source=source)
    for rec in read_records(source, follow=follow, stop=lambda: engine.finished):
        engine.feed(rec)
        if engine.finished:
            break
    engine._flush()
    return engine


def summary(engine: Recompute) -> str:
    rows = engine.closed
    pnl = sum(r.get("pnl_usd") or 0 for r in rows)
    return (f"B 거래 {len(rows)}건 (A 공유 {sum(r['fill_source'] == 'A' for r in rows)}, "
            f"가정 체결 {sum(r['fill_source'] == 'assumed' for r in rows)}) · 손익 {pnl:+.2f} USD")

