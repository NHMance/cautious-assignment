"""`/api/overseas/…` 라우트 — 해외 계좌.

주소는 NH선물 API 이름을 그대로 씁니다(`/api/{시장}/{API 이름}`). 응답은
원문이 아니라 **화면이 쓰는 모양**입니다 — 무엇을 어떻게 바꾸는지는
`overseas_account.py` 첫머리에 있습니다.

**오류는 화면용 문구로 냅니다.** 화면에는 무엇을 못 했는지를 알립니다.
NH선물이 오류 코드를 주면 그 코드와 서버 문구를 문장에 함께 싣습니다
(`orders.lookup_refusal`). 코드가 없는 실패는 서버 문구를 기록에만 남깁니다 —
요청에 실어 보낸 값이 되비쳐 브라우저의 네트워크 탭에 남지 않게 하려는 것입니다.

예외가 둘 있습니다. 모의투자에서 제공하지 않는 조회(`EGW053`)와 시세 사용 신청이
필요한 거래소(`ETR010`)는 서버 문구 대신 정해진 문장을 내고, 까닭을 헤더
`x-nh-error-kind` 로 답니다(`orders.paper_refusal`·`orders.quote_subscription_refusal`).

    504  응답을 받지 못했습니다 (연결 실패·시간 초과)
    502  NH선물이 요청을 거절했거나 업무 처리가 거부되었습니다
    503  시장의 참고 자료를 받을 인증 정보가 어느 환경에도 없습니다
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from functools import partial
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.config import ENVIRONMENT_PATTERN
from backend.nh_api import runtime as nh_runtime
from backend.nh_api.client import TradeClient, last_environment
from backend.nh_api.reference import (
    MISSING_KEYS_STATUS,
    MissingMarketKeys,
    reference_environment,
)
from backend import order_error_days, order_outcome
from backend.nh_api.errors import (
    BusinessError,
    GatewayError,
    HttpError,
    NHApiError,
    TransportError,
    paper_unavailable,
)
from backend.nh_api import error_codes
from backend.nh_api.replay import (
    MATCH_API,
    MATCH_CROSS_ENVIRONMENT,
    MATCH_EMPTY,
    MATCH_EXACT,
    MATCH_HEADER,
    MATCH_SYNTHETIC,
    RECORD_HEADER,
    SYMBOL_HEADER,
    SYMBOL_NONE,
    SYMBOL_SAME,
    last_replay,
)
from backend.nh_api.runtime import client as default_client
from backend.master import master_service
from backend.sessions import service as session_service
from backend.sessions.hours import iso, now_kst
from backend import deposit_history, market_values, orders, quotes
from backend.overseas_futureoption import overseas_account
from backend.overseas_futureoption import overseas_chain
from backend.overseas_futureoption import overseas_history as history
from backend.overseas_futureoption import overseas_product_service
from backend.overseas_futureoption import overseas_products
from backend.overseas_futureoption import overseas_quotes
from backend.overseas_futureoption import overseas_futureoption_functions as api

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/overseas", tags=["overseas"])

# 화면에 적는 실패 문구. **시세와 계좌를 가릅니다** — 국내 라우트와 같은 문구입니다.
# 예수금을 못 받았는데 「시세를 받지 못했습니다」가 서면 사용자는 종목을 다시
# 고르고, 시세를 못 받았는데 「계좌 정보」가 서면 설정 화면을 봅니다.
_QUOTE_NOT_RECEIVED = "시세를 받지 못했습니다. 잠시 뒤 다시 시도해 주세요."
_QUOTE_REJECTED = "시세 조회가 거절되었습니다. 종목과 조회 조건을 확인해 주세요."

_NOT_RECEIVED = "계좌 정보를 받지 못했습니다. 잠시 뒤 다시 시도해 주세요."
_REJECTED = "계좌 조회가 거절되었습니다. 조회 조건을 확인해 주세요."

_HOURS_NOT_RECEIVED = "거래시간을 받지 못했습니다. 잠시 뒤 다시 시도해 주세요."
_HOURS_REJECTED = "거래시간 조회가 거절되었습니다. 잠시 뒤 다시 시도해 주세요."

_PRODUCT_NOT_RECEIVED = "품목 정보를 받지 못했습니다. 잠시 뒤 다시 시도해 주세요."
_PRODUCT_REJECTED = "품목 정보 조회가 거절되었습니다. 종목을 확인해 주세요."

# 조회 갈래마다 실패 문구와 기록에 적는 말. 국내 라우트의 표와 같은 낱말입니다.
_FAILURES = {
    "quote": (_QUOTE_NOT_RECEIVED, _QUOTE_REJECTED, "시세"),
    "account": (_NOT_RECEIVED, _REJECTED, "계좌"),
    "hours": (_HOURS_NOT_RECEIVED, _HOURS_REJECTED, "거래시간"),
    "product": (_PRODUCT_NOT_RECEIVED, _PRODUCT_REJECTED, "품목 정보"),
}


def get_client() -> TradeClient:
    """앱이 쓰는 호출부. 테스트는 이 의존성을 바꿔 끼웁니다."""
    return default_client()


# ── 시세 ────────────────────────────────────────────────────
#
# **거래소코드를 화면이 만들어 보냅니다.** 선물이면 `F`, 옵션이면 `O` 에
# 거래소를 붙인 값입니다(`FCME`·`OCME`). 종목 마스터에 거래소와 종류가 이미
# 있어 화면이 조립할 수 있고, 백엔드가 종목코드로 마스터를 뒤지면 시세가
# 마스터 적재 상태에 묶입니다.

# 거래소코드 모양. 한 글자(F·O) + 거래소 이름입니다.
_EXCHANGE = r"^[A-Z][A-Z0-9]{1,7}$"


class QuoteSymbol(BaseModel):
    """물어볼 종목 하나. 거래소코드와 종목코드가 함께 있어야 합니다."""

    exch_cd: str = Field(pattern=_EXCHANGE, description="F/O + 거래소. 예 FCME")
    sym: str = Field(min_length=1, max_length=20, description="종목코드")


class PriceRequest(BaseModel):
    """현재가 조회. 화면은 관심종목을 한 번에 묻고, 백엔드가 종목마다 나눠 부릅니다."""

    syms: list[QuoteSymbol] = Field(min_length=1, max_length=api.MAX_SYMBOLS)


@router.post("/price")
def read_price(
    ask: PriceRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """해외 현재가. 물어본 종목 수만큼 옵니다.

    **서버가 한 번에 한 종목만 받으므로** 종목마다 한 번씩, 부른 차례로 불러
    합칩니다(호출 제한은 호출부가 지킵니다). 실패한 종목은 `quotes` 에서 빠지고
    `failed` 에 문구와 함께 실립니다 — 전부 실패하면 요청 자체가 실패한 것으로
    답합니다. 0줄로 답한 종목은 어느 쪽에도 없으니 화면은 물은 목록과 대조합니다.

    저장본으로 돌 때의 헤더는 종목마다의 표시를 모아 **한 번만** 찍습니다 —
    호출마다 찍으면 마지막 종목의 것만 남아 앞 종목의 줄과 어긋납니다.
    """
    made: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    notes: list[dict[str, str]] = []
    last_failure: HTTPException | None = None
    for one in ask.syms:
        try:
            raw = _call(
                partial(api.price, [(one.exch_cd, one.sym)], api=api_client), kind="quote"
            )
        except HTTPException as exc:
            failed.append({"code": one.sym, "message": str(exc.detail)})
            last_failure = exc
            continue
        note = last_replay.get()
        if note:
            notes.append(note)
        made.extend(overseas_quotes.quotes_of(raw, asked=[one.sym])["quotes"])
    if last_failure is not None and not made:
        raise last_failure
    # 잔존일은 현재가 응답에 없어, **이미 받아 둔 만기 조회**에서만 채웁니다
    # (`overseas_products.days_left`). 받아 둔 것이 없으면 `null` 그대로입니다.
    today = now_kst().date()
    for one in made:
        if one.get("daysLeft") is None:
            one["daysLeft"] = overseas_products.days_left(str(one.get("code") or ""), today)
    _stamp(response, _merged_note(notes) if notes else None)
    return {"quotes": made, "failed": failed}


@router.post("/option-chain")
def read_option_chain(
    ask: QuoteSymbol,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """옵션 체인 — 행사가마다 콜·풋 한 줄, 실제 종목코드와 함께.

    `sym` 은 행사가 없는 시리즈 코드(`O_ESU26`), `exch_cd` 는 `O` + 거래소(`OCME`)
    입니다. 한 번에 다 오므로 시세 호출 한 건입니다. 모양은 `overseas_chain.py`
    첫머리에 있습니다. `asOf` 는 이 응답을 만든 한국시간입니다.
    """
    raw = _call(
        lambda: api.price_all_option(ask.sym, exch_cd=ask.exch_cd, api=api_client),
        response,
        kind="quote",
    )
    made = overseas_chain.chain(raw, asked=ask.sym)
    made["asOf"] = iso(now_kst())
    return made


@router.post("/bid-price")
def read_bid_price(
    ask: QuoteSymbol,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """해외 호가. 다섯 단계입니다(국내는 열 단계).

    `asks` 는 먼 값부터, `bids` 는 가까운 값부터 옵니다. **값이 안 채워진
    단계는 빠지므로 두 배열 다 비어 올 수 있습니다.**
    """
    raw = _call(
        lambda: api.bid_price(ask.sym, exch_cd=ask.exch_cd, api=api_client),
        response,
        kind="quote",
    )
    return overseas_quotes.book(raw, asked=ask.sym)


class DailyChartRequest(QuoteSymbol):
    """차트(일) 조회. 날짜는 `YYYYMMDD` 여덟 자리입니다."""

    inq_strt_dt: str = Field(pattern=r"^\d{8}$")
    inq_ed_dt: str = Field(pattern=r"^\d{8}$")
    nxt_key: str = api.FIRST_PAGE


class MinuteChartRequest(DailyChartRequest):
    """차트(분) 조회. 일봉 인자에 시각과 봉 분이 더 있습니다."""

    strt_tm: str = Field(default="000000", pattern=r"^\d{6}$")
    ed_tm: str = Field(default="235959", pattern=r"^\d{6}$")
    # 몇 분을 한 봉으로 묶을지. 1이면 1분봉입니다.
    dcnt: str = Field(default="1", pattern=r"^\d{1,4}$")


#: 해외는 세션이 하나입니다. 국내 답과 칸을 맞추려고 목록으로 냅니다.
SESSION = "day"

#: 한 장에 걸을 쪽 수. 한 영업일이 세 쪽쯤이라 두 배로 둡니다.
#:
#: 상한을 두는 까닭은 **거래가 드문 종목**입니다. 한 쪽이 몇 달치를 담고 있으면
#: 영업일 경계가 첫 쪽에서 나오지만, 반대로 종일 거래가 없으면 경계가 한참
#: 뒤에 있습니다. 그때 끝까지 걸으면 한 번 누른 화면이 조회를 수십 건 냅니다.
MAX_PAGES = 6


class HistoryRequest(QuoteSymbol):
    """분봉을 **영업일 한 칸**씩 받는 요청.

    `before` 는 앞 답이 준 값을 그대로 다시 넣는 자리입니다 — 비우면 가장 최근
    영업일입니다. **속을 볼 것이 없습니다**(어디서부터 걸을지와 어느 영업일까지
    냈는지를 서버가 적어 둔 값입니다).
    """

    dcnt: str = Field(default="1", pattern=r"^\d{1,4}$")
    before: str = Field(
        default="",
        max_length=64,
        pattern=r"^(\d*@\d{0,8})?$",
        description="앞 답이 준 값을 그대로",
    )


@router.post("/chart-history")
def read_chart_history(
    ask: HistoryRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """분봉 한 **영업일**. 이어받기 키로 뒤로 걸으며 모읍니다.

    **날짜로는 과거를 못 부릅니다** — 해외 분봉은 날짜 인자를 안 보고 늘 가장
    최근 쪽을 냅니다. 까닭과 예는 `overseas_history.py` 첫머리에 있습니다.

    영업일은 줄마다 실려 옵니다(`occurs1_glb_biz_dt`). 거래소별 시간표가 없어도
    되는 자리입니다 — 모으는 영업일보다 **오랜 줄이 나오면** 그 영업일은 다
    찬 것입니다.

    `before` 를 비우면 **가장 최근 영업일**입니다. 답의 `before` 를 그대로 다시
    보내면 한 칸 더 과거로 갑니다.

    `partial` 은 **이 영업일을 다 받았다고 보증하지 못한다**는 뜻입니다. 둘 중
    하나면 참입니다 — 걷다가 조회가 두 번 다 실패했거나, 키가 더 안 나가거나
    (빈 값·같은 값). **창의 아래끝(키가 전부 9)은 탈이 아닙니다** — 더 과거가
    없다는 뜻이라 `before` 를 비워 화면이 멈추게 합니다.

    **쪽 상한(`MAX_PAGES`)에 닿는 것은 탈이 아닙니다.** 받은 만큼 내고 `before`
    가 **같은 영업일**을 이어 가리킵니다 — 다음 장이 같은 `businessDay` 로 옵니다.
    화면은 같은 영업일이 또 오면 이어 붙입니다(겹치는 봉은 시각으로 가립니다).
    국내는 한 장이 곧 한 영업일이라 이런 일이 없습니다.

    **첫 조회가 거절되면 그대로 올립니다.** 빈 답으로 감추면 화면이 「빈
    영업일」로 읽고 더 뒤로 갑니다 — 못 받는 거래소를 그렇게 지나치면 사용자는
    까닭을 영영 못 봅니다.

    `nextKey` 는 늘 빈 값입니다. 이어받기 키는 `before` 안에 있습니다 — 화면이
    들고 다니는 값을 국내와 같은 하나로 두려는 것입니다.
    """
    cursor, served = history.as_cursor(ask.before)
    # **보내는 값으로 맞춰 둡니다.** 빈 커서는 첫 쪽을 뜻하는 값으로 나가는데,
    # 그 값과 답의 키를 견줘야 「더 안 나간다」를 제대로 읽습니다.
    cursor = cursor or api.FIRST_PAGE
    walked: list[dict[str, Any]] = []
    day: date | None = None
    edge = cursor
    partial = False
    # 창의 아래끝까지 갔는지. 갔으면 다음 장이 없습니다.
    ended = False
    # 이 영업일을 **다 모았는지**. 다 모았으면 다음 장은 더 오랜 영업일로 가고,
    # 아니면 같은 영업일을 이어서 받습니다.
    done = False

    for turn in range(MAX_PAGES):
        raw = _history_piece(ask, cursor, response, api_client, first=turn == 0)
        if raw is None:
            partial = True
            break
        walked.append(raw)
        if day is None:
            # 이번에 모을 영업일. 첫 장이면 가장 새것, 이어 걷는 중이면 **이미
            # 낸 것보다 한 칸 오랜 것**입니다.
            day = history.before_in([raw], served) if served else history.latest_in(raw)
        if day is not None and history.has_older(raw, day):
            # 그 앞 영업일이 보입니다 — 다 찼습니다. 다음 장은 **이 쪽부터**
            # 다시 걸어야 여기 함께 있던 앞 영업일 줄을 안 잃습니다.
            edge, done = cursor, True
            break
        key = history.next_key_of(raw)
        if history.at_end(key):
            # **창의 아래끝입니다.** 더 과거가 없다는 뜻이라 탈이 아닙니다 —
            # 다음 장을 가리키지 않아 화면이 거기서 멈춥니다.
            edge, done, ended = cursor, True, True
            break
        if not key or key == cursor:
            # 더 못 갑니다. 다 찼는지는 알 수 없으니 그렇다고 적되, 커서는 **다
            # 낸 것으로** 넘깁니다 — 같은 자리를 이어서 부르면 같은 답이 오고
            # 화면이 제자리를 돕니다.
            edge, done = cursor, True
            partial = day is not None
            break
        cursor = key
    else:
        # 쪽 상한입니다. **탈이 아닙니다** — 받은 만큼 내고 다음 장이 같은
        # 영업일을 이어서 받습니다(`cursor` 는 아직 안 부른 쪽을 가리킵니다).
        edge = cursor

    pieces = (
        [quotes.bars(history.pick(one, day), minute=True, asked=ask.sym) for one in walked]
        if day is not None
        else []
    )
    # 한 조각도 못 받았으면 빈 벌입니다. 칸은 성한 답과 같게 둡니다.
    made = quotes.merge_bars(pieces, sessions=[SESSION]) if pieces else {}
    return {
        "code": made.get("code") or ask.sym,
        "bars": made.get("bars") or [],
        # 국내는 야간과 주간 둘인데 해외는 한 세션입니다. 칸은 같은 모양으로 둡니다.
        "sessions": [SESSION],
        "skipped": int(made.get("skipped") or 0),
        "businessDay": history.as_text(day) if day is not None else "",
        # 화면이 한 칸 더 과거로 갈 때 그대로 다시 실어 보내는 값입니다.
        "before": (
            ""
            if ended or day is None
            else history.as_before(edge, day if done else None)
        ),
        "partial": partial,
        "nextKey": "",
    }


def _history_piece(
    ask: HistoryRequest,
    cursor: str,
    response: Response,
    api_client: TradeClient,
    *,
    first: bool,
) -> dict[str, Any] | None:
    """쪽 하나. **못 받으면 `None`** 입니다 — 걷던 쪽이 받은 만큼 냅니다.

    **첫 쪽은 다릅니다.** 한 줄도 못 받은 채 빈 답을 내면 화면이 「빈 영업일」로
    읽고 더 뒤로 가므로, 거절이든 끊김이든 그대로 올립니다.

    **시작 날짜는 창의 아래끝입니다**(`overseas_history.py` 첫머리). 오늘로 두면
    오늘 00:00 에서 걷기가 끝나 앞 영업일로 못 갑니다 — `WINDOW_DAYS` 만큼
    과거를 아래끝으로 둡니다.
    """
    today = now_kst().date()
    floor = history.as_text(today - timedelta(days=history.WINDOW_DAYS))
    ceiling = history.as_text(today)

    def once() -> dict[str, Any]:
        return _call(
            lambda: api.chart_minute(
                ask.sym,
                exch_cd=ask.exch_cd,
                inq_strt_dt=floor,
                inq_ed_dt=ceiling,
                dcnt=ask.dcnt,
                next_key=cursor,
                api=api_client,
            ),
            response,
            kind="quote",
        )

    try:
        return once()
    except HTTPException as exc:
        logger.warning("해외 분봉 %s 를 받지 못했습니다: %s", ask.sym, exc.detail)
    try:
        # 한 번 죽었다고 그 영업일을 통째로 버리지 않습니다.
        return once()
    except HTTPException:
        if first:
            raise
        return None


@router.post("/chart-daily")
def read_chart_daily(
    ask: DailyChartRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """해외 차트(일). 봉은 오래된 것부터, 시각은 그날 0시(한국시간)입니다.

    응답의 종목 칸이 비어 오는 기록이 있어 `code` 는 부를 때 쓴 종목코드로
    채워집니다 — 분봉과 같습니다.
    """
    raw = _call(
        lambda: api.chart_daily(
            ask.sym,
            exch_cd=ask.exch_cd,
            inq_strt_dt=ask.inq_strt_dt,
            inq_ed_dt=ask.inq_ed_dt,
            next_key=ask.nxt_key,
            api=api_client,
        ),
        response,
        kind="quote",
    )
    return quotes.bars(raw, minute=False, asked=ask.sym)


@router.post("/chart-minute")
def read_chart_minute(
    ask: MinuteChartRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """해외 차트(분). 봉은 오래된 것부터 옵니다.

    **응답의 종목 칸이 비어 옵니다.** 그래서 `code` 는 부를 때 쓴 종목코드로
    채워집니다.
    """
    raw = _call(
        lambda: api.chart_minute(
            ask.sym,
            exch_cd=ask.exch_cd,
            inq_strt_dt=ask.inq_strt_dt,
            inq_ed_dt=ask.inq_ed_dt,
            strt_tm=ask.strt_tm,
            ed_tm=ask.ed_tm,
            dcnt=ask.dcnt,
            next_key=ask.nxt_key,
            api=api_client,
        ),
        response,
        kind="quote",
    )
    return quotes.bars(raw, minute=True, asked=ask.sym)


class DepositRequest(BaseModel):
    """해외 예수금 조회.

    **통화와 환산구분이 둘 다 필요합니다.** 기본은 합쳐 보기(`2`)이고 원화
    기준입니다. 통화를 나눠 보려면 `cvs_div="1"` 과 그 통화 코드를 함께.
    """

    # 명세가 char 3 입니다. 통화 코드는 세 글자 대문자입니다.
    cur_cd: str = Field(default=api.HOME_CURRENCY, pattern=r"^[A-Z]{3}$")
    cvs_div: str = Field(default=api.CONVERTED, pattern=r"^[12]$")
    # 명세상 선택입니다. 빈 값이면 안 보냅니다.
    biz_dt: str = Field(default="", pattern=r"^(\d{8})?$")

    @model_validator(mode="after")
    def _currency_for_split(self) -> "DepositRequest":
        # 통화별로 보겠다면서 통화를 안 주면 기본값(원화)으로 조용히 불려, 고르지
        # 않은 통화의 값이 옵니다. 원화를 적어 보내면 원화 예수금만 보는 뜻이라 받습니다.
        if self.cvs_div == api.BY_CURRENCY and "cur_cd" not in self.model_fields_set:
            raise ValueError("통화별로 보려면 통화 코드(cur_cd)를 함께 보내 주세요")
        return self


@router.post("/deposit")
def read_deposit(
    ask: DepositRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """해외 예수금.

    `groups` 는 총액·현금·대용·외화 네 칸을 다 가진 항목이고, `items` 는 총액
    하나로만 오는 항목입니다. **국내처럼 한 표로 합치지 않습니다** — 까닭은
    `overseas_account.py` 첫머리에.
    """
    raw = _call(
        lambda: api.deposit(
            cur_cd=ask.cur_cd,
            cvs_div=ask.cvs_div,
            biz_dt=ask.biz_dt,
            api=api_client,
        ),
        response,
    )
    made = overseas_account.deposit(raw, cur_cd=ask.cur_cd, cvs_div=ask.cvs_div)
    _keep_deposit(api_client, ask, raw, made)
    return made


def _keep_deposit(
    api_client: TradeClient, ask: DepositRequest, raw: dict[str, Any], made: dict[str, Any]
) -> None:
    """그날의 예수금을 쌓아 둡니다. **한 기준만 쌓습니다.**

    통화와 환산 기준마다 쌓으면 빠진 날을 채우는 조회가 기준 수만큼 늘어납니다.
    화면 머리의 「오늘 예탁자산」과 같은 기준(원화 환산 합계)만 모으고, 다른
    기준으로 부른 답은 그냥 답만 냅니다(`deposit_history.BASIS`).
    """
    if ask.cur_cd != api.HOME_CURRENCY or ask.cvs_div != api.CONVERTED:
        return
    environment = orders.environment_of(api_client)
    day = (
        str(raw.get("biz_dt") or "").strip()
        or ask.biz_dt
        or deposit_history.as_text(now_kst().date())
    )
    if not environment or deposit_history.as_day(day) is None:
        return
    kept = deposit_history.store
    if deposit_history.is_empty(made):
        kept.keep_empty(environment=environment, market="overseas", day=day)
        return
    kept.keep(environment=environment, market="overseas", day=day, row=made)


@router.get("/deposit-history")
def read_deposit_history(
    first: str = Query(alias="from", pattern=r"^\d{8}$", description="시작 영업일"),
    last: str = Query(alias="to", pattern=r"^\d{8}$", description="끝 영업일"),
) -> dict[str, Any]:
    """쌓아 둔 날짜별 예수금과 **아직 안 받은 날들**. 국내와 같은 모양입니다.

    쌓는 기준은 **원화 환산 합계** 하나입니다 — 통화를 나눠 부른 답은 추이에
    안 담깁니다. 채우는 길은 `POST /api/overseas/deposit` 입니다.

    `refresh` 는 기간 안의 최근 두 영업일(오늘 포함)입니다. 쌓여 있어도 다시
    물어 덮을 날입니다 — 정산 전에 물으면 앞 영업일 값이 그날 날짜로 올 때가
    있습니다.
    """
    made = deposit_history.answer(
        first, last, market="overseas", environment=str(nh_runtime.active_environment() or "")
    )
    if made is None:
        raise HTTPException(status_code=422, detail=deposit_history.BAD_PERIOD)
    return made


class PageRequest(BaseModel):
    """이어 받기만 받는 요청. 처음에는 빈 값입니다."""

    nxt_key: str = ""


@router.post("/open-interest")
def read_open_interest(
    ask: PageRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """해외 잔고내역(보유 포지션).

    **종목명이 응답에 없습니다** — 화면이 종목코드로 마스터에서 찾습니다.
    가격은 현재가이고, 만기와 통화는 응답에 실려 옵니다.
    """
    raw = _call(lambda: api.open_interest(next_key=ask.nxt_key, api=api_client), response)
    return overseas_account.positions(raw)


class ExpiryPositionsRequest(PageRequest):
    """해외 보유포지션 만기 조회. 셋 다 선택입니다.

    `und_sym` 은 기초자산 품목코드(비우면 전체), `inq_dy_cnt` 는 잔존일 상한(비우면
    서버 기본 2000일)입니다. `nxt_key` 를 비우면 첫 쪽입니다.
    """

    und_sym: str = Field(default="", pattern=r"^[A-Za-z0-9]{0,10}$")
    inq_dy_cnt: str = Field(default="", pattern=r"^(\d{1,6})?$")


@router.post("/open-interest-expiry")
def read_open_interest_expiry(
    ask: ExpiryPositionsRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """해외 보유포지션 만기 조회 — 보유 포지션마다 최초통보일·만기일·잔존일."""
    raw = _call(
        lambda: api.open_interest_expiry(
            und_sym=ask.und_sym,
            inq_dy_cnt=ask.inq_dy_cnt,
            next_key=ask.nxt_key or api.EXPIRY_FIRST_PAGE,
            api=api_client,
        ),
        response,
    )
    return overseas_account.expiry_positions(raw)


class OrderHistoryRequest(PageRequest):
    """해외 주문내역 조회.

    **미체결도 이 주소로 받습니다.** `inq_div` 로 고릅니다 — `0` 전체, `1` 주문,
    `2` 체결, `3` 미체결, `4` 미체결(지정청산). 국내와 값 체계가 다릅니다.
    """

    inq_div: str = Field(default=api.ALL, pattern=r"^[0-4]$")
    trd_div_cd: str = Field(default=api.BOTH_SIDES, pattern=r"^[0-2]$")
    # 품목코드. 명세상 선택(char 10)이고, 빈 값이면 안 보냅니다.
    prd_cd: str = Field(default="", pattern=r"^[A-Za-z0-9]{0,10}$")


@router.post("/order-history")
def read_order_history(
    ask: OrderHistoryRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """해외 주문내역과 미체결.

    줄 하나가 주문 하나이고 진행 상태가 함께 들어 있습니다. 화면의 두 표가
    같은 목록을 다르게 읽습니다.
    """
    raw = _call(
        lambda: api.order_history(
            inq_div=ask.inq_div,
            trd_div_cd=ask.trd_div_cd,
            prd_cd=ask.prd_cd,
            next_key=ask.nxt_key,
            api=api_client,
        ),
        response,
    )
    return overseas_account.order_history(raw)


class OrderableRequest(BaseModel):
    """해외 주문가능수량 조회.

    **NH선물은 방향을 안 물어봅니다** — 한 응답에 매수·매도가 다 옵니다.
    그래도 `side` 를 받는 것은 화면이 한 방향만 쓰기 때문입니다. 국내와 같은
    모양으로 내기 위해 물어본 쪽만 골라 냅니다.

    가격을 **문자열로 받습니다** — 화면에 보이는 글자를 그대로 보내기
    위해서입니다. 시장가에도 가격이 필요합니다(명세상 필수).

    STOP 두 유형(`3`·`4`)은 스탑가격이 함께 있어야 합니다.
    """

    sym: str = Field(min_length=1, description="종목 표준 코드")
    side: str = Field(pattern=r"^(buy|sell)$", description="화면 낱말. 매수 buy, 매도 sell")
    glb_ord_tp_cd: str = Field(
        pattern=r"^[1-4]$", description="1 시장가, 2 지정가, 3 STOP시장가, 4 STOP지정가"
    )
    ord_pric: str = Field(pattern=r"^-?\d{1,15}(\.\d{1,8})?$", description="주문가격")
    stop_pric: str = Field(default="", pattern=r"^(-?\d{1,15}(\.\d{1,8})?)?$")

    @model_validator(mode="after")
    def _stop_needs_price(self) -> "OrderableRequest":
        if self.glb_ord_tp_cd in api.STOP_TYPES and not self.stop_pric:
            raise ValueError("STOP 주문유형은 스탑가격이 필요합니다")
        return self


@router.post("/orderable-quantity")
def read_orderable_quantity(
    ask: OrderableRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """해외 주문가능수량.

    응답에 온 양쪽 가운데 물어본 방향만 냅니다 — 국내와 같은 낱말입니다.
    """
    raw = _call(
        lambda: api.orderable_quantity(
            ask.sym,
            glb_ord_tp_cd=ask.glb_ord_tp_cd,
            ord_pric=ask.ord_pric,
            stop_pric=ask.stop_pric,
            api=api_client,
        ),
        response,
    )
    return overseas_account.orderable_quantity(
        raw, code=ask.sym, side=ask.side, product_code=_product_code(ask.sym)
    )


def _product_code(sym: str) -> str | None:
    """그 종목의 품목코드 — 종목 마스터에서. 마스터가 모르면 `None` 입니다."""
    try:
        row = master_service.service.product_of(session_service.OVERSEAS, sym)
    except Exception:  # noqa: BLE001 — 품목을 못 찾았다고 수량 조회를 막지 않습니다.
        logger.warning("종목 %s 의 품목을 마스터에서 읽지 못했습니다.", sym, exc_info=True)
        return None
    if not row:
        return None
    return str(row.get("product_code") or "").strip() or None


# ── 주문오류·누적손익 ───────────────────────────────────────


class OrderErrorRequest(BaseModel):
    """주문오류내역 조회. 어느 날의 것인지 부르는 쪽이 정합니다.

    **비우면 오늘(한국시간)로 봅니다** — 화면이 날짜를 지어내지 않게 하려는
    것입니다. 국내 예수금과 같은 규칙입니다.
    """

    inq_dt: str = Field(default="", pattern=r"^(\d{8})?$", description="조회일자 YYYYMMDD")
    nxt_key: str = ""
    # 기간으로 물을 때. 둘 다 비우면 위의 하루 조회입니다.
    inq_strt_dt: str = Field(default="", pattern=r"^(\d{8})?$", description="기간 시작일 YYYYMMDD")
    inq_ed_dt: str = Field(default="", pattern=r"^(\d{8})?$", description="기간 끝날 YYYYMMDD")

    @field_validator("inq_strt_dt", "inq_ed_dt")
    @classmethod
    def _real_date(cls, value: str) -> str:
        # 자리 수만 맞는 `20261345` 는 서버에 보내기 전에 여기서 거릅니다.
        if value and market_values.date(value) is None:
            raise ValueError("있는 날짜여야 합니다")
        return value

    @model_validator(mode="after")
    def _period_shape(self):
        problem = order_error_days.period_problem(
            self.inq_dt, self.nxt_key, self.inq_strt_dt, self.inq_ed_dt
        )
        if problem:
            raise ValueError(problem)
        if self.inq_strt_dt:
            # 끝날이 오늘보다 뒤면 오늘로 자릅니다. 자른 기간으로 길이를 봅니다.
            order_error_days.resolve(self.inq_strt_dt, self.inq_ed_dt, now_kst().date())
        return self

    @property
    def period(self) -> bool:
        return bool(self.inq_strt_dt)

    @property
    def date(self) -> str:
        return self.inq_dt or now_kst().strftime("%Y%m%d")


@router.post("/order-error")
def read_order_error(
    ask: OrderErrorRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """해외 주문오류내역.

    국내와 달리 **줄에 일련번호가 있어** 목록 열쇠(`id`)를 낼 수 있습니다.
    구분 값들은 **코드 없이 이름만** 옵니다 — 까닭은 `overseas_account.py` 에.

    **기간으로도 물을 수 있습니다**(`inq_strt_dt`·`inq_ed_dt`, 31일까지) — 날마다
    차례로 불러 합칩니다(`backend/order_error_days.py`). 한 줄이 두 날의 조회에 함께
    걸려 올 수 있어 (주문일자, 일련번호)가 같은 줄은 한 번만 남깁니다.
    """
    if ask.period:
        return _order_error_period(ask, response, api_client)
    raw = _call(
        lambda: api.order_error(ask.date, next_key=ask.nxt_key, api=api_client),
        response,
    )
    return overseas_account.order_errors(raw)


def _order_error_period(
    ask: OrderErrorRequest, response: Response, api_client: TradeClient
) -> dict[str, Any]:
    first, last = order_error_days.resolve(ask.inq_strt_dt, ask.inq_ed_dt, now_kst().date())
    made = order_error_days.collect(
        first,
        last,
        lambda day, key: _call(
            lambda: api.order_error(day.strftime("%Y%m%d"), next_key=key, api=api_client),
            response,
        ),
        shape=lambda raw: overseas_account.order_errors(raw)["errors"],
        more=lambda raw: True,
    )
    body = order_error_days.answer(
        made,
        first,
        last,
        response,
        # 줄의 국내 일자가 먼저이고, 없으면 물은 날입니다.
        date_of=lambda one, day: one.get("date") or day.isoformat(),
        key_of=lambda one: (
            (str(one["orderDate"]), str(one["id"]))
            if one.get("orderDate") and one.get("id")
            else None
        ),
    )
    body["count"] = len(body["errors"])
    return body


# 누적손익의 기본 조회 기간(일). 끝날은 오늘이고 시작일은 이만큼 앞입니다.
CUMULATIVE_PL_DAYS = 30


class CumulativePlRequest(BaseModel):
    """누적손익 조회.

    `cvs_cur_cd` 는 **표시할 통화**(원화 또는 달러)이고, `cur_cd` 는 어느 통화의
    손익만 볼지 거르는 값입니다. 둘은 다른 자리입니다.

    **기간을 비우면 라우트가 채웁니다** — 끝날은 오늘(한국시간), 시작일은 그
    30일 전. 서버가 기간 없는 요청을 거절하므로 화면이 기간을 고르기 전에도
    답이 오게 합니다. 실제로 쓴 기간은 응답의 `period` 로 돌려줍니다.
    """

    inq_strt_dt: str = Field(default="", pattern=r"^(\d{8})?$")
    inq_ed_dt: str = Field(default="", pattern=r"^(\d{8})?$")
    cvs_cur_cd: str = Field(default=api.CONVERT_KRW, pattern=r"^(KRW|USD)$")
    cur_cd: str = Field(default="", pattern=r"^([A-Z]{3})?$")
    prd_cd: str = Field(default="", pattern=r"^[A-Za-z0-9]{0,10}$")
    nxt_key: str = ""

    @field_validator("inq_strt_dt", "inq_ed_dt")
    @classmethod
    def _real_date(cls, value: str) -> str:
        # 자리 수만 맞는 `20261345` 는 서버에 보내기 전에 여기서 거릅니다.
        if value and market_values.date(value) is None:
            raise ValueError("있는 날짜여야 합니다")
        return value

    @property
    def last_day(self) -> str:
        return self.inq_ed_dt or now_kst().strftime("%Y%m%d")

    @property
    def first_day(self) -> str:
        if self.inq_strt_dt:
            return self.inq_strt_dt
        # 끝날이 주어졌으면 그날부터, 아니면 오늘부터 거슬러 셉니다.
        end = datetime.strptime(self.last_day, "%Y%m%d")
        return (end - timedelta(days=CUMULATIVE_PL_DAYS)).strftime("%Y%m%d")

    @model_validator(mode="after")
    def _period_in_order(self) -> "CumulativePlRequest":
        # 거꾸로 된 기간은 서버가 거절만 하므로 여기서 이유를 말해 줍니다.
        if self.first_day > self.last_day:
            raise ValueError("시작일이 끝날보다 뒤입니다")
        return self


@router.post("/cumulative-pl")
def read_cumulative_pl(
    ask: CumulativePlRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """해외 누적손익.

    줄 하나가 품목 × 통화이고, **총합 줄은 목록에서 빼서 `total` 로** 냅니다 —
    두면 화면이 합을 다시 더합니다. `period` 는 실제로 물은 기간입니다 — 화면이
    기간을 안 골랐을 때 어느 기간의 값인지 보일 수 있게.

    **다음 쪽이 있으면 끝까지 이어 받아 한 답으로** 냅니다(`overseas_account.collect_pl`).
    총합이 마지막 쪽에만 오므로, 첫 쪽만 내면 총합 없는 일부가 됩니다. 어느 쪽에서든
    실패하면 전체를 실패로 답합니다. 끝까지 받지 못했으면 `truncated` 가 참입니다.

    `conversionCurrency` 는 **실제로 보낸 환산통화**입니다. 합계는 이 통화로 옵니다.
    """
    first_day, last_day = ask.first_day, ask.last_day

    def page(key: str) -> dict[str, Any]:
        return _call(
            lambda: api.cumulative_pl(
                inq_strt_dt=first_day,
                inq_ed_dt=last_day,
                cvs_cur_cd=ask.cvs_cur_cd,
                cur_cd=ask.cur_cd,
                prd_cd=ask.prd_cd,
                next_key=key,
                api=api_client,
            ),
            response,
        )

    pages, truncated = overseas_account.collect_pl(page, first_key=ask.nxt_key)
    made = overseas_account.cumulative_pl(pages, truncated=truncated)
    made["period"] = {
        "from": market_values.date(first_day),
        "to": market_values.date(last_day),
    }
    made["conversionCurrency"] = ask.cvs_cur_cd
    return made


@router.post("/market-operations")
def read_market_operations(
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """장운영정보 — 품목별 거래시간(1차·2차·익일)과 정산가 산정시간, 한국시간.

    **하루 한 번 받아 둔 것을 냅니다.** 서머타임이 바뀌는 날 값이 바뀌므로 날마다
    새로 받되, 요청마다 NH선물에 묻지는 않습니다. 새로 받지 못하면 받아 둔 것을
    그대로 냅니다. 모양은 `overseas_hours.py` 첫머리에 있습니다.
    """
    return _call(
        lambda: session_service.service.table(session_service.OVERSEAS, api=api_client),
        response,
        kind="hours",
    )


class ProductDetailRequest(BaseModel):
    """품목별세부정보 조회. 거르는 인자가 없습니다 — 늘 전체를 받습니다."""

    # 오늘 받아 둔 것이 있어도 다시 받습니다. 화면의 「다시 받기」가 씁니다.
    force: bool = False


@router.post("/product-detail")
def read_product_detail(
    ask: ProductDetailRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """품목별세부정보 — 품목마다 계약단위·틱사이즈·틱값·인수도구분·적용일자.

    **월물이 아니라 품목 단위입니다.** 줄의 `productCode` 는 `6A`·`O_4TF` 처럼
    품목코드이고, 화면은 종목코드에서 품목을 풀어 이 표를 찾습니다.

    **하루 한 번 받아 둔 것을 냅니다.** 계약 규격은 날마다 바뀌는 값이 아니라
    요청마다 NH선물에 묻지 않습니다. `source` 가 방금 받은 것(`api`)인지 받아 둔
    것(`cache`)인지 알려 줍니다. 모양은 `overseas_products.py` 첫머리에 있습니다.

    표는 실전 서버에서 받고, 어느 서버에서 받았는지는 `environment` 에 적습니다
    (`overseas_product_service.py` 첫머리). `x-nh-environment` 헤더는 화면에서 고른
    환경입니다.
    """
    return _call(
        lambda: overseas_product_service.service.products(api=api_client, force=ask.force),
        response,
        kind="product",
    )


#: 종목 하나의 품목 값을 못 채운 까닭. **화면이 그대로 적습니다.** **국내와 같은
#: 문구입니다** — 두 시장의 같은 자리가 다른 말을 하면 화면이 시장마다 다르게
#: 적어야 합니다. 코드가 받아 오는 자료의 이름을 쓰지 않는 까닭은 국내 쪽
#: (`domestic_routes.py`)에 있습니다.
_NO_MASTER = "종목 목록을 아직 받지 못했습니다"
_NO_SYMBOL = "종목 목록에서 이 종목을 찾지 못했습니다"
_NO_PRODUCT = "이 종목이 속한 품목을 품목정보에서 찾지 못했습니다"
_TABLE_FAILED = "품목정보를 받지 못했습니다. 잠시 뒤 다시 시도해 주세요."
_NO_KEYS = "해외 인증 정보가 없어 품목정보를 받지 못했습니다. 설정에서 해외 App Key 를 넣어 주세요."

#: `why` 문장 → 갈래 코드. 국내와 같은 코드를 씁니다 — 까닭은 국내 쪽
#: (`domestic_routes.py` 의 `_WHY_CODES`)에 있습니다.
_WHY_CODES: dict[str, str] = {
    _NO_MASTER: "no_master",
    _NO_SYMBOL: "no_symbol",
    _NO_PRODUCT: "no_product",
    _TABLE_FAILED: "table_failed",
    _NO_KEYS: "no_keys",
}


def _with_why_code(made: dict[str, Any]) -> dict[str, Any]:
    """답에 까닭의 갈래 코드를 답니다. 다 채웠으면 `None` 입니다."""
    made["whyCode"] = _WHY_CODES.get(made.get("why") or "")
    return made



class ProductInfoRequest(BaseModel):
    """종목 하나의 품목 값을 묻는 요청.

    **모르는 칸은 받지 않습니다**(`extra="forbid"`). 이 조회는 거르는 인자가
    종목 하나뿐이라, 딸려 온 칸은 부르는 쪽이 **다른 조회와 헷갈린 것**입니다 —
    조용히 버리면 「인자가 안 먹는다」로 보입니다(품목 표를 종목으로 묻는 경우가
    그렇습니다).
    """

    model_config = ConfigDict(extra="forbid")

    sym: str = Field(min_length=1, max_length=32, description="종목 표준 코드")


@router.post("/product-info")
def read_product_info(
    ask: ProductInfoRequest,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """종목 하나의 품목 값 — 계약단위·통화·인수도구분·적용일자.

    **품목별세부정보는 품목 단위라 종목코드로는 바로 못 찾습니다.** 마스터에서
    그 종목의 품목을 꺼내(`product_of`) 표의 `productCode` 와 잇습니다 — 국내
    `product-info` 와 같은 길입니다.

    **호가단위와 틱값은 안 냅니다.** 종목 마스터가 그 둘을 주고(`tsiz`·`tval`)
    화면이 이미 거기서 읽습니다 — 같은 값을 두 곳에서 내면 어긋나는 날 어느
    쪽이 맞는지 못 가립니다. 계약단위도 **틱값 ÷ 호가단위**로 나오지만 표에 직접
    있는 값을 냅니다. 셈해서 내면 그 나눗셈이
    맞는지 늘 함께 믿어야 합니다.

    못 채우면 값이 비고 `why` 가 까닭을 말합니다 — 「마스터에 없다」와 「표를
    아직 못 받았다」는 사용자가 할 일이 다릅니다.

    `source` 는 품목 표가 방금 받은 것(`api`)인지 받아 둔 것(`cache`)인지입니다.
    """
    made: dict[str, Any] = {
        "code": ask.sym,
        "productCode": None,
        "productName": None,
        "productKind": None,
        "contractUnit": None,
        "currency": None,
        "delivery": None,
        "appliedDate": None,
        "source": None,
        "why": "",
        "whyCode": None,
    }

    if master_service.service.index() is None:
        # **「없다」와 「아직 못 받았다」는 다릅니다.** 앱이 뜨고 마스터를 읽는
        # 동안 모든 종목이 「없다」는 답을 받으면 사용자는 코드를 고치려 듭니다.
        made["why"] = _NO_MASTER
        return _with_why_code(made)
    row = master_service.service.product_of(session_service.OVERSEAS, ask.sym)
    if row is None:
        made["why"] = _NO_SYMBOL
        return _with_why_code(made)
    made["productCode"] = str(row.get("product_code") or "") or None
    made["productKind"] = str(row.get("kind") or "") or None

    try:
        table = overseas_product_service.service.products(api=api_client)
    except MissingMarketKeys as exc:
        # **기다려도 안 되는 자리입니다.** 해외 인증 정보가 어디에도 없는 것이라
        # 「잠시 뒤 다시」가 아니라 무엇을 넣어야 하는지를 말합니다(국내와 같은 결).
        logger.warning("해외 인증 정보가 없어 품목별세부정보를 받지 않습니다: %s", exc)
        made["why"] = _NO_KEYS
        return _with_why_code(made)
    except NHApiError as exc:
        # **못 받아도 이 자리는 안 죽습니다.** 답 모양을 하나로 지키고 `why` 로
        # 말합니다 — 계약단위를 보던 사용자에게 다른 조회의 문구가 서면 엉뚱한
        # 자리를 봅니다.
        logger.warning("해외 품목별세부정보를 받지 못했습니다: %s", exc)
        made["why"] = _TABLE_FAILED
        return _with_why_code(made)

    made["source"] = table.get("source")
    found = next(
        (
            one
            for one in table.get("products") or []
            if str(one.get("productCode") or "") == made["productCode"]
        ),
        None,
    )
    if found is None:
        made["why"] = _NO_PRODUCT
        return _with_why_code(made)
    made["productName"] = found.get("name")
    made["contractUnit"] = found.get("contractUnit")
    made["currency"] = found.get("currency")
    made["delivery"] = found.get("delivery")
    made["appliedDate"] = found.get("appliedDate")
    return _with_why_code(made)


class ExpiryRequest(BaseModel):
    """만기 정보 조회. 종목코드가 필요합니다.

    `rem_dy` 는 잔존일 기준 — 만기까지 남은 날이 이 값 이하인 월물만 옵니다.
    비우면 먼 월물까지 다 오도록 크게 보냅니다.
    """

    sym: str = Field(min_length=1, max_length=32)
    fut_opt_div_cd: str = Field(default="", pattern=r"^[FOSZ]?$")
    rem_dy: str = Field(default=api.REMAINING_DAYS_ALL, pattern=r"^\d{1,5}$")
    nxt_key: str = ""


@router.post("/product-expiry")
def read_product_expiry(
    ask: ExpiryRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """해외 만기 정보 — 종목의 최초통보일·만기일(최종거래일)·최종결제일.

    **날짜만 있고 만기일의 거래시간은 없습니다.** 그날 몇 시에 끝나는지는
    장운영정보(`/market-operations`)의 거래시간으로 봅니다. 없는 종목이면 `rows`
    가 빈 목록입니다. `remainingDaysLimit` 는 실제로 보낸 잔존일 기준입니다.

    **실전 서버에서 받습니다.** 만기일은 시장의 값이라 거래시간표와 같은 규칙을
    따릅니다 — 거래시간표는 모의 서버 값이 실전과 다릅니다. 해외 실전 인증 정보가 없을 때만
    모의 서버에서 받고, 어느 쪽에서 받았는지는 `environment` 에 적습니다.
    `x-nh-environment` 헤더는 화면에서 고른 환경입니다(`backend.nh_api.reference`).
    """

    def fetch() -> tuple[dict[str, Any], str]:
        with reference_environment(api_client, session_service.OVERSEAS) as environment:
            raw = api.product_expiry(
                ask.sym,
                fut_opt_div_cd=ask.fut_opt_div_cd,
                rem_dy=ask.rem_dy,
                next_key=ask.nxt_key,
                api=api_client,
                environment=environment,
            )
        return raw, environment

    raw, environment = _call(fetch, response, kind="product")
    made = overseas_products.expiries(raw, asked=ask.sym, remaining_days_limit=ask.rem_dy)
    made["environment"] = environment
    return made


# ── 주문·정정·취소 ──────────────────────────────────────────
#
# **해외는 셋이 API 하나입니다** — `qt_div_cd` 로 가릅니다. 국내는 주간·야간까지
# 여섯으로 나뉘어 있어 라우트도 셋으로 나눕니다.
#
# 거절이 `err_cd`·`err_msg` 로 옵니다. 공통 판정(`msg_cd`)이 못 잡는 자리라
# 여기서 직접 봅니다.
#
# 요청에는 **화면이 믿는 환경**을 실어야 합니다. 서버의 활성 환경과 다르면
# 보내지 않습니다 — 까닭은 `backend/orders.py` 첫머리에 있습니다.

# 값을 안 받는 주문유형. 시장가·STOP시장가는 주문가격이 `0`,
# 시장가·지정가는 스탑가격이 빈 값입니다(명세).
_ZERO_PRICE = (api.MARKET_PRICE, api.STOP_MARKET)
_NEEDS_ORIGINAL = (api.MODIFY, api.CANCEL)


class OverseasOrderRequest(BaseModel):
    """해외 주문·정정·취소.

    `qt_div_cd` 가 무엇을 할지 정합니다 — `1` 신규, `2` 정정, `3` 취소.
    정정·취소는 원주문번호가 필요합니다.
    """

    environment: str = Field(
        pattern=ENVIRONMENT_PATTERN,
        description="화면이 믿는 환경. 서버의 활성 환경과 다르면 안 보냄",
    )
    sym: str = Field(min_length=1, description="종목 표준 코드")
    side: str = Field(pattern=r"^(buy|sell)$", description="화면 낱말. 매수 buy, 매도 sell")
    qt_div_cd: str = Field(pattern=r"^[1-3]$", description="1 신규, 2 정정, 3 취소")
    glb_ord_tp_cd: str = Field(
        pattern=r"^[1-4]$", description="1 시장가, 2 지정가, 3 STOP시장가, 4 STOP지정가"
    )
    ord_qty: int = Field(gt=0, description="주문수량. 정정·취소는 원주문 총수량 이하")
    ord_pric: str = Field(default="", pattern=r"^(-?\d{1,15}(\.\d{1,8})?)?$")
    stop_pric: str = Field(default="", pattern=r"^(-?\d{1,15}(\.\d{1,8})?)?$")
    org_ord_no: str = ""

    @property
    def price(self) -> str:
        return "0" if self.glb_ord_tp_cd in _ZERO_PRICE else self.ord_pric

    @property
    def stop(self) -> str:
        return self.stop_pric if self.glb_ord_tp_cd in api.STOP_TYPES else ""

    @model_validator(mode="after")
    def _values_match_kind(self) -> "OverseasOrderRequest":
        if self.glb_ord_tp_cd not in _ZERO_PRICE and not self.ord_pric:
            raise ValueError("지정가·STOP지정가 주문은 주문가격이 필요합니다")
        if self.glb_ord_tp_cd in api.STOP_TYPES and not self.stop_pric:
            raise ValueError("STOP 주문유형은 스탑가격이 필요합니다")
        if self.qt_div_cd in _NEEDS_ORIGINAL and not self.org_ord_no.strip():
            raise ValueError("정정·취소는 원주문번호가 필요합니다")
        return self


@router.post("/order")
def place_order(
    ask: OverseasOrderRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """해외 주문·정정·취소. 원주문번호는 여기서 열 자리로 채웁니다.

    화면이 믿는 환경(`environment`)이 서버의 활성 환경과 다르거나, 환경을
    바꾸는 중이거나, 환경을 못 읽으면 **보내지 않습니다.** 실전인지 모의인지로
    막는 것이 아닙니다 — 화면과 서버가 같은 환경을 말할 때만 보내는 것입니다.
    """
    refused = orders.refusal(api_client, ask.environment)
    if refused is not None:
        status, detail = refused
        logger.warning("주문을 내지 않았습니다(%s): %s", status, detail)
        raise HTTPException(
            status_code=status, detail=detail, headers=orders.environment_headers(api_client)
        )

    last_replay.set(None)
    last_environment.set(None)
    original = (
        orders.order_no(ask.org_ord_no) if ask.qt_div_cd in _NEEDS_ORIGINAL else api.NO_ORIGINAL
    )
    try:
        raw = api.order(
            ask.sym,
            qt_div_cd=ask.qt_div_cd,
            trd_div_cd=overseas_account.SIDE_CODE[ask.side],
            glb_ord_tp_cd=ask.glb_ord_tp_cd,
            ord_qty=str(ask.ord_qty),
            ord_pric=ask.price,
            stop_pric=ask.stop,
            org_ord_no=original,
            api=api_client,
        )
    except TransportError as exc:
        if orders.is_replay(api_client):
            logger.info("재생 모드에 그 주문 기록이 없습니다: %s", exc)
            raise HTTPException(status_code=409, detail=orders.REPLAY_REFUSED) from exc
        logger.warning("주문 결과를 받지 못했습니다: %s", exc)
        raise HTTPException(status_code=504, detail=orders.NOT_RECEIVED) from exc
    except BusinessError as exc:
        # 해외 거부는 `err_cd` 로 오는 것을 봤지만, 공통 판정이 보는 `msg_cd`
        # 로 오는 날에도 **화면 모양이 같아야** 합니다. 거절은 거절입니다.
        logger.info("주문이 거절되었습니다: %s", exc.code)
        return orders.rejected(
            code=exc.code, message=exc.detail, environment=orders.environment_of(api_client)
        )
    except GatewayError as exc:
        if paper_unavailable(exc):
            # 모의투자에서는 그 주문 API 가 없다는 답입니다 — 처리되지 않은 것이
            # 확실하므로 「알 수 없음」이 아니라 거절로 냅니다.
            logger.info("모의투자에서 부를 수 없는 주문입니다: %s", exc.code)
            return orders.rejected(
                code=exc.code,
                message=exc.detail,
                kind=error_codes.PAPER_UNAVAILABLE,
                environment=orders.environment_of(api_client),
            )
        # **처리됐는지 모르는 자리입니다.** 게이트웨이가 거절한 것은 「서버가
        # 보고 안 된다고 한 것」이 아니라 「무엇이라 했는지 모르는 것」이라,
        # 「잠시 뒤 다시」로 적으면 이미 나갔을 수 있는 주문을 한 번 더 내라는
        # 말이 됩니다. 응답을 못 받은 것과 같은 결로 냅니다.
        logger.warning("주문 결과를 알 수 없습니다(%s): %s", exc.code, exc)
        raise HTTPException(status_code=504, detail=orders.NOT_RECEIVED) from exc
    except (HttpError, NHApiError) as exc:
        logger.warning("주문을 내지 못했습니다: %s", exc)
        raise HTTPException(status_code=502, detail=orders.REJECTED) from exc
    finally:
        _stamp(response)

    # 한 번만 읽습니다. 접수와 거절이 서로 다른 환경을 적으면 안 됩니다.
    environment = orders.environment_of(api_client)
    error = str(raw.get("err_cd") or "").strip()
    if orders.failed(error):
        logger.info("주문이 거절되었습니다: %s", error)
        return orders.rejected(
            code=error,
            message=str(raw.get("err_msg") or "").strip(),
            environment=environment,
        )

    # **주문번호가 비어 오면 접수라고 답하지 않습니다.** 번호 없는 줄은 뒤이어
    # 오는 알림·조회가 덮을 수가 없어, 화면에 영영 남습니다. 국내와 같은 자리입니다.
    if not str(raw.get("ord_no") or "").strip():
        # 원문을 통째로 남기지 않습니다 — 요청에 실어 보낸 값이 되비쳐 올 수 있습니다.
        logger.warning("접수 답에 주문번호가 없습니다. 받은 칸: %s", sorted(raw))
        raise HTTPException(
            status_code=504,
            detail=orders.NO_ORDER_NUMBER,
            headers=orders.environment_headers(api_client),
        )

    reply = overseas_account.order_result(
        raw,
        code=ask.sym,
        side=ask.side,
        quantity=ask.ord_qty,
        kind=overseas_account.ORDER_KIND[ask.qt_div_cd],
        now=now_kst(),
        environment=environment,
        order_type_code=ask.glb_ord_tp_cd,
        # 신규 주문에는 가리킬 원주문이 없습니다 — 보내는 자리의 `0` 은 명세가
        # 요구하는 채움값이지 주문번호가 아닙니다.
        original_order_no=original if ask.qt_div_cd in _NEEDS_ORIGINAL else "",
    )
    return order_outcome.apply(reply, _outcome(reply["id"], ask, original, api_client))


def _outcome(
    order_id: str, ask: OverseasOrderRequest, original: str, api_client: TradeClient
) -> dict[str, Any]:
    """접수된 주문의 결과를 주문오류내역·주문내역으로 확인합니다(`backend/order_outcome.py`).

    접수 답이 「접수」여도 NH선물이 뒤에 거절할 수 있습니다 — 해외는 그 거절이
    주문오류내역에만 남습니다. 확인하느라 답이 1~3초 늦을 수 있습니다. **저장해 둔
    응답으로 도는 중에는 확인하지 않습니다** — 저장본의 내역은 방금 낸 주문과
    상관이 없습니다.
    """
    if orders.is_replay(api_client):
        return order_outcome.unknown()
    cancelling = ask.qt_div_cd == api.CANCEL
    return order_outcome.settle(
        lambda: order_outcome.look(
            order_outcome.OVERSEAS,
            order_id,
            cancelling=cancelling,
            original_no=original if cancelling else "",
            api=api_client,
        )
    )


def _call(
    action, response: Response | None = None, *, kind: str = "account"
) -> dict[str, Any]:
    """부르고, 실패는 화면용 문구로 바꿉니다.

    거절에 NH선물의 오류 코드가 실려 오면 문장에 코드와 원문을 함께 싣고 헤더로도
    답니다(`orders.lookup_refusal`). 코드가 없는 실패는 화면용 문구만 내고 원문은
    기록에 남깁니다.

    저장본으로 돌 때는 무엇으로 답했는지를 응답 헤더에 실어 보냅니다 —
    국내 라우트와 같은 규칙입니다. `kind` 는 무슨 조회인지(`_FAILURES` 의 열쇠)이고
    실패 문구가 갈립니다.
    """
    not_received, rejected, what = _FAILURES[kind]
    last_replay.set(None)
    last_environment.set(None)
    try:
        return action()
    except MissingMarketKeys as exc:
        # 시장의 참고 자료(장운영정보·품목별세부정보·만기일)를 받을 인증 정보가 어느
        # 환경에도 없습니다. 기다려도 안 되는 자리라 무엇을 넣으면 되는지를 답합니다.
        logger.info("해외 %s 조회를 하지 않습니다: %s", what, exc)
        raise HTTPException(status_code=MISSING_KEYS_STATUS, detail=str(exc)) from exc
    except TransportError as exc:
        logger.warning("해외 %s 조회 실패: %s", what, exc)
        raise HTTPException(status_code=504, detail=not_received) from exc
    except (HttpError, BusinessError) as exc:
        logger.warning("해외 %s 조회 거절: %s", what, exc)
        if orders.quote_subscription(exc):
            # 그 거래소는 시세 사용 신청이 먼저입니다. 무엇을 하면 되는지 알립니다.
            raise orders.quote_subscription_refusal(exc) from exc
        raise orders.lookup_refusal(exc, what, rejected) from exc
    except NHApiError as exc:
        if paper_unavailable(exc):
            # 모의투자에서는 NH선물이 이 조회를 제공하지 않습니다(`EGW053`).
            logger.info("해외 %s 는 모의투자에서 부를 수 없습니다: %s", what, exc)
            raise orders.paper_refusal(exc) from exc
        logger.warning("해외 %s 응답을 다루지 못했습니다: %s", what, exc)
        raise orders.lookup_refusal(exc, what, rejected) from exc
    finally:
        _stamp(response)


def _stamp(response: Response | None, note: dict[str, str] | None = None) -> None:
    """어느 환경의 값인지, 저장본으로 답했으면 그 사실을 헤더로 남깁니다.

    `note` 를 주면 그것을 찍고, 아니면 방금 부른 호출의 표시를 찍습니다.
    """
    if response is None:
        return
    nh_runtime.stamp_environment(response)
    note = note or last_replay.get()
    if not note:
        return
    response.headers[MATCH_HEADER] = note["match"]
    response.headers[SYMBOL_HEADER] = note["symbol"]
    if note["record"]:
        response.headers[RECORD_HEADER] = note["record"]


# 재생 단계를 값이 실제와 가까운 차례로. 뒤로 갈수록 약합니다.
_MATCH_ORDER = (MATCH_EXACT, MATCH_API, MATCH_CROSS_ENVIRONMENT, MATCH_SYNTHETIC)


def _merged_note(notes: list[dict[str, str]]) -> dict[str, str]:
    """종목마다 따로 부른 재생 표시를 하나로.

    기록 번호는 쓴 차례로 `+` 로 잇고, 단계는 쓴 것 중 **가장 약한 것**입니다 —
    한 줄이라도 다른 환경 기록이면 응답 전체가 그렇다고 알립니다. 전부 빈
    응답이면 `empty` 입니다. 여러 종목을 한 기록으로 답하던 때와 같은 모양입니다.
    """
    found = [note for note in notes if note["match"] != MATCH_EMPTY]
    if not found:
        return {"match": MATCH_EMPTY, "symbol": SYMBOL_NONE, "record": ""}

    def strength(match: str) -> int:
        return _MATCH_ORDER.index(match) if match in _MATCH_ORDER else len(_MATCH_ORDER)

    weakest = max((note["match"] for note in found), key=strength)
    records = "+".join(dict.fromkeys(note["record"] for note in found if note["record"]))
    return {"match": weakest, "symbol": SYMBOL_SAME, "record": records}
