"""Reproduce the two requested historical calibration and validation studies."""

import json
from collections import Counter
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path

from .market import load_bars
from .replay import replay
from .results import export, export_sensitivity, metrics, write_csv, read_rows
from .strategy import Costs, ENTRY_OPEN, FORCE_CLOSE, IndicatorSeries, calibrate

ROOT = Path(__file__).resolve().parents[1] / "results" / "period-studies"
PERIODS = (
    ("study1", "2026-10-05", "2026-10-16", "2026-10-19", "2026-10-23"),
    ("study2", "2026-10-19", "2026-10-30", "2026-11-02", "2026-11-06"),
)
LABELS = {"RANGE": "횡보", "TREND": "추세", "HIGH_VOL": "고변동성", "NEUTRAL": "중립", None: "지표 부족"}


def money(value):
    return f"{'-' if value < 0 else ''}${abs(value):,.2f}"


def markdown_table(headers, rows):
    return "\n".join(["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |",
                       *("| " + " | ".join(map(str, row)) + " |" for row in rows)])


def main():
    source = load_bars(ROOT / "raw" / "HGH27.csv")
    costs = Costs(0, 1, 12.5)
    manifest = []
    for key, *period in PERIODS:
        first, last, test_first, test_last = map(date.fromisoformat, period)
        bars = [bar for bar in source if first <= bar.start.date() <= test_last]
        thresholds = calibrate(bars, costs, symbol="HGH27", execution_symbol="HGH27", first=first, last=last)
        folder = ROOT / key
        portfolios, rows = replay(bars, thresholds, costs, first=test_first, last=test_last)
        export(folder, portfolios, rows, thresholds)
        export_sensitivity(folder, portfolios,
                           lambda ticks: replay(bars, thresholds, replace(costs, slippage_ticks=ticks),
                                                first=test_first, last=test_last)[0], 1)
        (folder / "thresholds.json").write_text(json.dumps(dict(baseline_first=str(first), baseline_last=str(last),
                                                                test_first=str(test_first), test_last=str(test_last),
                                                                thresholds=asdict(thresholds)), indent=2), encoding="utf-8")
        series = IndicatorSeries()
        base = [(bar, series.push(bar)) for bar in bars if bar.start.date() <= last]
        regime_rows = []
        for name, samples in (("baseline", [(bar, row, thresholds.regime(bar, row)) for bar, row in base]),
                              ("validation", rows)):
            for scope in ("all_returned", "09:00-23:45"):
                selected = [r for r in samples if scope == "all_returned" or ENTRY_OPEN <= r[0].start.time() < FORCE_CLOSE]
                counts = Counter(regime for _, _, regime in selected)
                for regime in ("RANGE", "TREND", "HIGH_VOL", "NEUTRAL", None):
                    regime_rows.append(dict(period=name, scope=scope,
                                            regime=regime or "UNAVAILABLE", bars=counts[regime], total=len(selected),
                                            percentage=100 * counts[regime] / len(selected)))
        write_csv(folder / "regime_ratios.csv", regime_rows, tuple(regime_rows[0]))
        daily = []
        for day in sorted({bar.start.date() for bar, _, _ in rows}):
            for p in portfolios[:3]:
                trades = [t for t in p.trades if t["exit_at"].date() == day]
                pnls = [t["net"] for t in trades]
                balance = minimum = 0
                for pnl in pnls:
                    balance += pnl
                    minimum = min(minimum, balance)
                daily.append(dict(date=str(day), strategy=p.variant, **metrics(pnls), intraday_min=minimum,
                                  daily_limit=thresholds.daily_loss_limit,
                                  reached=minimum <= -thresholds.daily_loss_limit,
                                  exceeded=minimum < -thresholds.daily_loss_limit,
                                  loss_limit_holds=sum(h["at"].date() == day and h["reason"] == "DAILY_LOSS" for h in p.holds)))
        write_csv(folder / "daily_summary.csv", daily, tuple(daily[0]))
        totals = {p.variant: metrics([t["net"] for t in p.trades]) for p in portfolios[:3]}
        paragraphs = [
            f"기준 기간은 <date start=\"{first}\" end=\"{last}\" />이며 시뮬레이션 기간은 <date start=\"{test_first}\" end=\"{test_last}\" />다. 모든 시각은 KST다.",
            "HGH27 표준 구리선물 2027년 3월물 5분봉을 사용했다. A는 고정 추세추종, B는 고정 평균회귀, C는 국면에 따라 A 또는 B의 규칙을 선택한다. C는 고변동성과 중립 국면의 신규 진입을 보류한다.",
            "## 기준 기간에서 산출한 설정",
            markdown_table(["항목", "계산 방법", "확정값"], [
                ["ER_lo", "기준 ER 30분위수", f"{thresholds.er_lo:.9f}"],
                ["ER_hi", "기준 ER 70분위수", f"{thresholds.er_hi:.9f}"],
                ["RV_hi", "기준 상대 ATR 90분위수", f"{thresholds.rv_hi:.9f}"],
                ["거래당 손실 예산", "손절금액 80분위수 + 기본 왕복비용, $5 단위 올림", money(thresholds.trade_loss_budget)],
                ["일일 손실 한도", "거래당 손실 예산 × 3", money(thresholds.daily_loss_limit)]]),
            "상대 ATR은 현재 ATR을 기준 기간 동일 KST 30분 구간 ATR 중앙값으로 나눈 값이다. HIGH_VOL을 우선 판정한 뒤 ER_hi 이상은 TREND, ER_lo 이하는 RANGE, 그 외는 NEUTRAL로 분류한다. 기준 기간 데이터로만 설정을 산출하고 검증 기간에는 고정했다.",
            "## 1. 국면별 비율",
            "아래 비율은 평일 09:00~23:45 KST 중 API가 반환한 5분봉을 분모로 계산했다. 기준 기간과 시뮬레이션 기간을 함께 제시한다. 지표 부족 봉은 별도로 포함했다.",
            markdown_table(["기간", "국면", "봉 수 / 전체", "비율"],
                           [["기준" if r["period"] == "baseline" else "시뮬레이션",
                             LABELS.get(r["regime"], "지표 부족"), f'{r["bars"]} / {r["total"]}', f'{r["percentage"]:.2f}%']
                            for r in regime_rows if r["scope"] == "09:00-23:45"]),
            "## 2. 전략별 승률과 MDD",
            markdown_table(["전략", "순손익", "거래 수", "승률", "MDD"],
                           [[v, money(m["net"]), m["trades"], f'{100*m["win_rate"]:.2f}%', money(m["mdd"])]
                            for v, m in totals.items()]),
            "MDD는 초기 실현손익 0에서 시작한 청산 거래 누적손익의 최대 고점 대비 하락액이다. 보유 중 평가손익은 포함하지 않았다. 기본 비용은 수수료 $0, 편도 슬리피지 1틱이며 HG 1틱은 $12.50이다.",
            "## 3. 편도 슬리피지 0~3틱별 순손익",
            "전략을 각 비용 조건으로 다시 실행했다. 국면 기준과 기본 손실 예산은 그대로 유지했다. 비용에 따라 진입 예산 통과 여부와 일일 손실 한도 도달 시점이 바뀔 수 있다.",
        ]
        sensitivity = [{k: str(value) for k, value in r.items()} for r in read_rows(folder / "sensitivity.csv")]
        for mode, heading in (("rerun", "### 전략 재실행"), ("reprice", "### 기본 거래 내역의 비용 재계산")):
            paragraphs.append(heading)
            paragraphs.append(markdown_table(["편도 슬리피지", "왕복 비용", "A 순손익 / 거래", "B 순손익 / 거래", "C 순손익 / 거래"],
                [[f"{ticks}틱", money(25*ticks),
                  *[money(float(r["net"])) + " / " + r["trades"] + "회"
                    for v in "ABC" for r in sensitivity if r["mode"] == mode and r["strategy"] == v and int(r["ticks"]) == ticks]]
                 for ticks in range(4)]))
        paragraphs.extend([
            "## 4. 날짜별 손익과 일일 손실 한도",
            markdown_table(["날짜", "A 순손익", "B 순손익", "C 순손익"],
                           [[day, *[money(r["net"]) for v in "ABC" for r in daily if r["date"] == day and r["strategy"] == v]]
                            for day in sorted({r["date"] for r in daily})]),
            f"일일 손실 한도는 {money(thresholds.daily_loss_limit)}다. 날짜별 장중 누적 실현손익 최저값으로 도달·초과 여부를 검사했다. 한도와 같은 손실은 도달, 한도보다 큰 손실은 초과로 표시한다.",
            markdown_table(["날짜", "전략", "장중 실현손익 최저", "한도 도달", "한도 초과", "한도에 의한 진입 보류"],
                           [[r["date"], r["strategy"], money(r["intraday_min"]), "예" if r["reached"] else "아니오",
                             "예" if r["exceeded"] else "아니오", r["loss_limit_holds"]] for r in daily]),
            "일일 한도는 신규 진입 중단 기준이다. 보유 포지션의 손절 체결과 시가 갭으로 한도를 초과할 수 있다.",
            "## 데이터와 해석 범위",
            f"기준 기간 반환 봉은 {len(base):,}개, 시뮬레이션 반환 봉은 {len(rows):,}개다. 전체 반환 봉의 지표 계산에는 야간·토요일 새벽 봉도 포함된다. 국면 비율 표의 분모는 평일 분석 시간으로 제한했다.",
            "반환되지 않은 시간은 국면 비율 분모에서 제외했다. 봉을 보간하지 않았으므로 ER(24)의 24구간이 항상 120분을 의미하지 않는다. 무거래로 추정되는 구간과 공급자 미반환은 독립 원본 자료 없이 확정 구분할 수 없다.",
            "신호 다음 봉의 시가에 편도 슬리피지를 적용했다. 과거 호가 자료가 없어 실시간 호가 스프레드 필터는 재현하지 않았다. 1계약·하루 최대 5회·60분 보유·23:45 강제 청산 등 기존 위험관리 규칙을 적용했다.",
            "두 실험은 2026-11-19에 실행한 회고 시뮬레이션이다. 미래 자료를 임계값 계산에 사용하지 않았으나, 실제 당시의 운용 성과와 구분해 해석해야 한다.",
            f"재현 자료는 저장소 week05/results/period-studies/{key}/에 저장했다. dashboard.html은 날짜별 화면, report.md는 보고서, results.json은 전체 표 데이터다. A/B/C 폴더에는 journal_YYYYMMDD.jsonl 형식의 일일 기록을 저장했다. 표 데이터는 results.json의 이름별 항목으로 읽는다.",
        ])
        content = "\n\n".join(paragraphs)
        (folder / "report.md").write_text(content, encoding="utf-8")
        from .compact import compact_results
        from .dashboard import render
        from .strategy import KST
        from datetime import datetime

        compact_results(folder)
        (folder / "dashboard.html").write_text(render(folder, datetime.now(KST)), encoding="utf-8")
        title = f"HGH27 · 기준 {first:%m/%d}~{last:%m/%d} → 시뮬레이션 {test_first:%m/%d}~{test_last:%m/%d}"
        manifest.append(dict(title=title, content=content))
        print(key, "baseline", len(base), "validation", len(rows), "ABC", totals, flush=True)
    (ROOT / "notion_pages.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
