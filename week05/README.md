# WEEK 05 HGH27

결과는 이전 주차와 같이 `dashboard.html`, `report.md`, 전략별 `A/B/C/journal_YYYYMMDD.jsonl`에서 확인한다. 기존 CSV 파일은 통합 JSON으로 변환한 뒤 제거했다. 아래 초기 설계의 CSV 이름은 이제 각 폴더 `results.json`의 같은 이름 항목에 대응한다. 예를 들어 `trades.csv`는 `results.json`의 `trades` 항목이다. 원본 봉은 `raw/results.json`의 종목별 항목에서 읽는다.

두 기간 실험은 `uv run python -m copper.period_studies`로 재현한다. `results/period-studies/study1/dashboard.html`과 `study2/dashboard.html`에서 각각 날짜별 A/B/C 결과를 확인할 수 있다.

2026-11-19 update. The active signal and execution instrument is standard Copper March 2027 (HGH27), 25,000 pounds per contract, price tick 0.0005, USD 12.50 per tick. Fees remain USD 0 and replay slippage remains 1 tick per side (USD 25 round trip). Thresholds and risk budgets were recalibrated from Nov 2-13 HG data. The dashboard contains Nov 16-18 replay results. Earlier Micro Copper results are archived under results/archive-MHGH27.

NH paper execution verified on Nov 19 at 18:21 KST: buy 1 at 5.7485, sell 1 at 5.7465, final position 0 and pending orders 0. Verification trades are recorded under results/paper-verification/HGH27_20261119_182129 and kept outside strategy performance.

The historical Micro Copper design below describes the original assignment. Use HGH27 for current collection, calibration and paper execution.

# NH망스 WEEK 05 · 마이크로 구리선물 시장 국면별 전략

확정된 5분봉마다 ER(24), Wilder ATR(14), EMA(9/21), 직전 24봉 표본 표준편차를 사용한 Z-score를 계산한다. KST 30분 구간별 기준 ATR 중앙값으로 상대 ATR을 구한다. 국면은 `HIGH_VOL` → `TREND` → `RANGE` → `NEUTRAL` 순서로 판정한다.

실행 모드는 과거 데이터 시뮬레이션(`replay`)과 실시간 NH 모의계좌 매매(`paper`)다. A/B/C 비교는 과거 데이터 시뮬레이션에서 수행하고 실시간 모의계좌에는 C를 적용한다.

| 전략 | 진입 | 청산 |
| --- | --- | --- |
| A | EMA 방향 조건의 거짓 → 참 전환 | 반대 방향 EMA 교차 |
| B | Z-score ±1.5 진입 조건의 거짓 → 참 전환 | 매수 Z ≥ -0.25, 매도 Z ≤ +0.25 |
| C | ER 조건과 A 또는 B 조건을 동시에 만족하는 첫 봉 | 최초 진입에 사용한 규칙 유지 |
| C-1 | C의 고변동성 진입 차단 해제 | C와 동일. 리플레이에서 실행 |
| A-unlimited | A와 동일 | 60분 보유 제한 해제. 강제 청산 유지. 리플레이에서 실행 |

## 비용과 운영 기준

- 사용자 설정에 따라 계약당 수수료는 **$0**, 기본 리플레이 편도 슬리피지는 **1틱**으로 계산한다. 1틱은 가격 $0.0005, 계약 손익 $1.25이며 기본 왕복 비용은 $2.50이다. 모의계좌는 NH REST에서 확인한 체결가를 기록한다. [CME 마이크로 구리선물 명세](https://www.cmegroup.com/articles/faqs/micro-copper-futures-faq.html)
- 공통 손절은 `max(신호 봉 ATR × 1.5, 8틱)`을 틱 단위로 올림한다. 2026-11-02~13의 손절 금액 80분위수에 기본 왕복 비용을 더하고 $5 단위로 올림한다. 일일 손실 한도는 이 예산의 3배다. 1계약, 하루 최대 5회, 60분 보유 제한을 적용한다.
- KST 09:00~23:30에 진입하고 22:20~22:40에는 진입을 보류한다. 23:45에 강제 청산한다. 실시간 신규 진입에는 **10초 이내**의 웹소켓 체결가와 호가가 필요하며 스프레드 한도는 3틱이다.
- 호가 집계는 KST 아시아 **09:00~17:00**, 런던 **17:00~23:30**, 미국 **23:30~익일 09:00** 구간을 사용한다. 각 구간의 시작 시각을 포함한다. 이 구간은 스프레드 통계 집계에 쓰인다.

## 실행

저장소 루트에서 Python 3.11 이상과 `uv`를 사용한다. 새로 설정할 때 `week05/.env.example`을 `week05/.env`로 복사한 뒤 NH 해외선물 모의계좌의 APP Key와 APP Secret을 채운다. 과거 데이터 조회와 실시간 거래는 같은 모의계좌 키를 사용한다.

```powershell
Copy-Item week05/.env.example week05/.env
uv run --project week05 python week05/main.py --help
uv run --project week05 python week05/main.py inspect --date 2026-11-13
uv run --project week05 python week05/main.py quotes --seconds 300
```

`inspect`의 1분봉과 5분봉 시각·OHLC를 비교하고 NH 분봉 라벨 기준을 확인한다. 이후 명령의 `<stamp>`는 확인한 `start` 또는 `end`로 교체한다. `collect`는 REST로 일자별 조회와 이어받기를 수행하고, `quotes`는 두 월물의 웹소켓 호가를 함께 수집한다. `coverage.csv`의 누락률은 기준 구간 평일 09:00~23:45 KST의 5분 간격을 분모로 계산한다. 휴장이나 장 운영 변경이 있으면 결측 판단에 함께 반영한다.

```powershell
uv run --project week05 python week05/main.py collect --bar-stamp <stamp>
uv run --project week05 python week05/main.py calibrate --symbol MHGH27 --selection-reason "월물 비교에서 확인한 근거"
uv run --project week05 python week05/main.py collect --bar-stamp <stamp> --last 2026-11-19
uv run --project week05 python week05/main.py replay
```

월물 비교에는 `results/raw/coverage.csv`의 거래량·0거래량 비율·누락률과 `results/liquidity/`의 스프레드 통계를 사용한다. `MHGH27`은 우선 검토 월물이다. 0거래량 봉이 20%를 초과하면 비교 결과를 보고 `MHGZ26` 또는 `HGH27` 사용 여부를 결정한다. `HGH27` 대리신호를 선택할 경우 두 수집 명령에 `--symbols HGH27 MHGH27`을 포함한다. 이 경우 `MHGH27`의 같은 시각 봉과 양의 거래량을 리플레이 진입에 요구한다. 최종 월물 선택 근거를 `--selection-reason`으로 넘기면 `results/thresholds.json`에 함께 저장된다.

`calibrate`는 기준 기간의 ER 30/70분위수와 상대 ATR 90분위수를 산출해 저장한다. 기존 파일이 있으면 종료한다. 리플레이 검증 구간은 2026-11-16~19다. 이미 분석에 활용한 날이 있으면 `replay --exclude-date YYYY-MM-DD`로 검증에서 제외한다. OHLCV 리플레이는 다음 봉 시가에 진입하며 과거 호가가 없는 상태의 결과다.

```powershell
uv run --project week05 python week05/main.py paper --bar-stamp <stamp>
```

`paper`는 NH 웹소켓 FA·FB로 현재가와 호가를 받고 C 전략의 모의 시장가 주문을 제출한다. NH REST로 확정봉·체결·미체결·잔고를 확인하고, 재조회 중 바뀐 봉 값은 지표에 반영한다. 시작 시 대상 월물의 잔고·미체결이 비어 있어야 한다. 주문 결과가 불명확하면 재주문을 잠그고 실행 기록에 상태를 남긴다. 실시간 손절은 웹소켓 체결가로 감시하고 예산을 초과한 실현 손실은 `budget_exceeded`로 표시한다.

## 결과

| 경로 | 내용 |
| --- | --- |
| `results/thresholds.json` | 월물 선택 근거, 기준 기간, ER·상대 ATR 임계값, 시간대별 ATR 중앙값, 손실 한도 |
| `results/raw/` | 확정 5분봉과 월물별 수집 품질 |
| `results/liquidity/` | 웹소켓 호가와 스프레드 평균·중앙값·분포 |
| `results/replay/` | A/B/C/C-1/A-unlimited 봉·거래·보류·성과·신호 분석 및 비용 민감도 |
| `results/paper/<실행>/` | C 모의계좌의 주문·체결·보류·성과와 실시간 호가 |
| `results/dashboard.html` | 날짜별 리플레이 비교와 저장된 NH 모의계좌 C 결과를 보는 대시보드 |

`week05` 폴더에서 `uv run python -m copper.dashboard`를 실행한 뒤 `results/dashboard.html`을 브라우저에서 연다. 대시보드는 저장된 결과를 포함하는 HTML 파일이다. 리플레이나 모의계좌 결과가 갱신되면 같은 명령으로 다시 내보낸다.

`signal_analysis.csv`는 각 전략별 적격 표본과 두 신호가 모두 적격인 공통 표본을 집계한다. 12봉 동안 시각 간격이 이어진 표본의 방향조정 수익률/ATR에 대해 평균·중앙값·양수 비율·표본 수를 기록한다. 전체 겹침 표본과 12봉 간격 비중첩 표본을 함께 출력한다. `sensitivity.csv`는 편도 0~3틱 비용으로 같은 거래를 재계산한 결과와 고정 손실 예산 아래 전략을 재실행한 결과를 제공한다. 데이터 종료 시 미청산 포지션이 있으면 `open_positions.csv`에 기록한다.

NH 인증과 실데이터 확인 후 최종 월물 선정, 실제 임계값 산출, NH 모의 주문·청산 검증 및 `screenshots/` 수집을 진행한다.
