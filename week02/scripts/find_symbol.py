"""삼성전자 주식선물 월물 목록과 2026년 11월물 코드를 찾습니다.

    uv run python scripts/find_symbol.py                 # 마스터를 내려받아 찾기 (인증 불필요)
    uv run python scripts/find_symbol.py --file stk_fut_opt.mst   # 받아 둔 파일로 찾기

종목 마스터 `stk_fut_opt.mst` 를 참고 저장소의 받기·파싱 코드로 읽습니다.
찾은 코드를 bot/settings.py 의 SYMBOL 에 넣고 README 에 적습니다.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from backend.domestic_futureoption.master import stk_fut_opt  # noqa: E402
from backend.master.master_download import fetch_one  # noqa: E402
from backend.master.master_files import MASTER_FILES_BY_NAME  # noqa: E402
from backend.master.master_parser import parse_master_text  # noqa: E402
from bot import settings  # noqa: E402

UNDERLYING = "삼성전자"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, help="받아 둔 stk_fut_opt.mst")
    parser.add_argument("--month", default=settings.CONTRACT_MONTH.replace("-", ""), help="만기년월 YYYYMM")
    args = parser.parse_args()

    if args.file:
        content = args.file.read_bytes()
    else:
        master = MASTER_FILES_BY_NAME["stk_fut_opt"]
        print(f"내려받는 중: {master.url}")
        with httpx.Client(timeout=30) as client:
            content = fetch_one(client, master)
    table = parse_master_text(content.decode("utf-8"), stk_fut_opt.FIELDS, name="stk_fut_opt", market="domestic")

    rows = [
        r for r in table.rows_as_dicts()
        # 기초자산명 칸은 `S11` 같은 코드로 와서 한글종목명(`삼성전자   F 202611 (  10)`)으로 거릅니다.
        if r["fo_tp"] == "F" and r["opt_tp"] == "1"
        and r["name_kor"].split()[0:1] == [UNDERLYING]
    ]
    if not rows:
        print(f"{UNDERLYING} 선물 행을 찾지 못했습니다. 전체 {len(table)}행.")
        return 1

    print(f"\n{'종목코드':<10} {'만기년월':<8} {'주야간':<6} {'승수':>6} {'잔존일':>5}  종목명")
    for r in sorted(rows, key=lambda r: (r["exp_ym"], r["cme_tp"])):
        mark = "  ◀" if r["exp_ym"].startswith(args.month) and r["cme_tp"] == "day" else ""
        print(f"{r['series']:<10} {r['exp_ym']:<8} {r['cme_tp']:<6} {r['ctrt_size']:>6} {r['rem_cnt']:>5}  {r['name_kor']}{mark}")

    # 만기년월 칸은 만기일(YYYYMMDD) 여덟 자리로 옵니다.
    target = [r for r in rows if r["exp_ym"].startswith(args.month)]
    if target:
        print(f"\n{args.month} 월물: " + ", ".join(f"{r['series']} ({r['name_kor']}, {r['cme_tp']})" for r in target))
        print("→ 주간 종목코드를 bot/settings.py 의 SYMBOL 에 넣으세요.")
    else:
        print(f"\n{args.month} 월물이 없습니다. 위 목록에서 만기년월을 확인하세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
