"""NH 해외선물 모의계좌에서 6주차 위험관리 전략을 실행한다."""

import argparse
import asyncio
import json
from collections import Counter
from dataclasses import asdict, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

from copper.results import export, write_csv
from copper.strategy import BASE_FIRST, BASE_LAST, KST, RUN_END, STEP, TEST_FIRST, TEST_LAST, Costs, Thresholds, calibrate

ROOT = Path(__file__).resolve().parent
SYMBOL, EXCHANGE = "HGH27", "FCME"
FIELDS = ("start", "open", "high", "low", "close", "volume")


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--results", type=Path, default=ROOT / "results")
    sub = cli.add_subparsers(dest="command", required=True)
    for name in ("collect", "paper", "inspect"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--env-file", type=Path, default=ROOT / ".env")
        if name != "inspect":
            cmd.add_argument("--bar-stamp", choices=("start", "end"), required=True)
    sub.choices["inspect"].add_argument("--date", type=date.fromisoformat, default=BASE_LAST)
    sub.choices["collect"].add_argument("--last", type=date.fromisoformat, default=BASE_LAST)
    sub.choices["paper"].add_argument("--function-test", choices=[f"R{n}" for n in range(1, 7)])
    sub.choices["paper"].add_argument("--force-at", type=time.fromisoformat)
    baseline = sub.add_parser("calibrate")
    baseline.add_argument("--fee-per-side", type=float, default=0)
    baseline.add_argument("--slippage-ticks", type=int, default=1)
    sub.add_parser("replay")
    sub.add_parser("bot-test")
    dash = sub.add_parser("dashboard")
    dash.add_argument("--port", type=int, default=8606)
    return cli


def weekdays(first, last):
    for offset in range((last - first).days + 1):
        day = first + timedelta(days=offset)
        if day.weekday() < 5:
            yield day


def coverage(bars):
    days = set(weekdays(BASE_FIRST, BASE_LAST))
    counts = Counter(bar.start.hour for bar in bars if bar.start.date() in days)
    expected = len(days) * 12
    for hour in range(9, 24):
        seen = counts[hour]
        yield dict(hour=f"{hour:02d}:00", expected=expected, received=seen, missing_ratio=(expected - seen) / expected)


def freeze(root, bars, costs):
    available = {bar.start.date() for bar in bars}
    missing = set(weekdays(BASE_FIRST, BASE_LAST)) - available
    if missing:
        raise ValueError("기준 기간에 통째로 비어 있는 평일: " + ", ".join(map(str, sorted(missing))))
    thresholds = calibrate(bars, costs, symbol=SYMBOL, execution_symbol=SYMBOL)
    data = dict(baseline_first=str(BASE_FIRST), baseline_last=str(BASE_LAST),
                frozen_at=datetime.now(KST).isoformat(), thresholds=asdict(thresholds))
    with (root / "thresholds.json").open("x", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def load_thresholds(root):
    data = json.loads((root / "thresholds.json").read_text(encoding="utf-8"))
    if (data["baseline_first"], data["baseline_last"]) != (str(BASE_FIRST), str(BASE_LAST)):
        raise ValueError("기준 기간이 2026-11-09~20인 thresholds.json이 필요합니다")
    thresholds = Thresholds(**data["thresholds"])
    if (thresholds.symbol, thresholds.execution_symbol) != (SYMBOL, SYMBOL):
        raise ValueError("HGH27 기준값이 필요합니다")
    return thresholds


def records(folder):
    for path in sorted(folder.glob("journal_*.jsonl")):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def paper(root, session, args, thresholds):
    from copper.live import RiskSettings, run_live

    now = datetime.now(KST)
    if now.weekday() >= 5 or now.time() >= RUN_END:
        raise ValueError("운영 시간은 평일 KST 17:00–23:30입니다")
    risk = RiskSettings()
    scenario = args.function_test
    if args.force_at and scenario != "R6":
        raise ValueError("--force-at은 R6 기능 검증에만 사용할 수 있습니다")
    if scenario == "R1":
        risk = replace(risk, stop_minimum=1, stop_multiple=0)
    elif scenario == "R2":
        thresholds = replace(thresholds, trade_loss_budget=1)
    elif scenario == "R3":
        thresholds = replace(thresholds, daily_loss_limit=10)
    elif scenario == "R4":
        thresholds = replace(thresholds, rv_hi=0)
    elif scenario == "R5":
        risk = replace(risk, max_hold=timedelta(minutes=5))
    elif scenario == "R6":
        if not args.force_at or not max(now.time(), time(17)) < args.force_at <= risk.force:
            raise ValueError("R6은 현재 시각과 17:00 이후, 22:45 이하의 --force-at HH:MM이 필요합니다")
        risk = replace(risk, force=args.force_at)
    category = "function-test" if scenario else "paper"
    run_name = now.strftime("%Y%m%d_%H%M%S_%f") + (f"_{scenario}" if scenario else "")
    destination = root / category / run_name
    root.mkdir(parents=True, exist_ok=True)
    lock = ROOT / ".paper.lock"
    try:
        handle = lock.open("x", encoding="utf-8")
    except FileExistsError:
        raise ValueError(f"{lock} 존재. 실행 중인 봇과 NH 미체결·잔고를 확인하세요") from None
    with handle:
        handle.write(str(destination.resolve()) + "\n")
    prior = [event for folder in sorted((root / "paper").glob(f"{now:%Y%m%d}_*"))
             for event in records(folder)] if not scenario else []
    print(f"NH 모의계좌 기록 → {destination}", flush=True)
    asyncio.run(run_live(destination, session, thresholds, EXCHANGE, args.bar_stamp, previous=prior, risk=risk))
    lock.unlink()


def main():
    cli = parser()
    args = cli.parse_args()
    root = args.results
    raw = root / "raw" / f"{SYMBOL}.csv"
    if args.command == "dashboard":
        from copper.dashboard import serve

        serve(root, args.port)
        return
    if args.command == "bot-test":
        from copper.bot_test import run_scenarios

        run_scenarios(root / "bot-test")
        return

    from copper.market import api, connect, fetch_bars, load_bars

    try:
        if args.command == "calibrate":
            freeze(root, load_bars(raw), Costs(args.fee_per_side, args.slippage_ticks))
            print(root / "thresholds.json")
            return
        if args.command == "replay":
            from copper.replay import replay

            thresholds = load_thresholds(root)
            bars = load_bars(raw)
            if not any(TEST_FIRST <= bar.start.date() <= TEST_LAST for bar in bars):
                raise ValueError("11/23–27의 확정봉을 먼저 수집하세요")
            costs = Costs(thresholds.fee_per_side, thresholds.slippage_ticks, thresholds.tick_value)
            export(root / "replay", *replay(bars, thresholds, costs), thresholds)
            print(root / "replay")
            return

        session = connect(args.env_file)
        try:
            if args.command == "collect":
                if not BASE_LAST <= args.last <= TEST_LAST:
                    raise ValueError("--last는 2026-11-20~27 사이여야 합니다")
                bars = fetch_bars(SYMBOL, EXCHANGE, BASE_FIRST, args.last, args.bar_stamp)
                if not bars:
                    raise ValueError("확정봉을 받지 못했습니다")
                write_csv(raw, map(asdict, bars), FIELDS)
                write_csv(raw.parent / "coverage.csv", coverage(bars), ("hour", "expected", "received", "missing_ratio"))
                print(raw.parent)
            elif args.command == "inspect":
                from trader.market import DATE_FIELDS, TIME_FIELDS

                day = args.date.strftime("%Y%m%d")
                keys = (*DATE_FIELDS, *TIME_FIELDS, *(f"occurs1_{k}_pric" for k in ("open", "high", "low", "close")))
                samples = {}
                for minutes in ("1", "5"):
                    response = api.chart_minute(SYMBOL, exch_cd=EXCHANGE, inq_strt_dt=day, inq_ed_dt=day,
                                                dcnt=minutes, req_qty="30")
                    samples[minutes] = [{key: row[key] for key in keys if key in row}
                                        for row in response.get("Occurs1", [])]
                print(json.dumps(samples, ensure_ascii=False, indent=2))
            else:
                paper(root, session, args, load_thresholds(root))
        finally:
            session.close()
    except (ValueError, OSError) as exc:
        cli.exit(2, f"{exc}\n")


if __name__ == "__main__":
    main()
