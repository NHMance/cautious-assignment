"""해외 품목 조회 응답 둘을 화면 모양으로 바꿉니다 — 만기일 조회와 품목별세부정보.

**만기일 조회**(`expiries`)는 줄 하나가 종목(월물)이고 날짜 셋이 옵니다 —
최초통보일·만기일(최종거래일)·최종결제일. **날짜만 있고 만기일의 거래시간은
없습니다** — 그날 몇 시에 거래가 끝나는지는 장운영정보의 거래시간
(`overseas_hours`)으로 봐야 합니다.

옵션은 종목 칸이 행사가 없는 시리즈(`O_ESU26`)로 옵니다 — 물은 코드와 다르므로
응답 첫머리에 물은 코드를 따로 둡니다.

**품목별세부정보**(`details`)는 줄 하나가 월물이 아니라 **품목**입니다. 계약단위·
틱사이즈·틱값이 여기 있고, 그 셋이 있어야 화면이 손익을 셈할 수 있습니다. 숫자는
자리를 0 으로 채운 문자열로 오므로(`00000000000100000.00000000`) 숫자로 바꿔 냅니다.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any

from backend import market_values
from backend.overseas_futureoption.overseas_hours import kind_of

# 이 자리로 오는 것은 전부 해외입니다.
MARKET = "overseas"

#: 인수도구분. 만기에 현금으로 셈하는지 실물을 주고받는지입니다.
DELIVERY = {"1": "cash", "2": "physical"}


def expiries(raw: dict[str, Any], *, asked: str, remaining_days_limit: str) -> dict[str, Any]:
    """만기일 응답을 화면 모양으로.

    `code` 는 부를 때 쓴 종목코드, `rows` 는 온 줄 전부입니다(없는 종목이면 빈
    목록). `remainingDaysLimit` 는 **실제로 보낸 잔존일 기준**입니다 — 서버가 그
    안의 월물만 냈으므로, 화면이 「N일 안 월물만」이라고 말할 수 있게 돌려줍니다.
    """
    rows = raw.get("Occurs1")
    rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    made = [_row(row) for row in rows]
    remember(made)
    return {
        "code": asked,
        "market": MARKET,
        "rows": made,
        "count": market_values.count(raw.get("nrec")),
        "nextKey": str(raw.get("nxt_key") or "").strip(),
        "remainingDaysLimit": market_values.count(remaining_days_limit),
    }


#: 만기 조회로 **이미 받아 둔** 종목별 만기일(`YYYY-MM-DD`). 현재가 답의 잔존일을
#: 여기서 채웁니다 — 해외 현재가 응답에는 잔존일이 없고, 현재가를 부를 때마다
#: 만기 조회를 한 번 더 부르면 시세 호출 제한을 두 배로 씁니다. 앱이 켜져 있는
#: 동안만 두는 값입니다. 열쇠가 종목코드라 크기는 만기를 조회한 종목 수를 넘지 않습니다.
_EXPIRIES: dict[str, str] = {}


def remember(rows: Iterable[dict[str, Any]]) -> None:
    """만기 조회 줄들의 만기일을 담아 둡니다. 만기일이 없는 줄은 건너뜁니다."""
    for row in rows:
        code = str(row.get("code") or "").strip()
        expiry = row.get("expiry")
        if code and expiry:
            _EXPIRIES[code] = str(expiry)


def days_left(code: str, today: date) -> int | None:
    """그 종목의 만기일까지 남은 날. **받아 둔 만기일이 없으면 `None`** 입니다.

    셈은 만기 조회의 잔존일과 같습니다(만기일 − 오늘) — 같은 종목의 두 답이
    다른 수를 말하지 않게 합니다. 만기가 지났으면 0 입니다. 국내 현재가의 잔존일
    (`rem_dy`)은 만기일을 포함해 세므로 이 셈과 1 차이가 납니다.
    """
    held = _EXPIRIES.get(str(code or "").strip())
    if not held:
        return None
    try:
        expiry = date.fromisoformat(held)
    except ValueError:
        return None
    return max((expiry - today).days, 0)


def _row(row: dict[str, Any]) -> dict[str, Any]:
    """만기 한 줄. 날짜는 `YYYY-MM-DD`, 없거나 날짜가 아니면 `null` 입니다."""
    return {
        "code": _text(row.get("occurs1_sym")),
        "name": _text(row.get("occurs1_sym_nm")),
        "productCode": _text(row.get("occurs1_prd_cd")),
        "exchange": _text(row.get("occurs1_exch_cd")),
        # 결제방법명(현금결제·실물인수도). 원문 그대로입니다.
        "settlement": _text(row.get("occurs1_sett_nm")),
        "firstNotice": market_values.date(row.get("occurs1_fst_ntf_dt")),
        "expiry": market_values.date(row.get("occurs1_exp_dt")),
        "lastSettlement": market_values.date(row.get("occurs1_lst_sett_dt")),
        # 만기일까지 남은 날. 만기일을 포함해 셉니다(명세).
        "remainingDays": market_values.count(row.get("occurs1_rem_dy")),
    }


def details(raws: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """품목별세부정보 응답들 → `{products, count}`.

    이어 받은 쪽들을 한 벌로 합칩니다. **품목코드가 열쇠**라 같은 코드가 두 번
    오면 뒤엣것이 남고, 코드가 비어 있는 줄은 뺍니다 — 화면이 품목코드로 찾아
    쓰는 표라 열쇠가 없는 줄은 둘 자리가 없습니다.
    """
    merged: dict[str, dict[str, Any]] = {}
    for raw in raws:
        rows = raw.get("Occurs1") if isinstance(raw, dict) else None
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            made = _detail(row)
            code = made["productCode"]
            if code:
                merged[code] = made
    return {"products": list(merged.values()), "count": len(merged)}


def _detail(row: dict[str, Any]) -> dict[str, Any]:
    """품목 한 줄. 숫자로 못 읽는 값과 빈 값은 `null` 입니다."""
    return {
        # 월물이 아니라 품목 코드입니다 — 옵션은 `O_`, 스프레드는 `S_` 가 붙습니다.
        "productCode": _text(row.get("occurs1_prd_cd")),
        "name": _text(row.get("occurs1_ko_prd_nm")),
        "kind": kind_of(row.get("occurs1_fut_opt_div_cd")),
        # 품목그룹은 **이름만** 옵니다(금리·통화·지수…). 코드는 이 응답에 없습니다.
        "group": _text(row.get("occurs1_prd_grp_nm")),
        "exchangeCode": _text(row.get("occurs1_exch_cd")),
        "currency": _text(row.get("occurs1_cur_cd")),
        "contractUnit": market_values.number(row.get("occurs1_ctr_unt")),
        "tickSize": market_values.number(row.get("occurs1_tick_unt")),
        "tickValue": market_values.number(row.get("occurs1_tick_val")),
        "delivery": DELIVERY.get(str(row.get("occurs1_dlv_div") or "").strip()),
        "appliedDate": market_values.date(row.get("occurs1_apl_dt")),
    }


def _text(value: Any) -> str | None:
    return str(value or "").strip() or None
