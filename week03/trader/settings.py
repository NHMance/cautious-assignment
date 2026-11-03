"""3주차 전략 파라미터. 모든 숫자는 이 파일 한 곳에만 둡니다.

전략: EMA(9)·EMA(21) 교차 추세추종. A 는 기본형, B 는 같은 신호에 ATR 필터를 더한 개선형.
결정 기록: GitHub 이슈 #1~#3 (2026-11-02).
"""
from __future__ import annotations

# ── 종목 ────────────────────────────────────────────────────
SYMBOL = "MKUX26"            # SGX 미니 원달러 2026-11 (종목 마스터에서 확인)
EXCH_CD = "FSGX"             # 해외 거래소코드 = F + 거래소
EXCHANGE = "SGX"             # 장운영정보의 거래소 낱말
PRODUCT = "MKU"              # 장운영정보의 품목코드
TICK = 0.0001                # 최소 호가단위
TICK_VALUE = 2.5             # 1틱 손익(USD)
POINT_VALUE = TICK_VALUE / TICK
CURRENCY = "USD"

# ── 신호 ────────────────────────────────────────────────────
BAR_MINUTES = 5
EMA_FAST = 9
EMA_SLOW = 21
ATR_PERIOD = 14              # Wilder
ATR_BASE_BARS = 20           # 기준값 = 직전 20개 봉의 ATR 평균 (현재 봉 제외)
ATR_BAND = (0.90, 1.30)      # B 만: ATR_t / 기준값 이 이 범위(양끝 포함)여야 진입
PRELOAD_DAYS = 5             # 전날 봉으로 지표를 미리 채울 때 거슬러 올라가 받을 날 수(주말·휴장 포함)

# ── 청산·위험관리 (A·B 같음) ────────────────────────────────
STOP_ATR = 1.5               # 손절 거리 = 진입 당시 ATR × 1.5
TAKE_ATR = 2.0               # 익절 거리 = 진입 당시 ATR × 2
MAX_QTY = 1
MAX_ENTRIES_PER_DAY = 5
DAILY_LOSS_LIMIT_USD = 25.0   # 그날 실현 손익 합이 -25 USD 이하가 되면 신규 진입 중단 (보유분 청산 규칙은 그대로)
EST_ROUNDTRIP_COST = 0.0     # 수수료는 손익에서 제외 (이슈 #3). 기록용 자리만 둡니다.

# ── 운영 시간 (이슈 #1, 2026-11-03 야간장으로 고정) ──────────
# 장운영정보 API 에는 MKU 줄이 없어(SGX 는 IU·UC 등 8종목만) 시간을 여기 고정합니다. 모두 한국시간.
# 봇 운영: 20:50 ~ 다음 날 06:15 — 실제 야간장 거래 시간과 같습니다 (분봉으로 확인, 06:15~08:25 거래 없음).
# 진입 마감·강제 청산은 실제 거래 마감(TRADE_END) 기준 — 포지션을 든 채 장이 닫히지 않게 합니다.
#   신규 진입: 20:50 ~ 05:30 / 강제 청산 시작: 05:45 / 종료(RUN_END): 06:15
# --session HH:MM-HH:MM 으로 운영 시간만 바꿀 수 있습니다. TRADE_END 가 그 안에 없으면 마감 기준으로 셉니다.
SESSION_OPEN = "20:50"
SESSION_CLOSE = "06:15"
TRADE_END = "06:15"
ENTRY_CUTOFF_MIN_BEFORE_CLOSE = 45
FORCE_CLOSE_MIN_BEFORE_CLOSE = 30

# ── 루프 ────────────────────────────────────────────────────
PRICE_POLL_SEC = 2           # 보유 중 손절·익절 감시 주기 (실시간 체결이 있으면 그 값을 씀)
FEED_STALE_SEC = 5           # 실시간 체결이 이보다 오래됐으면 REST 현재가로 대신
BAR_FETCH_DELAY_SEC = 5      # 봉 마감 후 조회까지 대기
FILL_TIMEOUT_SEC = 10
FILL_POLL_SEC = 1
MAX_CONSECUTIVE_LOOP_ERRORS = 10   # 루프 한 바퀴 예외가 연속 이만큼이면 멈춤(CRASH) — 그 전까지는 기록하고 이어 감
EXIT_PENDING_WAIT_ATTEMPTS = 3      # 청산 전 미체결이 남아 있으면 이만큼(× 감시 주기 2초) 기다린 뒤 취소
EXIT_STUCK_ATTEMPTS = 5            # 청산 시도가 이 횟수를 넘으면 EXIT_STUCK 경고(이후 30번마다)

# 봇 테스트(--bot-test): 서버 없이 합성 봉으로 흐름만 점검
BOT_TEST_DATE = "20261102"
BOT_TEST_SESSION = ("08:30", "17:00")


def snapshot() -> dict[str, object]:
    """RUN_START 에 남길 설정 전체."""
    return {
        name: value
        for name, value in globals().items()
        if name.isupper() and isinstance(value, (str, int, float, bool, tuple))
    }
