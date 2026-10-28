"""전략 파라미터. 모든 숫자는 이 파일 한 곳에만 둡니다."""
from __future__ import annotations

SYMBOL = "A116B000"          # scripts/find_symbol.py 결과: 삼성전자 F 202611, 만기 2026-11-12, 승수 10
CONTRACT_MONTH = "2026-11"
MULTIPLIER = 10              # 거래승수
INITIAL_CAPITAL = 10_000_000
MAX_LOSS_PER_TRADE = 30_000  # 계획 손실 한도
INIT_MARGIN_RATE = 0.4260
MAX_QTY = 1
MAX_ENTRIES_PER_DAY = 2
BAR_MINUTES = 5
RANGE_START = "09:00"; RANGE_END = "09:20"   # 기준 구간 (5분봉 4개)
ENTRY_CUTOFF = "15:00"       # 재확인 봉 확정 시각이 이보다 이전이어야 진입
FORCE_CLOSE_START = "15:15"
FORCE_CLOSE_DEADLINE = "15:20"
STOP_TICKS = 5
TAKE_TICKS = 10
EST_ROUNDTRIP_COST = 0       # 수수료 추정값. 확인 후 입력
PRICE_POLL_SEC = 2           # 포지션 보유 중 현재가 조회 주기
BAR_FETCH_DELAY_SEC = 5      # 봉 마감 후 조회까지 대기 (봉 확정 지연 대비)
DRY_RUN = False              # True면 주문 API를 호출하지 않고 기록만

FILL_TIMEOUT_SEC = 10        # 주문 후 체결 확인을 기다리는 시간
FILL_POLL_SEC = 1            # 체결 확인 조회 주기

# 봇 테스트(--bot-test, NH_REPLAY=1)에서 쓸 종목·날짜. 목업 응답(tests/fixtures/nh)에 들어 있는 값입니다.
BOT_TEST_SYMBOL = SYMBOL
BOT_TEST_DATE = "20261027"

# 호가단위 표: (이 가격 미만, 호가단위). 마지막 줄은 그 이상 전부.
# KRX 주식 호가가격단위(2023.1~) 기준. 20만~50만원 구간 500원은 10/29 preflight.py 로 확인했습니다
#   (현재가 243,000, 호가 15개 간격 모두 500 → results/preflight_20261029.json). 다른 구간은 확인하지 않은 값입니다.
TICK_TABLE: tuple[tuple[float, float], ...] = (
    (2_000, 1),
    (5_000, 5),
    (20_000, 10),
    (50_000, 50),
    (200_000, 100),
    (500_000, 500),
    (float("inf"), 1_000),
)


def tick_size(price: float) -> float:
    """가격대별 호가단위. 1틱 손익 = 호가단위 × MULTIPLIER."""
    for upper, tick in TICK_TABLE:
        if price < upper:
            return tick
    return TICK_TABLE[-1][1]


def snapshot() -> dict[str, object]:
    """RUN_START 에 남길 설정 전체."""
    return {
        name: value
        for name, value in globals().items()
        if name.isupper() and isinstance(value, (str, int, float, bool, tuple))
    }
