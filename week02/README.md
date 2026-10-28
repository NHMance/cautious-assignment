# 2주차 · 국내상품 전략 구현 — 삼성전자 주식선물 장 초반 돌파 재확인

**제출일: 2026/10/30(금) 23:59 까지**

## 한눈에 보기

삼성전자 주식선물(`A116B000`)에 **장 초반 가격 돌파 재확인** 전략을 적용한 자동매매 봇입니다.

- **전략**: 09:00\~09:20 5분봉 4개로 기준 범위(고가 H, 저가 L)를 정합니다. 확정된 5분봉 종가가 범위를 벗어나고 **다음 봉에서도 유지되면** 그 방향으로 1계약 진입합니다. 한 봉만 벗어났다 돌아오는 거짓 돌파는 걸러 냅니다.
- **청산·위험 관리**: 손절 5틱, 익절 10틱, 하루 최대 2회 진입. 15:00 이후 신규 진입 없음, 15:15부터 강제 청산.
- **실행 방식**: NH선물 Open API **모의환경(paper)** 에서만 돕니다. 실전계좌 주문은 하지 않습니다.
  - 실시간 모의 트레이딩: 장중에 모의계좌로 실제 시장가 주문 → `bot.main`
  - 리플레이: 지난 하루의 실제 분봉을 받아 가상 시계로 다시 돌림, 주문 없음 → `bot.main --replay-date YYYYMMDD`
  - 봇 테스트: 목업 API 응답으로 서버·인증 없이 흐름만 점검 → `bot.main --bot-test`
- **기록**: 신호·주문·체결·손익·오류를 `results/` 에 JSONL 한 줄 한 이벤트로 남기고, `summarize.py` 로 표를 만듭니다.

```bash
uv sync && cp .env.example .env                  # 설치, .env 에 모의 APP Key·Secret 입력
uv run python -m bot.main --replay-date 20261028 # 리플레이 (지난 하루, 주문 없음)
uv run python -m bot.main                        # 실시간 모의 트레이딩 (모의계좌 실주문)
```

## 목차

- [폴더 구성](#폴더-구성)
- [1. 과제 요약](#1-과제-요약)
- [2. 실행 방법](#2-실행-방법)
  - [2-1. 설치](#2-1-설치) · [2-2. 인증 정보 설정](#2-2-인증-정보-설정) · [2-3. 실행 모드 구분](#2-3-실행-모드-구분)
  - [2-4. 리플레이·봇 테스트 (주문 없음)](#2-4-리플레이봇-테스트-주문-없음)
  - [2-5. 실시간 모의 트레이딩 (모의계좌 실주문)](#2-5-실시간-모의-트레이딩-모의계좌-실주문)
  - [2-6. 결과 요약](#2-6-결과-요약) · [2-7. 설정 바꾸기](#2-7-설정-바꾸기) · [2-8. 대시보드](#2-8-대시보드-localhost-읽기-전용)
- [3. 전략과 매매 계획](#3-전략과-매매-계획)
- [4. 사용 API와 프로그램 구조](#4-사용-api와-프로그램-구조)
- [5. 실행 환경](#5-실행-환경)
- [6. 실행 결과](#6-실행-결과)
- [7. 결과 해석과 한계](#7-결과-해석과-한계)
- [8. 오류 제보와 개선사항](#8-오류-제보와-개선사항)
- [9. 향후 계획](#9-향후-계획)

> 이 README 하나가 2주차 보고서이자 사용 설명서입니다. 수치는 모두 `results/` 의 원본 기록에서 읽은 값만 씁니다.
> 이 실행은 **모의계좌 주문**입니다. NH선물 모의환경(paper)에 실제 주문을 내고, 체결가는 `order_history` 응답에서 확인한 값입니다. 실전계좌 주문은 하지 않습니다.
> 6\~9절(결과·해석·오류·개선)은 10/28\~30 실행 뒤 채웁니다.

| 항목 | 내용 |
| --- | --- |
| 전략 이름 | 장 초반 가격 돌파 재확인 (Opening Range Breakout + 1봉 재확인) |
| 대상 종목 | 삼성전자 주식선물 2026년 11월물 (`A116B000`) |
| 실행 환경 | NH선물 모의환경 — 모의계좌 시장가 주문 |
| 실행 기간 | 2026-10-28(수) \~ 10-30(금) 09:00\~15:20 KST |
| 구현 기준 | [create-week02.md](../create-week02.md) |

## 폴더 구성

| 경로 | 내용 |
| --- | --- |
| `README.md` | 이 보고서 |
| `pyproject.toml` | 의존성 (`httpx`, `pyyaml`, `truststore`, 개발용 `pytest`) |
| [bot/](bot/) | 직접 작성한 봇 - settings.py 에서 전략 설정 |
| [backend/](backend/) | API 호출 담당(실시간/리플레이 데이터) 및 오류 판정 |
| [frontend/](frontend/) | localhost 대시보드 — `app.py`(FastAPI, 저널 읽기 전용) + `index.html` · 2-8절 |
| [scripts/](scripts/) | `find_symbol.py`(종목코드) · `preflight.py`(장 전 점검) · `summarize.py`(결과 표) |
| [tests/](tests/) | 상태 머신 단위 테스트, 하루 전체 루프 테스트, 봇 테스트용 목업 응답 5개(`fixtures/nh`) |
| [results/](results/) | 실행 근거 — `journal_YYYYMMDD.jsonl`, `preflight_YYYYMMDD.json`, `summary.md`. 리플레이는 `results/replay/`, 봇 테스트는 `results/bot-test/`, 장중 DRY_RUN 은 `results/dry-run/` 에 따로 |
| [screenshots/](screenshots/) | 실제 화면 캡처 |

## 1. 과제 요약

**과제:** NH선물 Open API 로 국내 선물 상품 하나에 대한 규칙 기반 전략을 구현하고, 실제로 실행한 기록으로 결과를 분석합니다.
수익률은 평가 기준이 아닙니다. 「계획한 대로 돌아갔는가 · API를 바르게 썼는가 · 결과를 설명할 수 있는가」를 봅니다.

| 필수 기능 | 이 과제에서 한 것 | 근거 |
| --- | --- | --- |
| 시세 수신 | 5분봉 경계마다 `chart_minute` 로 확정봉 조회, 보유 중 2초마다 `price` 로 현재가 | `BAR` 이벤트, `bot/market.py` |
| 신호 판정 | 09:00\~09:20 범위(H/L) 돌파 → 다음 봉 재확인 | `RANGE_SET`·`BREAKOUT`·`CONFIRM_OK/FAIL`, `bot/strategy.py` |
| 주문 처리 | 모의계좌 시장가 주문 → `order_history` 로 체결가 확인 → `open_interest` 로 잔고 교차 확인 | `ORDER_SUBMIT`·`FILL`, `bot/broker.py` |
| 위험 관리 | 진입 전 6개 체크, 손절 5틱·익절 10틱, 하루 2회, 15:15 강제 청산 | `RISK_BLOCK`·`EXIT_TRIGGER`, `bot/risk.py` |
| 실행 기록 | 신호·주문·체결·손익·오류를 한 줄 한 이벤트로 기록 | `results/journal_*.jsonl` |

## 2. 실행 방법

### 2-1. 설치

Python 3.11 이상과 [uv](https://docs.astral.sh/uv/) 가 필요합니다. `week02/` 폴더에서 실행합니다.

```bash
brew install uv          # macOS (최초 1회. 이미 있다면 실행 X)
cd week02
uv sync                  # .venv 생성 + 의존성 설치
uv run pytest            # 17개 테스트 통과 확인
```

### 2-2. 인증 정보 설정

국내 **모의** APP Key·Secret 만 넣으면 됩니다. 계좌 비밀번호는 쓰지 않습니다. 두 방법 중 하나를 고릅니다(**환경변수가 우선**).
실행하면 첫 줄에 `인증 정보: env` 또는 `인증 정보: config.yaml` 로 어느 쪽을 읽었는지 나옵니다.

- 모의 키는 모의거래 신청·모의계좌 개설 후, 리얼 HTS **[6128] REST API 신청** 화면에서 **모의계좌를 골라** 신청하면 발급됩니다(국내·해외 따로).
- **실전 키를 넣으면** 모의 서버 토큰 발급이 `EAU024`(발급 서비스 불일치)로 거부됩니다.

**방법 1. 환경변수 / `.env`** — `bot/credentials.py` 가 읽습니다. `.env` 는 `.gitignore` 에 있어 Git 에 올라가지 않습니다.
모의(paper) 키만 받으므로 이 방법으로는 실전 주문이 나갈 수 없습니다.

```bash
cp .env.example .env     # 열어서 NH_PAPER_APP_KEY, NH_PAPER_APP_SECRET 입력
# 또는 셸에서: export NH_PAPER_APP_KEY=... NH_PAPER_APP_SECRET=...  (셸 값이 .env 보다 우선)
```

**방법 2. 설정 파일** — `.env`·환경변수에 키가 없을 때 읽습니다. 참고 저장소와 같은 방식으로 **저장소 밖** `~/NHFutures/config/config.yaml` 에 둡니다
(Windows: `%USERPROFILE%\NHFutures\config\config.yaml`).

```yaml
default_environment: paper   # 반드시 paper. 봇은 paper 가 아니면 시작하지 않습니다.

paper:
  base_url: ""               # 비우면 https://apidemo.futures.co.kr
  domestic:
    app_key: "발급받은 모의 APP Key"
    app_secret: "발급받은 모의 APP Secret"
  overseas:
    app_key: ""
    app_secret: ""

real:
  base_url: ""
  domestic: { app_key: "", app_secret: "" }
  overseas: { app_key: "", app_secret: "" }
```

### 2-3. 실행 모드 구분

**리플레이**는 지난 하루의 실제 데이터로 전략을 다시 돌리고(주문 없음), **봇 테스트**는 목업 응답으로 프로그램 흐름만 점검합니다.
**실시간 모의 트레이딩**은 장중에 모의계좌로 실제 주문을 냅니다. 세 기록은 섞지 않습니다.

| 구분 | 모드 | 명령 | 데이터 | 주문 | 기록 |
| --- | --- | --- | --- | --- | --- |
| 리플레이 | 지난 하루 (전략 검증) | `--replay-date YYYYMMDD` | 그날 분봉을 모의 서버에서 받음 | 없음 | `results/replay/journal_YYYYMMDD.jsonl` |
| 봇 테스트 | 흐름 점검 (서버·인증 없음) | `--bot-test` | 목업 응답 (`tests/fixtures/nh`) | 없음 | `results/bot-test/journal_20261027.jsonl` |
| 실시간 모의 | 장중 DRY_RUN | `--dry-run` | 실시간 조회 | 없음 (기록만) | `results/dry-run/journal_YYYYMMDD.jsonl` |
| 실시간 모의 | **모의계좌 실주문** | (옵션 없음) | 실시간 조회 | **모의계좌 시장가** | `results/journal_YYYYMMDD.jsonl` |

기록의 `RUN_START.environment` 가 `replay` / `bot-test` / `paper` 로 구분되고, `summarize.py` 는 `paper` 기록만 셉니다.
(10/28 리플레이 기록은 이름을 바꾸기 전에 만들어 `environment` 가 옛 이름 `sim` 으로 남아 있습니다. 같은 뜻으로 읽습니다.)

### 2-4. 리플레이·봇 테스트 (주문 없음)

#### 2-4-1. 리플레이: 지난 하루로 다시 돌리기 — `--replay-date` (모의 키 필요)

```bash
uv run python -m bot.main --replay-date 20261028   # → results/replay/journal_20261028.jsonl
```

시작할 때 그날 5분봉·1분봉을 모의 서버에서 한 번 받아 `results/replay/bars_<종목>_<날짜>.json` 에 남기고,
**가상 시계**로 08:55\~15:20 을 몇 초 만에 돌립니다. 봇은 그 시각까지 확정된 봉만 봅니다(미래 봉을 보지 않음).

- 현재가 = 그 시각까지 확정된 마지막 1분봉 종가. 봉 안의 고가·저가는 보지 않아 손절·익절이 실제보다 늦게 잡히거나 빠질 수 있습니다.
- 주문은 보내지 않고 체결가는 그때 현재가로 가정합니다(슬리피지 0). 잔고·미체결·주문가능수량 조회도 하지 않습니다.
- **날짜는 자유롭게** 고릅니다. 단, 미래 날짜·주말·장이 끝나기 전(15:30 전)의 오늘은 API 를 부르기 전에 거절합니다.
- 공휴일처럼 봉이 0개인 날(또는 그날 거래되지 않은 종목코드)은 안내만 하고 끝나며, **전에 남긴 기록·봉 파일은 지우지 않습니다.**
- 봉을 받은 뒤에만 그날 기록을 새로 씁니다(같은 날짜로 다시 돌리면 덮어씀). 봉이 15:20 전에 끊기면 주의 문구를 냅니다.
- 전략 규칙이 그날 어떻게 반응했는지 보는 용도이며, 모의계좌 실행 결과(6절)로 세지 않습니다.

#### 2-4-2. 봇 테스트: 목업 응답으로 흐름 점검 — `--bot-test` (서버 접속·인증 없음)

```bash
uv run pytest                                   # 상태 머신 + 하루 전체 루프(DRY_RUN) 테스트
uv run python -m bot.main --bot-test           # → results/bot-test/journal_20261027.jsonl (1초 남짓)
```

참고 저장소의 재생 모드(`NH_REPLAY=1`)로 서버 대신 목업 응답(`tests/fixtures/nh`)이 답하고, 가상 시계로 하루를 끝까지 돌립니다.
봇 → `backend` 호출 → 응답 해석 → 상태 머신 → 기록까지 이어지는지 보는 **배선 점검**이며, 전략 검증이 아닙니다(값은 어림값).

| 목업 | 내용 |
| --- | --- |
| `domestic_chart-minute` | 2026-10-27 삼성전자 11월물(`A116B000`) 가상의 5분봉 81개 |
| `domestic_price` | 현재가 226,500 고정 |
| `domestic_order-history` | 미체결 없음 |
| `domestic_orderable-quantity` | 매수·매도 각 1계약 가능 |

시나리오: 기준 구간 H 232,000 / L 228,000 → 09:20 상향 돌파 → 09:25 **재확인 실패(거짓 돌파)** → 09:35 하향 돌파 → 09:40 재확인 성공 → 매도 진입 →
현재가가 고정이라 손절·익절 없이 **15:15 강제청산**(손익 0) → `DONE reason=CUTOFF` 로 끝나는 것이 정상입니다.

- 주문은 재생할 수 없어 항상 DRY_RUN 으로 돕니다. 계좌 응답 목업이 없어 잔고는 DRY_RUN 의 가상 잔고를 씁니다.
- 서버로 가지 않으므로 호출 제한 대기를 건너뜁니다. 기록은 매번 새로 씁니다.

### 2-5. 실시간 모의 트레이딩 (모의계좌 실주문)

#### 2-5-1. 장 시작 전 (08:30 이전)

```bash
uv run python scripts/find_symbol.py    # 종목 마스터에서 삼성전자 11월물 코드 확인 (인증 불필요)
uv run python scripts/preflight.py      # 모의 서버 점검 → results/preflight_YYYYMMDD.json
```

`preflight.py` 가 확인하는 것과 볼 곳:

| 항목 | 정상 | 아닐 때 |
| --- | --- | --- |
| 환경 | `환경: paper` | `.env` 의 모의 키 또는 설정 파일 `default_environment` 확인 |
| 토큰 발급 | 현재가 조회 성공 | APP Key·Secret 확인 (`EAU024` 면 실전 키) |
| 호가단위 | `관측 호가단위` = `settings.tick_size()` | `bot/settings.py` 의 `TICK_TABLE` 수정 후 README 8절에 근거 기록 |
| 주문가능수량 | 매수·매도 `ord_psb_qty` ≥ 1 | 모의계좌 예수금 확인 |
| 잔고·미체결 | 없음 / 0건 | HTS·앱에서 정리 후 시작 |

이 화면(예수금·주문가능금액)을 `screenshots/` 에 캡처합니다.

#### 2-5-2. 장중 실행

```bash
uv run python -m bot.main               # 모의계좌 실주문. 08:55 에 시작, 15:20 에 스스로 끝남
uv run python -m bot.main --dry-run     # 조회는 실제 서버, 주문은 보내지 않고 기록만 → results/dry-run/ (실주문 기록과 섞이지 않음)
uv run python -m bot.main --symbol A116C000   # 다른 종목코드로 (settings.SYMBOL 대신)
```

터미널에는 `BAR` 를 뺀 이벤트가 한 줄씩 찍힙니다. 하루 흐름과 확인할 것:

| 시각 | 봇이 하는 일 | 터미널에서 볼 이벤트 | 할 일 |
| --- | --- | --- | --- |
| 08:55 | 시작, 잔고·미체결·오늘 진입 횟수 복구 | `RUN_START`, `RECOVER` | `environment: paper` 확인 |
| 09:20:05 | 기준 봉 4개로 H/L 확정 | `RANGE_SET` | H/L 캡처 |
| 5분마다 +5초 | 확정봉 판정 | `BREAKOUT`, `CONFIRM_OK` / `CONFIRM_FAIL` | — |
| 진입 시 | 리스크 체크 → 시장가 주문 → 체결 확인 | `RISK_BLOCK` 또는 `ORDER_SUBMIT`·`FILL`·`ENTRY_OPEN` | HTS/앱 체결 화면 캡처 |
| 보유 중 2초마다 | 현재가로 손절·익절 감시 | `EXIT_TRIGGER`, `TRADE_CLOSED` | 청산 화면 캡처 |
| 15:00 | 신규 진입 종료 | `DONE reason=CUTOFF` | — |
| 15:15 | 보유 중이면 강제 청산 | `EXIT_TRIGGER reason=FORCE` | — |
| 15:20 | 종료 | `RUN_END position=null` | HTS/앱 잔고 0 확인·캡처 |

#### 2-5-3. 중단·재시작

- **`Ctrl+C`**: 보유 포지션 여부를 출력하고 `RUN_END interrupted=true` 를 남깁니다. **자동 청산하지 않으므로** 보유 중이라는 경고가 나오면 HTS/앱에서 직접 확인합니다.
- **다시 실행**: 같은 날 다시 `uv run python -m bot.main` 하면 오늘 journal 에서 진입 횟수를, `open_interest()` 에서 보유 포지션을 되살려 이어 갑니다.
  보유가 있으면 잔고 평균가 기준으로 손절·익절가를 다시 세웁니다. 지난 봉에서 생긴 신호로는 주문하지 않습니다(`CONFIRM_OK reason=STALE`).

### 2-6. 결과 요약

```bash
uv run python scripts/summarize.py                   # 모의 실행 기록만 → results/summary.md
uv run python scripts/summarize.py --include-test    # 봇 테스트·리플레이 기록도 포함
```

일별·전체로 거래 수, 승/패, 틱·원화 손익, 최대 손실, **돌파 vs 재확인 성공 vs 거짓 돌파**, 리스크 차단·API 오류 횟수, 슬리피지를 표로 냅니다.
재시작으로 같은 이벤트가 두 번 적힌 경우 (종류, 방향, 봉 시각)으로 중복을 빼고 셉니다.
`results/replay/`·`results/bot-test/`·`results/dry-run/` 의 기록은 읽지 않습니다.

### 2-7. 설정 바꾸기

모든 숫자는 [bot/settings.py](bot/settings.py) 한 곳에만 있습니다.

| 설정 | 기본값 | 뜻 |
| --- | --- | --- |
| `SYMBOL` | `A116B000` | 종목코드 |
| `RANGE_START` / `RANGE_END` | 09:00 / 09:20 | 기준 구간 |
| `ENTRY_CUTOFF` | 15:00 | 재확인 봉 종료가 이 시각 이하여야 진입 |
| `STOP_TICKS` / `TAKE_TICKS` | 5 / 10 | 손절·익절 틱 |
| `MAX_ENTRIES_PER_DAY` / `MAX_QTY` | 2 / 1 | 하루 진입 횟수, 계약 수 |
| `FORCE_CLOSE_START` / `FORCE_CLOSE_DEADLINE` | 15:15 / 15:20 | 강제 청산 |
| `MAX_LOSS_PER_TRADE` | 30,000 | 1회 계획 손실 한도 |
| `EST_ROUNDTRIP_COST` | 0 | 왕복 수수료 추정 (확인 후 입력) |
| `PRICE_POLL_SEC` / `BAR_FETCH_DELAY_SEC` | 2 / 5 | 현재가 조회 주기, 봉 마감 후 조회 대기 |
| `TICK_TABLE` | 20만\~50만원 500원 등 | 호가단위 표 (20만\~50만원 500원은 10/29 확인, 8-1절) |
| `DRY_RUN` | False | True 면 `--dry-run` 과 같음 |

전략 규칙을 바꾸면 3절과 `tests/test_strategy.py` 도 함께 고칩니다. 규칙과 코드가 다르면 안 됩니다.

### 2-8. 대시보드 (localhost, 읽기 전용)

```bash
uv sync --group dashboard                          # fastapi·uvicorn (처음 한 번)
uv run --group dashboard uvicorn frontend.app:app --port 8000   # → http://127.0.0.1:8000
```

봇은 터미널에서 따로 돌리고, 대시보드는 `results/journal_*.jsonl` · `results/replay/journal_*.jsonl` 을 2초마다 읽어 보여 줍니다. 주문·계좌 조회는 하지 않습니다.

- 위쪽 탭으로 **실시간 모의**(`environment=paper`)와 **리플레이**(`--replay-date`, `replay`)를 나눠 보고, 오른쪽 위 배지와 안내 띠 색(주황 / 보라)으로도 구분합니다. 마지막 `RUN_START` 뒤에 `RUN_END` 가 없으면 「진행 중」입니다.
- 봇 테스트(`--bot-test`)·단위 테스트 기록은 전략 결과가 아니라 흐름 점검용이라 화면에 나오지 않습니다.
- 왼쪽: 상태·기준 구간 H/L·포지션·손절/익절·진입 횟수·누적 손익 카드, 5분봉 차트(H/L 선, 진입 ▲·청산 ▼), 거래 내역 표
- 오른쪽: 로그 창. 새 줄이 생기면 따라 내려가고, 봉(`BAR`) 숨기기와 검색이 되며, 줄을 누르면 원문 JSON 이 펼쳐집니다.

## 3. 전략과 매매 계획

### 3-1. 대상 상품

| 항목 | 값 | 근거 |
| --- | --- | --- |
| 종목 | 삼성전자 F 202611 | `scripts/find_symbol.py` (종목 마스터 `stk_fut_opt.mst`, 10/27 받음) |
| 종목코드 | `A116B000` | 같은 출력 |
| 만기일 | 2026-11-12 | 마스터 `exp_ym` |
| 거래승수 | 10 | 마스터 `ctrt_size` |
| 가격제한 (10/27 마스터) | 192,200 \~ 234,500 | 마스터 `lower_p_lv1`·`upper_p_lv1` |

### 3-2. 가설

장 초반 20분 가격 범위를 벗어난 움직임이 **다음 5분봉에서도 유지되면** 추세가 이어질 가능성이 높다.
한 봉만 벗어났다 돌아오는 돌파(거짓 돌파)를 걸러 내는 것이 핵심이고, 거짓 돌파 횟수 자체가 검증 데이터입니다.

### 3-3. 매매 규칙 (`bot/strategy.py` 와 1:1)

1. **기준가**: 09:00·09:05·09:10·09:15 시작 5분봉 4개가 모두 확정되면 `H = 최고가`, `L = 최저가`. 장중 변경 없음.
   4개가 모이지 않으면(09:25까지) 당일 거래하지 않고 사유(`RANGE_INCOMPLETE`, 빠진 봉)를 기록.
2. **확정봉만 사용**: 종료 시각이 지나지 않은 봉(진행 중 봉)은 판단에 쓰지 않는다.
3. **돌파**: 직전 봉 종가 ≤ H 이고 이번 확정봉 종가 > H → 상향 돌파. 직전 봉 종가 ≥ L 이고 이번 종가 < L → 하향 돌파.
4. **재확인**: 돌파 다음 확정봉 종가가 여전히 H 위(L 아래)면 진입, 아니면 **거짓 돌파**로 기록하고 감시로 복귀.
   재확인에 실패한 봉이 반대쪽 돌파 조건을 만족하면 그 봉을 반대쪽 돌파로 본다.
5. **진입**: 리스크 체크 통과 시 시장가 1계약. 재확인 봉 종료가 15:00 이하일 때만(14:55\~15:00 봉까지).
6. **청산** (실제 진입 체결가 기준, 시장가):
   매수 손절 = 체결가 − 5틱, 익절 = 체결가 + 10틱 / 매도 손절 = 체결가 + 5틱, 익절 = 체결가 − 10틱.
   15:15부터 무조건 청산, 15:20까지 체결·잔고 0 확인. 실패하면 ERROR 기록 후 재시도.
7. **횟수**: 하루 최대 2회 진입. 청산 후 재진입은 **새로운 돌파**(직전 봉 종가가 다시 기준선 안쪽)가 있어야 한다.

상태 흐름: `WAIT_RANGE → WATCH → CONFIRM_LONG/SHORT → ENTERING → IN_POSITION → EXITING → WATCH` (진입 2회 소진·15:00 이후는 `DONE`)

### 3-4. 리스크 체크 (진입 직전 — `bot/risk.py`)

하나라도 실패하면 진입하지 않고 실패 항목마다 `RISK_BLOCK` 을 남깁니다.

| # | 체크 | 기준 |
| --- | --- | --- |
| 1 | 계획 손실 | 손절 5틱 × 호가단위 × 10 + 비용 ≤ 30,000원 |
| 2 | 명목금액 | 현재가 × 10 ≤ 10,000,000원 |
| 3 | 증거금 | 주문가능수량(`orderable_quantity`) ≥ 1 (필요 증거금 = 현재가 × 10 × 42.6% 함께 기록) |
| 4 | 보유 없음 | `open_interest` 에 해당 종목 없음 |
| 5 | 미체결 없음 | `order_history(exec_div="2")` 에 해당 종목 없음 |
| 6 | 횟수 | 당일 진입 < 2 |

### 3-5. 미리 알고 있던 약점

- 손절·익절을 2초 폴링으로 감시하므로 손절가를 넘어 체결될 수 있다(슬리피지). 체결가 그대로 기록하고 `slippage_ticks` 로 분석한다.
- 5분봉 종가로만 판단하므로 봉 안에서 범위를 넘었다 돌아오는 움직임은 돌파로 보지 않는다.
- 3일·하루 최대 2회라 표본이 작다. 결론보다 「거짓 돌파가 얼마나 걸러졌는가」를 본다.

## 4. 사용 API와 프로그램 구조

### 4-1. 구조

```
[NH선물 Open API — 모의환경 REST]
        ↓  backend/ (참고 저장소 코드: 인증·토큰·호출 제한·오류 판정·리플레이)
[bot/market.py]   5분봉·현재가 → Bar 목록 (확정봉만)
        ↓
[bot/strategy.py] 상태 머신 (순수 로직, API 호출 없음) → NONE / ENTER_LONG / ENTER_SHORT
        ↓
[bot/risk.py]     진입 전 6개 체크
        ↓
[bot/broker.py]   시장가 주문 → 체결 확인 → 잔고 교차 확인 (DRY_RUN 이면 가상 체결)
        ↓
[bot/journal.py]  results/journal_YYYYMMDD.jsonl  한 줄 = 한 이벤트
        ↓
[scripts/summarize.py]  기록 → results/summary.md

[bot/main.py] 위를 묶는 루프 · [bot/clock.py] 실제 시계 / 가상 시계(리플레이·봇 테스트·테스트)
[bot/backtest.py] --replay-date: 받아 둔 하루치 봉으로 market 을, 계좌 조회 없는 DRY_RUN 으로 broker 를 대신함
```

### 4-2. 사용 API

| 용도 | 함수 (`domestic_futureoption_functions`) | API | 사용 위치 |
| --- | --- | --- | --- |
| 5분봉 | `chart_minute(minutes="5")` | `domestic_chart-minute` | 봉 판정 |
| 현재가 | `price` (`last_pric`) | `domestic_price` | 손절·익절 감시, 리스크 체크 |
| 주문 | `order` (시장가 `ord_tp_cd=2`, `ord_pric=0`, FAS) | `domestic_order` | 진입·청산 |
| 체결 확인·미체결 | `order_history` | `domestic_order-history` | 체결가·체결수량, 미체결 체크 |
| 취소 | `cancel` | `domestic_cancel` | 10초 안에 체결되지 않은 잔량 |
| 잔고 | `open_interest` | `domestic_open-interest` | 청산 전 방향·수량, 체결 후 교차 확인, 재시작 복구 |
| 주문가능수량 | `orderable_quantity` | `domestic_orderable-quantity` | 증거금 체크 |
| 예수금·호가 | `deposit`, `price_bid` | `domestic_deposit`, `domestic_price-bid` | `preflight.py` |

- **신규/청산 구분 필드가 없습니다.** 보유 반대 방향 주문이 청산이므로, 청산 전 반드시 `open_interest` 로 방향·수량을 확인합니다.
- 주문 접수만으로 체결로 기록하지 않습니다 — `order_history` 의 체결가·체결수량이 있어야 `FILL`.
- 주문 응답 시간 초과 시 **재전송하지 않고** `order_history` 에서 새로 생긴 주문을 찾아 접수 여부를 확인합니다.
- 종목코드는 NH선물이 배포하는 종목 마스터(`stk_fut_opt.mst`)에서 읽습니다.

### 4-3. 기록 형식

`results/journal_YYYYMMDD.jsonl` — 한 줄이 한 이벤트(JSON). 공통 칸: `ts`(KST), `type`, `symbol`, `state`.

| type | 담긴 것 |
| --- | --- |
| `RUN_START` / `RUN_END` | 설정 스냅샷 전체, 환경(paper/replay/bot-test), DRY_RUN 여부 / 종료 시 보유·진입 횟수·H/L |
| `RECOVER` | 시작 시 복구한 진입 횟수·보유·미체결 |
| `BAR` | 확정봉 OHLCV |
| `RANGE_SET` | H, L, 사용한 봉 4개 |
| `BREAKOUT` | 방향, 돌파 봉, 직전 봉 종가 |
| `CONFIRM_OK` / `CONFIRM_FAIL` | 방향, 재확인 봉, 돌파 봉 (`entry=false` 면 사유: `AFTER_CUTOFF`·`MAX_ENTRIES`·`STALE`) |
| `RISK_BLOCK` | 실패한 체크 항목, 값, 기준 |
| `ORDER_SUBMIT` | 역할(ENTRY/EXIT), 요청 파라미터, 응답 원문, 주문번호 |
| `FILL` | 주문번호, 체결가, 수량 |
| `ENTRY_OPEN` | 진입가, 손절가, 익절가, 호가단위 |
| `EXIT_TRIGGER` | 사유(STOP/TAKE/FORCE), 트리거 가격, 손절·익절가, 시도 횟수 |
| `TRADE_CLOSED` | 진입가, 청산가, 틱 손익, 원화 손익, 슬리피지(틱) |
| `DONE` | 신규 진입 종료 사유 (`RANGE_INCOMPLETE`·`CUTOFF`·`MAX_ENTRIES`·`AFTER_CUTOFF`) |
| `ERROR` | API, 오류 종류, 메시지 코드, 메시지 |

## 5. 실행 환경

| 항목 | 내용 |
| --- | --- |
| 환경 | NH선물 Open API **모의환경(paper)** |
| 계좌 주문 | 모의계좌 시장가 주문 (실전계좌 주문 없음) |
| 초기 자금 기준 | 10,000,000원 (리스크 체크 기준값) |
| 수수료 | 추정값 0원 (`EST_ROUNDTRIP_COST`, 확인 후 반영) |
| 실행 일시 | 2026-10-28 \~ 10-30, 08:55 시작 · 15:20 종료 |
| 실행 PC | macOS, Python 3.11+, uv |
| 시간대 | 모든 시각은 한국 시간(KST) |

## 6. 실행 결과

> 장 종료 후 `scripts/summarize.py` 결과(`results/summary.md`)와 캡처를 옮깁니다.

### 6-1. 근거 파일 (`results/`)

| 파일 | 내용 |
| --- | --- |
| `journal_20261028.jsonl` \~ `journal_20261030.jsonl` | 모의 실행 기록 (실행 후 생성) |
| `preflight_YYYYMMDD.json` | 장 전 점검 결과 (실행 후 생성) |
| `summary.md` | 요약 표 (실행 후 생성) |
| `replay/journal_20261028.jsonl` · `replay/bars_A116B000_20261028.json` | 10/28 리플레이 기록과 그날 받은 봉 (주문 없음 — 6절 결과로 세지 않음) |
| `bot-test/journal_20261027.jsonl` | 봇 테스트 점검 기록 (목업 응답, DRY_RUN — 결과 분석에 쓰지 않음) |

### 6-2. 일별 결과

> 작성 예정

## 7. 결과 해석과 한계

> 돌파 수 vs 재확인 성공 수 vs 거짓 돌파 수, 재확인 후 진입의 승패, 손절가 대비 실제 청산가(슬리피지)를 분석합니다.

## 8. 오류 제보와 개선사항

### 8-1. 확인 사항

| 항목 | 내용 | 근거 |
| --- | --- | --- |
| 분봉 시각 = 봉 **시작** 시각 | 09:01:01 에 받은 5분봉 응답에 `occurs1_exch_tm=090000` 봉이 들어 있음 → 진행 중인 09:00 봉. 봉 종료 = 시작 + 5분, 종료가 지난 봉만 확정봉 | [참고 저장소](https://github.com/PetterChanHyuk/nh-futures-open-api)의 녹화본 `tests/fixtures/nh/domestic_chart-minute` |
| 체결가 필드 | 저장된 모의 시장가 체결 줄에서 `occurs1_avg_exec_pric` 가 0, `occurs1_exec_pric` 에 체결가 → `exec_pric` 우선 | [참고 저장소](https://github.com/PetterChanHyuk/nh-futures-open-api)의 녹화본 `tests/fixtures/nh/domestic_order-history` |
| 연속조회 키 | 줄이 4개뿐인 응답에도 `nxt_key` 가 옴 → 한 쪽이 꽉 찼을 때(300줄)만 이어 받음 | 같은 분봉 응답 |
| 종목 마스터 기초자산명 | `under_name` 이 `S11` 코드로 와서 한글종목명(`삼성전자   F 202611`)으로 거름 | `find_symbol.py` 실행 |
| 호가단위 | `TICK_TABLE` 은 KRX 주식 호가가격단위 기준. 10/29 01시 `preflight.py` 에서 현재가 243,000, 호가 15개의 간격이 모두 500 → 관측 호가단위 500 = `tick_size()` 500. 20만\~50만원 구간만 확인했고 다른 구간은 KRX 기준값 그대로 | `results/preflight_20261029.json` |

### 8-2. 오류 제보

> 실제로 발생한 오류만 적습니다. (실행 후 작성)

### 8-3. 개선사항

> 실행 후 작성.

## 9. 향후 계획

> 실행 후 작성.

인증정보와 계좌 식별정보는 이 폴더 어디에도 담지 않았습니다.

[전체 일정으로](../README.md)
