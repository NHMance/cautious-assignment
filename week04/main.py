"""4주차 실행: check(기본), collect, compare, paper. 기본값은 읽기 전용입니다."""
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
from spread.strategy import KST, ROOT, Settings, Strategy, at, quote_problem, exit_reason, entry_problem
from spread.replay import summarize, run_replay


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
    strategy = Strategy(cfg)
    last_quotes = None
    uncertain = False
    end_reason = "DEADLINE"

    def stop_handler(signum, frame):
        nonlocal stopped
        stopped = True  # 주문 응답 대기 중 예외로 빠져나가지 않고 현재 대사부터 마칩니다.

    def persist(status, **extra):
        save_json(state_path, {"status": status, "day": str(day), "run": str(root),
                               "entries": engine.entries, "realized": engine.realized,
                               "halted": engine.halted, "position": engine.position,
                               "closing": engine.closing, "last_bar": str(strategy.last) if strategy.last else None,
                               **extra})

    try:
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if state.get("status", "IDLE") != "IDLE":
            raise Uncertain(f"이전 실행이 {state.get('status')} 상태입니다. {state_path} 와 HTS 잔고·미체결을 대사하세요")
        client = create_client(env_file)
        n, f = pair["near"]["series"], pair["far"]["series"]
        journal.write("RUN_START", settings=asdict(cfg), environment="paper", account_orders=True,
                      provenance="NH paper REST quotes",
                      pair=pair, restored_entries=state.get("entries", 0))
        verify_prices(client, pair, journal)
        broker = PaperBroker(client, (n, f), journal)
        engine = PairExecutor(broker, n, f, journal, cfg)
        engine.entries, engine.realized = state.get("entries", 0), state.get("realized", 0)
        engine.halted = state.get("halted", False)
        broker.assert_positions({n: 0, f: 0})
        last_done = state.get("last_bar")
        last_done = datetime.fromisoformat(last_done) if last_done else None
        for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", signal.SIGTERM)):
            previous_handlers[sig] = signal.signal(sig, stop_handler)
        next_bars = 0
        closing_reason = None
        while not stopped and datetime.now(KST) < at(day, cfg.deadline):
            candidate = None
            now = datetime.now(KST)
            if time.monotonic() >= next_bars:
                nb = fetch_bars(client, n, day, journal)
                fb = fetch_bars(client, f, day, journal)
                now = datetime.now(KST)
                pairs, missing = align(nb, fb, now)
                journal.write("DATA_ALIGNMENT", missing=missing, aligned=len(pairs))
                for p in pairs:
                    if strategy.last and p.ts <= strategy.last:
                        continue
                    sig, reason, stats = strategy.observe(p)
                    journal.write("OBSERVATION", ts=p.ts, reason=reason, **stats)
                    if sig and (last_done is None or sig.ts > last_done) and 0 <= (now - sig.ts).total_seconds() <= 30:
                        candidate = sig
                    elif sig:
                        journal.write("SKIP", ts=p.ts, reason="OLD_SIGNAL")
                # 다음 5분봉 확정 시각의 5초 뒤에 갱신합니다.
                next_boundary = ((int(now.timestamp()) // 300) + 1) * 300 + 5
                next_bars = time.monotonic() + max(1, next_boundary - now.timestamp())
            nq, fq = quote(client, n, journal), quote(client, f, journal)
            now = datetime.now(KST)
            problem = quote_problem(nq, fq, now, cfg)
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
                    journal.write("SKIP", reason=block, signal=candidate)
                else:
                    persist("BUSY", entries=engine.entries + 1)
                    engine.open(candidate, nq, fq)
            persist("HOLDING" if engine.position else "IDLE")
            if now >= at(day, cfg.force_close) and not engine.position:
                end_reason = "FLAT_AFTER_FORCE_CLOSE"
                break
            time.sleep(5)
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


def main(argv=None):
    parser = argparse.ArgumentParser(description="미국달러선물 근월·차월 스프레드. 기본 명령은 조회 전용 check입니다.")
    parser.add_argument("command", nargs="?", default="check", choices=("check", "collect", "compare", "paper"))
    parser.add_argument("--env-file", type=Path, help="국내 모의 인증 파일. 기본값: week04/.env")
    parser.add_argument("--date", help="collect에 사용할 완료된 거래일 YYYYMMDD")
    parser.add_argument("--input", type=Path, default=ROOT / "results/replay/bars_USD_20261109.json", help="compare 입력 가격 JSON")
    parser.add_argument("--output", type=Path, help="결과 기준 폴더. 기본값: 명령별 고정 폴더")
    parser.add_argument("--slippage-ticks", type=int, help="compare의 편도 슬리피지")
    parser.add_argument("--fee", type=float, help="compare의 레그당 편도 수수료(원)")
    args = parser.parse_args(argv)
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
    default_root = {"check": ROOT / "results/check", "collect": ROOT / "results/replay",
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
