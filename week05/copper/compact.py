"""Keep human-facing results together with strategy/day journals."""

import csv
import json
from datetime import datetime
from pathlib import Path


def compact_results(root):
    root = Path(root)
    consolidated = root / "results.json"
    tables = json.loads(consolidated.read_text(encoding="utf-8")) if consolidated.exists() else {}
    sources = sorted(root.glob("*.csv"))
    details = root / "details"
    # Read every source before moving files so a failed read leaves the current results intact.
    for path in sorted(details.glob("*.csv")) + sources:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            tables[path.stem] = list(csv.DictReader(handle))
    working = root / "results.tmp"
    working.write_text(json.dumps(tables, ensure_ascii=False, indent=2), encoding="utf-8")
    working.replace(root / "results.json")
    report = root / "report.md"
    if not report.exists():
        lines = ["# 전략 시뮬레이션 결과", "", "dashboard.html에서 날짜별 A/B/C 결과를 확인할 수 있습니다.", "",
                 "| 전략 | 순손익 USD | 거래 수 | 승률 | MDD USD |", "| --- | ---: | ---: | ---: | ---: |"]
        for row in tables.get("summary", []):
            if row["regime"] == "ALL":
                lines.append(f'| {row["strategy"]} | {float(row["net"]):,.2f} | {row["trades"]} | '
                             f'{100 * float(row["win_rate"]):.2f}% | {float(row["mdd"]):,.2f} |')
        lines.extend(["", "전략별 일일 기록은 A/B/C 폴더의 journal_YYYYMMDD.jsonl에 있습니다.",
                      "전체 표 데이터는 results.json에 통합되어 있습니다.",
                      "MDD는 청산 실현손익 기준입니다. 과거 호가가 없어 실시간 스프레드 필터는 재현하지 않았습니다."])
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    bars = tables.get("bars", [])
    trades = tables.get("trades", [])
    holds = tables.get("holds", [])
    positions = tables.get("open_positions", [])
    variants = {r["strategy"] for r in tables.get("summary", [])}
    days = sorted({r["start"][:10] for r in bars} | {r["exit_at"][:10] for r in trades})
    for variant in sorted(variants):
        folder = root / variant
        folder.mkdir(exist_ok=True)
        for day in days:
            records = [dict(type="BAR", ts=row["start"], **row) for row in bars if row["start"][:10] == day]
            for kind, rows, time_key in (("TRADE", trades, "exit_at"), ("HOLD", holds, "at")):
                records.extend(dict(type=kind, ts=row[time_key], **row) for row in rows
                               if row["strategy"] == variant and row[time_key][:10] == day)
            records.sort(key=lambda r: datetime.fromisoformat(r["ts"]))
            day_trades = [r for r in trades if r["strategy"] == variant and r["exit_at"][:10] == day]
            records.insert(0, dict(type="RUN_START", strategy=variant, date=day, source="results.json"))
            records.append(dict(type="RUN_END", strategy=variant, date=day, closed_trades=len(day_trades),
                                net_pnl=sum(float(r["net"]) for r in day_trades),
                                open_positions=[r for r in positions if r["strategy"] == variant
                                                and r["entry_at"][:10] == day]))
            target = folder / f"journal_{day.replace('-', '')}.jsonl"
            temporary = target.with_suffix(".tmp")
            temporary.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records), encoding="utf-8")
            temporary.replace(target)
    for source in sources + list(details.glob("*.csv")):
        source.unlink()
    if details.is_dir() and not any(details.iterdir()):
        details.rmdir()
