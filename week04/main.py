"""4주차 실행: check(기본), stream-check, collect, compare, paper."""
from __future__ import annotations
import argparse
from dataclasses import asdict, replace
from types import SimpleNamespace
from datetime import datetime
from pathlib import Path
import json
import os
import signal
import time
from spread.broker import PaperBroker, PairExecutor, Uncertain
from spread.market import fetch_bars, align, create_client, verify_prices, quote, load_pair, read_bars, write_bars
from spread.journal import Journal, save_json
from spread.strategy import KST, ROOT, Settings, at, quote_problem, exit_reason, entry_problem
from spread.replay import summarize, run_replay
from spread.realtime import QuoteFeed
from spread.live import BarReference, quote_signal


def run_paper(pair, cfg, root: Path, *, env_file=None):
    mode = "paper"
    day = datetime.now(KST).date()
    if day.weekday() >= 5 or not at(day, "09:00") <= datetime.now(KST) < at(day, cfg.deadline):
        raise ValueError("평일 한국시간 09:00~15:20 에 실행하세요. 휴장일은 시세 유효성으로 추가 차단합니다")
    state_dir = ROOT / "results" / "private"
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = state_dir / f"{mode}.lock"
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, f"pid={os.getpid()}\n".encode())
    os.close(fd)
    state_path = state_dir / f"{mode}_{day:%Y%m%d}.json"
    client = None
    previous_handlers = {}
    stopped = False
    journal = Journal(root, mode, echo=True)
    engine = None
    feed = reference_feed = None
    last_quotes = None
    uncertain = False
    end_reason = "DEADLINE"

    def stop_handler(signum, frame):
        nonlocal stopped
        stopped = True  # 주문 응답 대기 중 예외로 빠져나가지 않고 현재 대사부터 마칩니다.

    def persist(status, **extra):
        reference = reference_feed.latest() if reference_feed else None
        save_json(state_path, {"status": status, "day": str(day), "run": str(root),
                               "entries": engine.entries, "realized": engine.realized,
                               "halted": engine.halted, "position": engine.position,
                               "closing": engine.closing,
                               "last_bar": str(reference.end) if reference else None,
                               **extra})

    try:
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if state.get("status", "IDLE") != "IDLE":
            raise Uncertain(f"이전 실행이 {state.get('status')} 상태입니다. {state_path} 와 HTS 잔고·미체결을 대사하세요")
        client = create_client(env_file)
        n, f = pair["near"]["series"], pair["far"]["series"]
        journal.write("RUN_START", settings=asdict(cfg), environment="paper", account_orders=True,
                      provenance="NH paper KB websocket midpoint",
                      reference_source="last 24 confirmed 5-minute REST bars",
                      pair=pair, restored_entries=state.get("entries", 0))
        verify_prices(client, pair, journal)
        broker = PaperBroker(client, (n, f), journal)
        engine = PairExecutor(broker, n, f, journal, cfg)
        engine.entries, engine.realized = state.get("entries", 0), state.get("realized", 0)
        engine.halted = state.get("halted", False)
        broker.assert_positions({n: 0, f: 0})
        for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", signal.SIGTERM)):
            previous_handlers[sig] = signal.signal(sig, stop_handler)
        feed = QuoteFeed(client, (n, f))
        reference_feed = BarReference(client, (n, f), cfg)
        feed.start()
        reference_feed.start()
        startup = time.monotonic()
        version = -1
        armed = True
        last_observation = last_block = None
        closing_reason = None
        while not stopped and datetime.now(KST) < at(day, cfg.deadline):
            candidate = None
            now = datetime.now(KST)
            for source in (feed, reference_feed):
                for event, fields in source.drain_events():
                    journal.write(event, **fields)
            if feed.fatal:
                raise RuntimeError("웹소켓 초기화/구독 실패: WS_STATUS를 확인하세요")
            snapshot = feed.latest(after=version, timeout=0.25)
            now = datetime.now(KST)
            if snapshot is None:
                if engine.position and last_quotes:
                    persist("BUSY")
                    engine.close(*last_quotes, "STALE_FEED")
                    persist("HOLDING" if engine.position else "IDLE")
                if now >= at(day, cfg.force_close) and not engine.position:
                    end_reason = "FLAT_AFTER_FORCE_CLOSE"
                    break
                if time.monotonic() - startup > 30 and last_quotes is None:
                    raise RuntimeError("30초 안에 두 월물의 KB 호가를 받지 못했습니다")
                time.sleep(0.05)
                continue
            updated = snapshot.version != version
            version = snapshot.version
            nq, fq = snapshot.near, snapshot.far
            if updated:
                journal.write("QUOTE_PAIR", near_quote=nq, far_quote=fq,
                              near_received=snapshot.near_received, far_received=snapshot.far_received)
            problem = quote_problem(nq, fq, now, cfg)
            reference = reference_feed.latest()
            sig, reason, stats = quote_signal(reference, nq, fq, now, cfg)
            observation = (problem or reason, reference, round(fq.mid - nq.mid, 8))
            if updated and observation != last_observation:
                journal.write("OBSERVATION", reason=problem or reason, **stats)
                last_observation = observation
            if not problem and reason == "NO_SIGNAL":
                armed = True
            if updated and not problem and armed:
                candidate = sig
            # 잔량·호가 폭 필터는 신규 진입에 적용합니다. 모의계좌 청산은 시장가입니다.
            stale = problem in ("STALE_QUOTE", "QUOTE_TIME_SKEW", "INVALID_BOOK", "INSUFFICIENT_DEPTH")
            last_quotes = nq, fq
            if engine.position:
                if stale:
                    closing_reason = closing_reason or "STALE_FEED"
                closing_reason = closing_reason or exit_reason(engine.position, nq, fq, now, cfg, engine.realized)
                journal.write("MARK", ts=now, liquidation_pnl=engine.position.liquidation_pnl(nq, fq, cfg.fee_per_leg_side),
                              frozen_mean=engine.position.signal.mean, frozen_std=engine.position.signal.std)
                if closing_reason:
                    if closing_reason == "DAILY_LOSS":
                        engine.halted = True
                    persist("BUSY")
                    if engine.close(nq, fq, closing_reason):
                        closing_reason = None
            if candidate and not engine.position and not closing_reason:
                block = (problem or entry_problem(candidate, nq, fq, cfg) or ("HALTED" if engine.halted else
                         "MAX_ENTRIES" if engine.entries >= cfg.max_entries else
                         "DAILY_LOSS" if engine.realized <= -cfg.daily_loss_krw else
                         "ENTRY_CUTOFF" if now >= at(day, cfg.entry_cutoff) else None))
                if block:
                    if block != last_block:
                        journal.write("SKIP", reason=block, signal=candidate)
                    last_block = block
                else:
                    def refresh_quotes():
                        latest = feed.latest()
                        current = reference_feed.latest()
                        if stopped or not latest or current != reference or current.problem(datetime.now(KST), cfg):
                            return None
                        if datetime.now(KST) >= at(day, cfg.entry_cutoff):
                            return None
                        return latest.near, latest.far
                    persist("BUSY", entries=engine.entries + 1)
                    engine.open(candidate, nq, fq, refresh_quotes=refresh_quotes)
                    armed = False
                    last_block = None
            persist("HOLDING" if engine.position else "IDLE")
            if now >= at(day, cfg.force_close) and not engine.position:
                end_reason = "FLAT_AFTER_FORCE_CLOSE"
                break
        if stopped:
            end_reason = "OPERATOR_STOP"
        if engine.position and last_quotes:
            persist("BUSY")
            engine.close(*last_quotes, "OPERATOR_STOP" if stopped else "DEADLINE")
        broker.assert_positions(engine.expected())
        if engine.position:
            raise Uncertain("종료 시 포지션이 남았습니다. HTS 잔고·미체결을 확인하세요")
        persist("IDLE")
    except BaseException as exc:
        end_reason = type(exc).__name__
        uncertain = isinstance(exc, Uncertain)
        journal.write("HALT", error_kind=type(exc).__name__,
                      api=getattr(exc, "api_id", None),
                      cause=type(exc.__cause__).__name__ if exc.__cause__ else None,
                      detail=str(exc) if isinstance(exc, (Uncertain, ValueError, RuntimeError)) else "API/시스템 오류: 예외 종류와 원본 API 기록 확인",
                      manual_reconciliation=uncertain)
        # 상태 불명은 반대주문도 보내지 않습니다. 상태가 알려진 보유분만 비상 청산합니다.
        if engine and engine.position and not uncertain and last_quotes:
            try:
                engine.halted = True
                persist("BUSY")
                engine.close(*last_quotes, "ERROR_EXIT")
                persist("HALTED" if engine.position else "IDLE")
            except BaseException:
                uncertain = True
        if engine and uncertain:
            engine.halted = True
            persist("UNKNOWN")
        raise
    finally:
        if feed:
            feed.stop()
        if reference_feed:
            reference_feed.stop()
        if engine:
            journal.write("RUN_END", reason=end_reason, flat=not uncertain and engine.position is None,
                          expected_positions=engine.expected(), halted=engine.halted, net_pnl=engine.realized)
            summarize(journal.path)
        if client:
            client.session.close()
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        lock.unlink(missing_ok=True)  # 이번 실행이 만든 잠금만 해제합니다.
    return root


def run_stream_check(pair, root, *, env_file=None, seconds=10):
    """주문 없이 KB 연결·호가 수신만 확인합니다."""
    client = create_client(env_file)
    feed = None
    journal = Journal(root, "stream-check", echo=True)
    cfg = Settings.load()
    count, version = 0, -1
    try:
        feed = QuoteFeed(client, (pair["near"]["series"], pair["far"]["series"]))
        feed.start()
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            for event, fields in feed.drain_events():
                journal.write(event, **fields)
            if feed.fatal:
                raise RuntimeError("KB 구독이 거절되었습니다. WS_STATUS를 확인하세요")
            snapshot = feed.latest(after=version, timeout=0.25)
            if snapshot and snapshot.version != version:
                version = snapshot.version
                problem = quote_problem(snapshot.near, snapshot.far, datetime.now(KST), cfg)
                journal.write("QUOTE_PAIR", near_quote=snapshot.near, far_quote=snapshot.far,
                              near_received=snapshot.near_received, far_received=snapshot.far_received,
                              quote_problem=problem)
                if problem not in {"INVALID_BOOK", "STALE_QUOTE", "QUOTE_TIME_SKEW"}:
                    count += 1
            elif snapshot is None:
                time.sleep(0.05)
        if not count:
            raise RuntimeError("확인 시간 안에 두 월물의 유효한 KB 호가를 받지 못했습니다")
        journal.write("CHECK", source="KB_websocket", quote_pairs=count, orders_submitted=False)
        print(f"KB 두 월물 호가 {count}쌍 수신 확인; 주문 없음")
    finally:
        if feed:
            feed.stop()
        client.session.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="미국달러선물 근월·차월 스프레드. 기본 명령은 조회 전용 check입니다.")
    parser.add_argument("command", nargs="?", default="check", choices=("check", "stream-check", "collect", "compare", "paper"))
    parser.add_argument("--seconds", type=float, default=10, help="stream-check 수신 확인 시간(초, 0 초과 60 이하)")
    parser.add_argument("--env-file", type=Path, help="국내 모의 인증 파일. 기본값: week04/.env")
    parser.add_argument("--date", help="collect에 사용할 완료된 거래일 YYYYMMDD")
    parser.add_argument("--input", type=Path, default=ROOT / "results/replay/bars_USD_20261109.json", help="compare 입력 가격 JSON")
    parser.add_argument("--output", type=Path, help="결과 기준 폴더. 기본값: 명령별 고정 폴더")
    parser.add_argument("--slippage-ticks", type=int, help="compare의 편도 슬리피지")
    parser.add_argument("--fee", type=float, help="compare의 레그당 편도 수수료(원)")
    args = parser.parse_args(argv)
    if not 0 < args.seconds <= 60:
        parser.error("--seconds는 0 초과 60 이하여야 합니다")
    cfg = Settings.load()
    if args.command == "compare":
        if args.slippage_ticks is not None:
            cfg = replace(cfg, slippage_ticks=args.slippage_ticks)
        if args.fee is not None:
            cfg = replace(cfg, fee_per_leg_side=args.fee)
        pairs = read_bars(args.input)
        if pairs[-1].ts > datetime.now(KST):
            parser.error("미래 시각의 확정봉은 재생할 수 없습니다")
        if len({p.near.start.date() for p in pairs}) != 1:
            parser.error("리플레이는 하루 자료씩 실행하세요")
        source = json.loads(args.input.read_text()).get("provenance", {"kind": "unverified", "market_evidence": False})
        source = {**source, "input": str(args.input)}
    elif args.command == "collect":
        if not args.date:
            parser.error("collect에는 --date YYYYMMDD가 필요합니다")
        day = datetime.strptime(args.date, "%Y%m%d").date()
        if day >= datetime.now(KST).date():
            parser.error("전일까지의 완료된 거래일을 지정하세요")
    default_root = {"check": ROOT / "results/check", "stream-check": ROOT / "results/check", "collect": ROOT / "results/replay",
                    "compare": ROOT / "results/replay", "paper": ROOT / "results/B"}
    root = args.output or default_root[args.command]
    root.mkdir(parents=True, exist_ok=True)
    if args.command == "compare":
        for label, z in (("A", 1.0), ("B", 1.5), ("C", 2.0)):
            result = run_replay(pairs, replace(cfg, entry_z=z), root / label, provenance=source)
            print(f"{label} ({z:.1f}σ): 진입 {result['entry_attempts']}회, 완료 {result['closed_pairs']}회, 가정 손익 {result['net_pnl']:,.0f}원")
    else:
        pair = load_pair()
        if args.command == "paper":
            run_paper(pair, cfg, root, env_file=args.env_file)
        elif args.command == "stream-check":
            run_stream_check(pair, root, env_file=args.env_file, seconds=args.seconds)
        else:
            client = create_client(args.env_file)
            responses = []
            journal = (Journal(root, "check") if args.command == "check" else
                       SimpleNamespace(write=lambda event, **fields: responses.append(
                           {"ts": datetime.now(KST).isoformat(), "type": event, "mode": "collect", **fields})))
            try:
                if args.command == "check":
                    verify_prices(client, pair, journal)
                    nq = quote(client, pair["near"]["series"], journal)
                    fq = quote(client, pair["far"]["series"], journal)
                    positions, pending = PaperBroker(client, (nq.symbol, fq.symbol), journal).snapshot()
                    problem = quote_problem(nq, fq, datetime.now(KST), cfg)
                    journal.write("CHECK", source="paper", near_quote=nq, far_quote=fq,
                              entry_block=problem, positions=positions, pending_count=len(pending), orders_submitted=False)
                    print(f"호가 필터: {problem or '통과'}; 잔고: {positions}; 미체결: {len(pending)}건; 주문 없음")
                else:
                    nb = fetch_bars(client, pair["near"]["series"], day, journal)
                    fb = fetch_bars(client, pair["far"]["series"], day, journal)
                    pairs, missing = align(nb, fb, datetime.now(KST))
                    if not pairs:
                        raise ValueError("두 월물의 공통 확정봉이 없습니다")
                    path = root / f"bars_USD_{day:%Y%m%d}.json"
                    write_bars(path, pairs, provenance={"kind": "NH REST historical bars", "source": "paper",
                               "market_evidence": True, "date": str(day), "pair": pair, "missing": missing,
                               "downloaded_at": datetime.now(KST)}, responses=responses)
                    print(f"공통 확정봉 {len(pairs)}개: {path}")
            finally:
                client.session.close()
    print(f"기록: {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
