"""저장된 완료 거래일을 동일 설정으로 재생하고 대시보드를 생성합니다. API·주문 없음."""
from dataclasses import replace
import json
from datetime import datetime

from spread.market import read_bars
from spread.replay import run_replay
from spread.strategy import KST, Settings
from frontend.app import RESULTS, SCENARIOS, export


def main():
    paths = sorted(RESULTS.glob("bars_USD_*.json"))
    if not paths:
        raise ValueError("가격 JSON이 없습니다. main.py collect --date YYYYMMDD로 먼저 조회하세요.")
    cfg = Settings.load()
    # 모든 입력을 먼저 검증해 잘못된 파일 때문에 일부 로그만 덮어쓰지 않습니다.
    inputs = []
    for path in paths:
        pairs = read_bars(path)
        days = {p.near.start.date() for p in pairs}
        if len(days) != 1 or max(days) >= datetime.now(KST).date():
            raise ValueError(f"완료된 거래일 하루 자료가 필요합니다: {path}")
        if path.stem[-8:] != pairs[0].near.start.strftime("%Y%m%d"):
            raise ValueError(f"파일명과 봉 날짜가 다릅니다: {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not raw.get("provenance", {}).get("market_evidence"):
            raise ValueError(f"과거 시장 자료 출처가 확인되지 않았습니다: {path}")
        inputs.append((path, pairs, raw))
    costs = {"base": (1, 0), "zero": (0, 0), "high": (2, 500)}
    for path, pairs, raw in inputs:
        for key, label, folder in SCENARIOS:
            slip, fee = costs[key]
            for variant, z in (("A", 1.0), ("B", 1.5), ("C", 2.0)):
                result = run_replay(pairs, replace(cfg, entry_z=z, slippage_ticks=slip, fee_per_leg_side=fee),
                                    RESULTS / folder / variant,
                                    provenance={**raw["provenance"], "input": str(path.relative_to(RESULTS.parent.parent)),
                                                "bars_sha256": raw["bars_sha256"]})
                print(f"{path.stem[-8:]} {label} {variant}({z}σ): {result['closed_pairs']}거래 / {result['net_pnl']:,.0f}원")
    export()
    print(f"대시보드: {RESULTS / 'dashboard.html'}")


if __name__ == "__main__":
    main()
