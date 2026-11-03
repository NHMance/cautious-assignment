"""실행 진입점. A 와 B 는 각자 터미널에서 돌립니다.

    # 실시간 (20:50 전에 켜 두면 20:50 에 돌기 시작하고, 다음 날 06:15 에 끝납니다)
    uv run python -m trader.main --strategy A               # 모의계좌 실주문 + 실시간 체결(웹소켓)
    uv run python -m trader.main --strategy B               # A 의 오늘 기록을 따라 읽으며 다시 계산 (주문 없음)
    uv run python -m trader.main --strategy A --dry-run     # 주문 없이 기록만 (B 도 --dry-run 으로 따라 읽음)

    # 리플레이 (지난 장의 봉을 받아 가상 시계로. 주문 없음). 날짜는 장이 열린 날 — 20261102 = 11/2 20:50 ~ 11/3 06:15
    uv run python -m trader.main --strategy AB --replay-date 20261102

    # 봇 테스트 (서버·인증 없이 합성 봉으로 흐름만 점검)
    uv run python -m trader.main --strategy AB --bot-test

장 시간은 settings.py 에 고정합니다(운영 20:50~06:15, 실제 거래 마감 06:15 기준으로 진입 마감·강제 청산).
`--session 08:25-20:35` 처럼 운영 시간만 바꿀 수 있습니다.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import trader  # noqa: F401  (week02 경로 등록)
from bot.clock import KST, RealClock, SimClock  # week02
from trader import settings
from trader.journal import RESULTS_DIR, DayJournal, root_for
from trader.strategy import Session


def _parse_session(text: str) -> tuple[time, time]:
    try:
        a, b = text.replace("~", "-").split("-")
        return time.fromisoformat(a.strip()), time.fromisoformat(b.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--session 은 HH:MM-HH:MM 입니다: {text!r}") from exc


def _mode(args: argparse.Namespace) -> str:
    if args.bot_test:
        return "bot-test"
    if args.replay_date:
        return "replay"
    return "dry-run" if args.dry_run else "live"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="3주차 MKU EMA 교차 + ATR 검증 봇 (A 기본형 / B 개선형)")
    ap.add_argument("--strategy", required=True, choices=["A", "B", "AB"],
                    help="A: 모의계좌 주문 / B: A 기록으로 다시 계산 / AB: 리플레이·봇 테스트에서 A 다음 B")
    ap.add_argument("--dry-run", action="store_true", help="주문 없이 기록만 (실시간)")
    ap.add_argument("--replay-date", metavar="YYYYMMDD", help="리플레이: 그날 봉으로 가상 시계 실행 (주문 없음)")
    ap.add_argument("--bot-test", action="store_true", help="서버 없이 합성 봉으로 흐름 점검")
    ap.add_argument("--session", type=_parse_session, metavar="HH:MM-HH:MM",
                    help=f"운영 시간(한국시간). 비우면 {settings.SESSION_OPEN}-{settings.SESSION_CLOSE}")
    ap.add_argument("--no-ws", action="store_true", help="실시간 체결(웹소켓) 없이 REST 현재가만")
    ap.add_argument("--symbol", default=settings.SYMBOL)
    args = ap.parse_args(argv)

    if args.replay_date and args.bot_test:
        print("--replay-date 와 --bot-test 는 함께 쓸 수 없습니다.")
        return 2
    mode = _mode(args)
    if args.strategy == "AB" and mode in ("live", "dry-run"):
        print("실시간은 A 와 B 를 각자 터미널에서 돌립니다: --strategy A / --strategy B")
        return 2
    day: date | None = None
    if args.replay_date:
        try:
            day = datetime.strptime(args.replay_date, "%Y%m%d").date()
        except ValueError:
            print(f"--replay-date 는 YYYYMMDD 형식입니다: {args.replay_date!r}")
            return 2
        problem = _replay_problem(day, datetime.now(tz=KST))
        if problem:
            print(f"리플레이할 수 없습니다: {problem}")
            return 2

    if args.strategy == "B":
        return run_b(mode, day or _today(args))
    code = run_a(args, mode, day)
    if code == 0 and args.strategy == "AB":
        print()
        code = run_b(mode, day or datetime.strptime(settings.BOT_TEST_DATE, "%Y%m%d").date())
    return code


def _today(args: argparse.Namespace) -> date:
    if args.bot_test:
        return datetime.strptime(settings.BOT_TEST_DATE, "%Y%m%d").date()
    return datetime.now(tz=KST).date()


def _replay_problem(day: date, now: datetime) -> str | None:
    if day > now.date():
        return f"{day} 은 아직 오지 않은 날입니다."
    if day.weekday() >= 5:
        return f"{day} 은 {'토일'[day.weekday() - 5]}요일이라 장이 열리지 않았습니다."
    return None


# ── B ───────────────────────────────────────────────────────

def run_b(mode: str, day: date) -> int:
    from trader import recompute

    source = root_for("A", mode) / f"journal_{day:%Y%m%d}.jsonl"
    follow = mode in ("live", "dry-run")
    if not follow and not source.exists():
        print(f"A 기록이 없습니다: {source}\n먼저 A 를 같은 옵션으로 돌리세요.")
        return 2
    print(f"[B] A 기록 {'따라 읽기' if follow else '읽기'}: {source}")
    if follow and not source.exists():
        print("    A 기록이 아직 없습니다. A 가 시작하면 이어서 읽습니다. (Ctrl+C 로 멈춤)")
    try:
        engine = recompute.run(source, root_for("B", mode), follow=follow)
    except KeyboardInterrupt:
        print("\n[B] Ctrl+C 로 멈췄습니다. 다시 켜면 A 기록을 처음부터 다시 읽어 이어 갑니다.")
        return 0
    print(f"[B] {recompute.summary(engine)}")
    print(f"[B] 기록: {engine.journal.path()}")
    return 0


# ── A ───────────────────────────────────────────────────────

def run_a(args: argparse.Namespace, mode: str, day: date | None) -> int:
    from trader.bot import TraderBot

    if mode == "bot-test":
        return _run_a_bot_test(args)

    from backend.config import ConfigError
    from backend.nh_api import runtime
    from backend.nh_api.session import create_session
    from trader import credentials

    try:
        config, source = credentials.load()
    except ConfigError as exc:
        print(exc)
        return 2
    print(f"인증 정보: {source}")
    session = create_session(config=config)
    runtime.use(session)
    feed = None
    try:
        try:
            session.tokens.token("overseas", "paper")
        except Exception as exc:  # noqa: BLE001 — 인증이 안 되면 어떤 조회·주문도 못 합니다
            print(f"해외 모의 접속 토큰을 받지 못했습니다: {exc}\n"
                  "week03/.env 의 NH_PAPER_OVERSEAS_APP_KEY·SECRET 을 확인하세요 (GitHub 이슈 #4).")
            return 2
        hours = args.session or _fixed_hours()
        trade_end = time.fromisoformat(settings.TRADE_END)
        if mode == "replay":
            assert day is not None
            sess = Session.of(day, *hours, trade_end)
            if sess.close > datetime.now(tz=KST):
                print(f"리플레이할 수 없습니다: {day} 장({sess.open:%m-%d %H:%M} ~ {sess.close:%m-%d %H:%M})이 아직 끝나지 않았습니다.")
                return 2
            return _run_a_replay(args, session_obj=sess, day=day)

        from trader.broker import OverseasBroker
        from trader.market import OverseasMarket, session_for

        clock = RealClock()
        sess = session_for(clock.now(), hours, trade_end)
        journal = DayJournal(args.symbol, sess.open.date(), clock=clock, root=root_for("A", mode))
        market = OverseasMarket(args.symbol)
        if not args.no_ws:
            feed = _start_feed(session, market, journal)
            market.feed = feed
        print(f"장: {sess.open:%m-%d %H:%M} ~ {sess.close:%m-%d %H:%M} · 신규 진입 마감 {sess.entry_cutoff:%H:%M} · "
              f"강제 청산 {sess.force_close:%H:%M} · 기록 {journal.path()}")
        bot = TraderBot(symbol=args.symbol, market=market,
                        broker=OverseasBroker(args.symbol, journal, clock, dry_run=mode == "dry-run"),
                        journal=journal, clock=clock, session=sess, environment="paper",
                        extra_start={"realtime": not args.no_ws})
        reason = bot.run()
        if reason == "NOT_TODAY":
            print(f"오늘 장({sess.close:%H:%M} 마감)이 이미 끝났습니다.")
        print(f"기록: {journal.path()}")
        return 0
    finally:
        if feed is not None:
            feed.stop()
        session.close()


def _fixed_hours() -> tuple[time, time]:
    return time.fromisoformat(settings.SESSION_OPEN), time.fromisoformat(settings.SESSION_CLOSE)


def _start_feed(session: Any, market: Any, journal: DayJournal) -> Any:
    from trader import credentials
    from trader.realtime import FA, NHStream, RealtimeFeed, Subscription, TickRecorder, session_credentials

    try:
        real_sym = market.real_symbol()
    except Exception as exc:  # noqa: BLE001
        journal.error("overseas_price", exc, note="실시간 종목코드 확인 실패 — 조회 종목코드로 구독")
        real_sym = market.symbol
    subs = [Subscription(FA, real_sym)]

    def on_status(kind: str, detail: dict[str, Any]) -> None:
        if kind in ("init", "disconnected", "fatal", "error", "RT_FAIL", "subscribe_fail"):
            try:
                journal.write("WS_STATUS", kind=kind, detail=detail)
            except OSError:
                pass

    feed = RealtimeFeed(
        lambda data, status: NHStream(credentials.ws_url(), session_credentials(session), subs, data, status),
        listeners=[TickRecorder(RESULTS_DIR / "ticks", market.symbol)], on_status=on_status,
    )
    feed.start()
    if not feed.ready.wait(timeout=15):
        print("실시간 체결(웹소켓) 연결을 아직 못 했습니다. REST 현재가로 대신하며 뒤에서 계속 다시 접속합니다.")
    else:
        print(f"실시간 체결 구독: FA {real_sym}")
    return feed


def _run_a_replay(args: argparse.Namespace, *, session_obj: Session, day: date) -> int:
    from trader.market import OverseasMarket

    live = OverseasMarket(args.symbol)
    first = day - timedelta(days=settings.PRELOAD_DAYS)
    try:
        bars_5m = live.fetch_bars(first, session_obj.close.date())
        bars_1m = [b for b in live.fetch_bars(day, session_obj.close.date(), 1)
                   if session_obj.open <= b.start < session_obj.close]
    except Exception as exc:  # noqa: BLE001
        print(f"{day} 봉을 받지 못했습니다: {exc}")
        return 2
    bars_5m = [b for b in bars_5m if b.start < session_obj.close]
    in_session = [b for b in bars_5m if b.start >= session_obj.open]
    if not in_session or not bars_1m:
        print(f"{day} {args.symbol} 장중 봉이 없습니다(5분봉 {len(in_session)}개, 1분봉 {len(bars_1m)}개). "
              "휴장이거나 그날 거래되지 않은 종목코드일 수 있습니다. 전에 남긴 기록은 그대로 둡니다.")
        return 2
    _save_bars("replay", day, bars_5m, bars_1m)
    return _run_sim(args, "replay", session_obj, bars_5m, bars_1m)


def _run_a_bot_test(args: argparse.Namespace) -> int:
    from trader.synthetic import make_day

    logging.getLogger("backend").setLevel(logging.ERROR)
    day = datetime.strptime(settings.BOT_TEST_DATE, "%Y%m%d").date()
    sess = Session.of(day, *(args.session or tuple(time.fromisoformat(t) for t in settings.BOT_TEST_SESSION)))
    bars_5m, bars_1m = make_day(sess)
    return _run_sim(args, "bot-test", sess, bars_5m, bars_1m)


def _run_sim(args: argparse.Namespace, mode: str, sess: Session, bars_5m: list[Any], bars_1m: list[Any]) -> int:
    from trader.bot import TraderBot
    from trader.broker import OverseasBroker
    from trader.market import HistoricalMarket

    clock = SimClock(sess.open - timedelta(minutes=10))
    journal = DayJournal(args.symbol, sess.open.date(), clock=clock, root=root_for("A", mode))
    journal.path().unlink(missing_ok=True)  # 다시 돌리면 새로 씁니다(지난 실행의 진입 횟수를 되살리지 않게)
    print(f"[A] {mode}: 장 {sess.open:%m-%d %H:%M} ~ {sess.close:%m-%d %H:%M} · 진입 마감 {sess.entry_cutoff:%H:%M} · "
          f"강제 청산 {sess.force_close:%H:%M} · 5분봉 {len(bars_5m)}개(미리 채우기 포함) · 1분봉 {len(bars_1m)}개")
    bot = TraderBot(symbol=args.symbol, market=HistoricalMarket(bars_5m, bars_1m, clock),
                    broker=OverseasBroker(args.symbol, journal, clock, dry_run=True),
                    journal=journal, clock=clock, session=sess, environment=mode)
    bot.journal.echo = False
    bot.run()
    closed = [r for r in journal.today() if r.get("type") == "TRADE_CLOSED"]
    pnl = sum(r.get("pnl_usd") or 0 for r in closed)
    print(f"[A] 거래 {len(closed)}건 · 손익 {pnl:+.2f} USD · 기록 {journal.path()}")
    return 0


def _save_bars(mode: str, day: date, bars_5m: list[Any], bars_1m: list[Any]) -> Path:
    path = RESULTS_DIR / mode / f"bars_{settings.SYMBOL}_{day:%Y%m%d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    def dump(b: Any) -> dict[str, Any]:
        return {"start": b.start.isoformat(), "end": b.end.isoformat(), "open": b.open, "high": b.high,
                "low": b.low, "close": b.close, "volume": b.volume}
    path.write_text(json.dumps({"bars_5m": [dump(b) for b in bars_5m], "bars_1m": [dump(b) for b in bars_1m]},
                               ensure_ascii=False), encoding="utf-8")
    return path


if __name__ == "__main__":
    sys.exit(main())
