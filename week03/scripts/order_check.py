"""모의계좌 주문 왕복 점검 — 1계약 시장가 매수 → 바로 시장가 매도. **모의계좌에 실제로 주문합니다.**

    uv run python scripts/order_check.py                  # MKUX26
    uv run python scripts/order_check.py --symbol MKUZ26

확인하는 것 (봇과 같은 `trader.broker.OverseasBroker` 경로)
1. 주문 응답 모양(`ord_no`·`err_cd`), 주문내역의 체결가·체결수량·상태 칸, 잔고 조회
2. 웹소켓 주문/체결 `O1` 메시지 모양 — 원문을 `results/order-check/ws_*.jsonl` 에 남깁니다(계좌번호 등은 지움)
3. 매수 → 매도 뒤 잔고가 0 으로 돌아오는지

안전장치
- 시작 전: 보유 없음 · 미체결 없음 · 주문가능수량 ≥ 1 이 아니면 주문하지 않고 끝냅니다.
- 매수 뒤에는 잔고가 0 이 될 때까지 매도를 다시 시도합니다(미체결이 남아 있으면 새로 내지 않고 취소부터).
  끝내 정리하지 못하면 화면에 크게 경고하고 실패 코드로 끝납니다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import trader  # noqa: E402,F401
from backend.config import ConfigError  # noqa: E402
from backend.nh_api import runtime  # noqa: E402
from backend.nh_api.session import create_session  # noqa: E402
from bot.clock import KST, RealClock  # noqa: E402
from bot.journal import scrub  # noqa: E402
from trader import credentials, settings  # noqa: E402
from trader.broker import OverseasBroker  # noqa: E402
from trader.journal import DayJournal, scrub_extra  # noqa: E402
from trader.market import OverseasMarket  # noqa: E402
from trader.realtime import FA, O1, NHStream, RealtimeFeed, Subscription, session_credentials  # noqa: E402
from trader.strategy import LONG, SHORT  # noqa: E402

OUT = ROOT / "results" / "order-check"
CLOSE_TRIES = 5


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=settings.SYMBOL)
    args = ap.parse_args()

    try:
        config, source = credentials.load()
    except ConfigError as exc:
        print(exc)
        return 2
    session = create_session(config=config)
    runtime.use(session)
    clock = RealClock()
    now = clock.now()
    journal = DayJournal(args.symbol, now.date(), clock=clock, root=OUT)
    broker = OverseasBroker(args.symbol, journal, clock, dry_run=False)
    market = OverseasMarket(args.symbol)
    feed = None
    print(f"인증 정보: {source} · 기록 {journal.path()}")

    try:
        # ── 사전 점검 ─────────────────────────────────────
        price = market.get_price()
        real_sym = market.real_symbol()
        held = broker.position()
        pending = broker.pending_count()
        orderable = broker.orderable_qty(LONG, price)
        journal.write("RUN_START", strategy="ORDER_CHECK", environment="paper", price=price, real_sym=real_sym,
                      position=asdict(held) if held else None, pending=pending, orderable=orderable)
        print(f"현재가 {price} · 보유 {held} · 미체결 {pending} · 매수 가능 {orderable}")
        if held is not None or pending or not orderable or orderable < 1:
            print("사전 점검 실패 — 주문하지 않습니다.")
            journal.write("RUN_END", stop_reason="PRECHECK_FAILED")
            return 1

        # ── 웹소켓 FA + O1 원문 기록 ──────────────────────
        ws_path = OUT / f"ws_{args.symbol}_{now:%Y%m%d_%H%M%S}.jsonl"
        counts: dict[str, int] = {}

        def on_data(code: str, msg: dict) -> None:
            counts[code or "?"] = counts.get(code or "?", 0) + 1
            if code == FA and counts[FA] > 20:
                return
            with ws_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"received": datetime.now(tz=KST).isoformat(), "code": code, "msg": scrub_extra(scrub(msg))},
                                    ensure_ascii=False) + "\n")
            if code != FA:
                print(f"  [WS {code or '?'}] {json.dumps(scrub_extra(scrub(msg)), ensure_ascii=False)[:400]}")

        def on_status(kind: str, detail: dict) -> None:
            if kind in ("init", "fatal", "error", "RT_FAIL", "subscribe_fail", "disconnected"):
                print(f"  [WS 상태] {kind} {json.dumps(scrub(detail), ensure_ascii=False)[:200]}")

        subs = [Subscription(FA, real_sym), Subscription(O1)]
        feed = RealtimeFeed(
            lambda data, status: NHStream(credentials.ws_url(), session_credentials(session), subs, data, status),
            listeners=[on_data], on_status=on_status,
        )
        OUT.mkdir(parents=True, exist_ok=True)
        feed.start()
        if not feed.ready.wait(timeout=15):
            print("웹소켓 준비가 안 됐습니다 — O1 없이 REST 로만 확인합니다.")
        time.sleep(2)

        # ── 매수 ─────────────────────────────────────────
        t0 = time.time()
        buy_no = broker.submit(LONG, 1, role="ENTRY")
        buy = broker.wait_fill(buy_no, side=LONG, qty=1, ref_price=price, role="ENTRY") if buy_no else None
        t_buy = time.time() - t0
        after_buy = broker.position()
        journal.write("CHECK", step="after_buy", order_no=buy_no, fill=asdict(buy) if buy else None,
                      position=asdict(after_buy) if after_buy else None, seconds=round(t_buy, 2))
        print(f"매수: 주문 {buy_no} · 체결 {buy} · {t_buy:.1f}초 · 잔고 {after_buy}")

        # ── 매도 (잔고가 0 이 될 때까지) ─────────────────────
        sell = None
        sell_no = None
        t1 = time.time()
        for attempt in range(1, CLOSE_TRIES + 1):
            held = broker.position()
            if held is None:
                break
            if broker.pending_count():
                print(f"  미체결이 남아 취소부터 합니다 (시도 {attempt})")
                broker.cancel_pending()
                time.sleep(2)
                continue
            side = SHORT if held.side == LONG else LONG
            sell_no = broker.submit(side, held.qty, role="EXIT")
            sell = broker.wait_fill(sell_no, side=side, qty=held.qty, ref_price=market.get_price(),
                                    role="EXIT") if sell_no else None
            time.sleep(1)
        t_sell = time.time() - t1
        final = broker.position()
        journal.write("CHECK", step="after_sell", order_no=sell_no, fill=asdict(sell) if sell else None,
                      position=asdict(final) if final else None, seconds=round(t_sell, 2))
        print(f"매도: 주문 {sell_no} · 체결 {sell} · {t_sell:.1f}초 · 잔고 {final}")

        # ── 주문내역 원문 (필드 확인용) ─────────────────────
        rows = [r for r in broker.history() if str(r.get("occurs1_ord_no") or "").lstrip("0") in (buy_no, sell_no)]
        journal.write("CHECK", step="history_rows", rows=rows)

        time.sleep(3)  # 늦게 오는 O1 을 받습니다
        pnl_ticks = round((sell.price - buy.price) / settings.TICK, 2) if buy and sell else None
        journal.write("RUN_END", stop_reason="DONE" if final is None else "POSITION_LEFT",
                      buy_price=buy.price if buy else None, sell_price=sell.price if sell else None,
                      pnl_ticks=pnl_ticks, pnl_usd=pnl_ticks * settings.TICK_VALUE if pnl_ticks is not None else None,
                      ws_counts=counts)
        print(f"\n결과: 매수 {buy.price if buy else '-'} → 매도 {sell.price if sell else '-'} · "
              f"{pnl_ticks}틱 · 웹소켓 메시지 {counts} · 원문 {ws_path}")
        if final is not None:
            print(f"\n!!! 잔고가 남았습니다: {final} — HTS/앱에서 직접 정리하세요 !!!")
            return 3
        return 0
    finally:
        if feed is not None:
            feed.stop()
        session.close()


if __name__ == "__main__":
    sys.exit(main())
