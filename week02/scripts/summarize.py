"""results/journal_*.jsonl → 일별·전체 표, results/summary.md 저장.

    uv run python scripts/summarize.py
    uv run python scripts/summarize.py --include-test     # 봇 테스트·리플레이 기록도 포함

재시작하면 지난 봉을 다시 보며 같은 이벤트가 다시 적힐 수 있어 (날짜, 종류, 봉 시각)으로 중복을 뺍니다.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

RESULTS = Path(__file__).resolve().parents[1] / "results"


def load(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def day_stats(records: list[dict]) -> dict:
    seen: set = set()

    def once(r: dict) -> bool:
        key = (r["type"], r.get("side"), (r.get("bar") or {}).get("start"))
        if key in seen:
            return False
        seen.add(key)
        return True

    trades = [r for r in records if r["type"] == "TRADE_CLOSED"]
    pnl = [t.get("pnl_krw") for t in trades if t.get("pnl_krw") is not None]
    ticks = [t.get("ticks") for t in trades if t.get("ticks") is not None]
    breakouts = [r for r in records if r["type"] == "BREAKOUT" and once(r)]
    oks = [r for r in records if r["type"] == "CONFIRM_OK" and once(r)]
    fails = [r for r in records if r["type"] == "CONFIRM_FAIL" and once(r)]
    ranges = [r for r in records if r["type"] == "RANGE_SET"]
    done = [r for r in records if r["type"] == "DONE"]
    starts = [r for r in records if r["type"] == "RUN_START"]
    return {
        "env": sorted({str(r.get("environment")) for r in starts}),
        "dry_run": any(r.get("dry_run") for r in starts),
        "H": ranges[-1]["H"] if ranges else None,
        "L": ranges[-1]["L"] if ranges else None,
        "trades": trades,
        "wins": sum(1 for p in pnl if p > 0),
        "losses": sum(1 for p in pnl if p < 0),
        "ticks": sum(ticks),
        "pnl": sum(pnl),
        "net": sum(t.get("pnl_net_krw") or 0 for t in trades),
        "max_loss": min(pnl) if pnl else 0,
        "breakouts": len(breakouts),
        "confirm_ok": len(oks),
        "confirm_fail": len(fails),
        "by_side": Counter((r["type"], r.get("side")) for r in breakouts + oks + fails),
        "risk_blocks": Counter(r.get("check") for r in records if r["type"] == "RISK_BLOCK"),
        "errors": Counter(f"{r.get('api')}:{r.get('code')}" for r in records if r["type"] == "ERROR"),
        "slippage": [t.get("slippage_ticks") for t in trades if t.get("slippage_ticks") is not None],
        "done": [f"{r.get('reason')}" + (f" {r.get('missing')}" if r.get("missing") else "") for r in done],
    }


def render(days: dict[str, dict]) -> str:
    lines = ["# 실행 결과 요약", "", "자동 생성: `scripts/summarize.py`", ""]
    lines += ["## 일별", "",
              "| 날짜 | 환경 | H | L | 거래 | 승/패 | 틱 합 | 손익(원) | 최대손실 | 돌파 | 재확인 성공 | 거짓 돌파 | 리스크 차단 | API 오류 | 종료 사유 |",
              "| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    total = Counter()
    for day, s in sorted(days.items()):
        env = ",".join(s["env"]) + (" (DRY)" if s["dry_run"] else "")
        lines.append(
            f"| {day} | {env} | {s['H']} | {s['L']} | {len(s['trades'])} | {s['wins']}/{s['losses']} | {s['ticks']:g} | "
            f"{s['pnl']:,} | {s['max_loss']:,} | {s['breakouts']} | {s['confirm_ok']} | {s['confirm_fail']} | "
            f"{sum(s['risk_blocks'].values())} | {sum(s['errors'].values())} | {'; '.join(s['done']) or '-'} |"
        )
        for key in ("wins", "losses", "ticks", "pnl", "net", "breakouts", "confirm_ok", "confirm_fail"):
            total[key] += s[key]
        total["trades"] += len(s["trades"])
        total["max_loss"] = min(total["max_loss"], s["max_loss"])

    rate = f"{total['confirm_ok'] / total['breakouts']:.0%}" if total["breakouts"] else "-"
    lines += ["", "## 전체", "",
              f"- 거래 {total['trades']}회, 승 {total['wins']} / 패 {total['losses']}",
              f"- 틱 손익 합 {total['ticks']:g}틱, 원화 손익 합 {total['pnl']:,}원 (비용 반영 {total['net']:,}원), 최대 손실 {total['max_loss']:,}원",
              f"- **가설 검증**: 돌파 {total['breakouts']}회 → 재확인 성공 {total['confirm_ok']}회 / 거짓 돌파 {total['confirm_fail']}회 (재확인 성공률 {rate})",
              ""]

    lines += ["## 거래 내역", "", "| 날짜 | 방향 | 진입가 | 청산가 | 사유 | 틱 | 손익(원) | 손절가 | 익절가 | 슬리피지(틱) |",
              "| --- | --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |"]
    for day, s in sorted(days.items()):
        for t in s["trades"]:
            lines.append(f"| {day} | {t.get('side')} | {t.get('entry_price')} | {t.get('exit_price')} | {t.get('reason')} | "
                         f"{t.get('ticks')} | {t.get('pnl_krw')} | {t.get('stop')} | {t.get('take')} | {t.get('slippage_ticks', '-')} |")

    lines += ["", "## 리스크 차단·오류", ""]
    for day, s in sorted(days.items()):
        if s["risk_blocks"] or s["errors"]:
            lines.append(f"- {day}: 차단 {dict(s['risk_blocks']) or '-'} / 오류 {dict(s['errors']) or '-'}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--include-test", action="store_true", help="봇 테스트·리플레이 기록도 포함")
    args = parser.parse_args()

    days: dict[str, dict] = {}
    for path in sorted(RESULTS.glob("journal_*.jsonl")):
        records = load(path)
        starts = [r for r in records if r["type"] == "RUN_START"]
        # paper 가 아닌 기록(봇 테스트·리플레이·단위 테스트)은 기본으로 뺍니다. "sim" 은 --replay-date 의 옛 이름입니다.
        not_paper = all(r.get("environment") in ("bot-test", "replay", "sim", "test") for r in starts) if starts else True
        if not_paper and not args.include_test:
            continue
        days[path.stem.removeprefix("journal_")] = day_stats(records)

    text = render(days)
    print(text)
    (RESULTS / "summary.md").write_text(text, encoding="utf-8")
    print(f"저장: {RESULTS / 'summary.md'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
