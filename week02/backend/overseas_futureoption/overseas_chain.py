"""해외 옵션전체시세 응답을 옵션 체인(행사가별 콜·풋 한 줄) 모양으로 바꿉니다.

국내 체인과 같은 뼈대입니다 — 한 줄이 행사가 하나, 그 줄에 콜과 풋. **행마다
실제 콜·풋 종목코드**(`O_ESU26-C7100.0` 꼴)가 실려 화면이 그 코드로 현재가를
조회하고 실시간을 구독합니다. 한 번에 다 오므로 이어받기가 없습니다.

해외 응답에는 국내와 달리 이론가·그릭스·미결제약정이 없고, 대신 호가 잔량과
ATM 표시가 있습니다. 기초자산·선물 값도 오지 않아 그 자리는 `null` 입니다 —
칸은 국내와 같이 두어 화면이 시장에 따라 모양을 바꾸지 않아도 됩니다.

가격 칸의 `0` 은 국내와 같이 **없음**입니다.
"""
from __future__ import annotations

from typing import Any

from backend import market_values


def chain(raw: dict[str, Any], *, asked: str) -> dict[str, Any]:
    """옵션전체시세 응답을 체인 하나로. `asked` 는 부를 때 쓴 시리즈 코드입니다."""
    rows = raw.get("Occurs1") if isinstance(raw, dict) else None
    rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    return {
        "market": "overseas",
        "series": asked,
        "underlying": None,
        "future": None,
        # 만기까지 남은 날 — 받은 값 그대로입니다. 명세는 「만기일 포함」이라 적지만
        # 해외 보유포지션만기조회의 같은 이름 칸은 만기일을 포함하지 않습니다
        # (만기일 − 오늘). 이 응답도 같은 셈인지는 명세로 가릴 수 없어 그대로 냅니다.
        # 국내는 선물 잔존일이 이 자리에 있습니다.
        "daysLeft": market_values.count(raw.get("rem_dy")) if isinstance(raw, dict) else None,
        "rows": [_row(row) for row in rows],
        "count": len(rows),
    }


def _row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "strike": market_values.number(row.get("occurs1_strk_pric")),
        # 진법구분 원문(1: 정수, 2: 소수 한 자리, …). 화면이 자리 수를 맞출 때 씁니다.
        "priceCode": _text(row.get("occurs1_pric_ind")),
        "call": _leg(row, "c"),
        "put": _leg(row, "p"),
    }


def _leg(row: dict[str, Any], side: str) -> dict[str, Any]:
    def get(name: str) -> Any:
        return row.get(f"occurs1_{side}_{name}")

    return {
        "code": _text(get("sym")),
        "exchange": _text(get("exec_cd")),
        "last": _positive(get("last_pric")),
        "change": market_values.number(get("diff")),
        "changePct": market_values.number(get("rt")),
        "bid": _positive(get("bid_pric")),
        "ask": _positive(get("ask_pric")),
        "bidQty": market_values.count(get("bid_qty")),
        "askQty": market_values.count(get("ask_qty")),
        "volume": market_values.count(get("trd_qty")),
        "atm": str(get("atm_flag") or "").strip() == "1",
    }


def _positive(text: Any) -> float | None:
    """0 을 「없음」으로 보는 값."""
    value = market_values.number(text)
    return None if value is None or value == 0 else value


def _text(value: Any) -> str | None:
    return str(value or "").strip() or None
