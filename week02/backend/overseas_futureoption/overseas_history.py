"""해외 분봉을 **영업일 단위**로 모읍니다.

차트를 왼쪽으로 끌 때마다 앞 영업일을 하나씩 이어 붙이는 자리입니다.

## 국내와 나누는 방법이 다릅니다

국내는 영업일 경계가 **정해져 있습니다**(전날 18:00 야간 개장부터 그날 주간
마감까지). 해외는 거래소마다 다르고, 그 시간표가 응답에 없습니다.

대신 NH선물이 **줄마다 해외영업일을 답해 줍니다**(`occurs1_glb_biz_dt`). 그래서
창으로 나누지 않고 **받은 줄을 그 칸으로 가릅니다** — 거래소가 늘어도 시간표를
채울 일이 없고, 경계가 몇 시인지 알 필요도 없습니다.

## 시작 날짜는 **창의 아래끝**입니다

실서버의 응답은 이렇습니다.

    시작 날짜 20260922 로 물음  →  495줄 09-22 21:26 ~ 09-23 13:34 + 시각 커서
    시작 날짜 20260919(토)      →  같은 495줄
    시작 날짜 오늘              →  341줄 09-23 00:00 ~ + 키가 전부 9

곧 `inq_strt_dt` 는 「그 날짜의 것을 달라」가 아니라 **「여기까지만 거슬러 가라」**
입니다. 서버는 그 창 안에서 **가장 최근 495줄**을 내고, `nxt_key` 로 창 안을
뒤로 걷게 해 주며, **창 아래끝에 닿으면 키를 전부 9 로** 답합니다.

그래서 시작 날짜를 「오늘」로 두면 **오늘 00:00 에서 걷기가 끝나** 앞 영업일로
갈 수가 없습니다. `WINDOW_DAYS` 만큼 과거를 아래끝으로 둡니다 — 창을 넓혀도
한 응답의 줄 수는 그대로라 **조회가 늘지 않습니다.**

    1쪽 495줄  09-23 03:03~12:17   키 20260923030200999999
    2쪽 495줄  09-22 18:48~03:02   키 20260922184700999999

한 쪽이 여덟아홉 시간이고 해외 영업일은 스물세 시간쯤이라, **한 영업일이 세
쪽쯤**입니다. 그래서 키로 뒤로 걸으면서 `glb_biz_dt` 로 영업일을 모읍니다.

**키를 지어내지 않습니다.** 위 두 쪽에서 「가장 오랜 줄의 1분 전」이라는
규칙이 보이지만 명세에 없는 규칙이라, 서버가 준 값만 되돌려 보냅니다.

## 한 영업일이 다 찼는지는 「그 앞 영업일이 보이면」

줄은 새것부터 옵니다. 모으는 영업일보다 **오랜 영업일의 줄**이 나오면 그 영업일은
다 지나온 것입니다. 경계가 걸린 그 쪽에는 두 영업일이 함께 들어 있어, 다음 장은
**그 쪽부터 다시** 걸어야 줄을 안 잃습니다 — 그래서 `before` 한 칸에 **커서와
이미 낸 영업일**을 함께 담습니다(`as_before`).

## 빈 한 시간은 정상입니다

`05:59 → 07:00` 처럼 한 시간이 비는 자리가 있습니다(CME 유지보수). 영업일
경계와 같은 자리입니다 — **빠진 자료가 아니라 장이 쉬는 시간**이라, 빈
구간을 찾는 검사를 두지 않습니다. 두면 날마다 거짓 경보가 납니다.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Iterable

from backend.sessions.hours import is_weekend

#: 줄마다 실려 오는 해외영업일. **이 칸 하나로 영업일을 가릅니다.**
BIZ_FIELD = "occurs1_glb_biz_dt"

#: 응답에서 봉 줄이 담기는 자리.
ROWS_FIELD = "Occurs1"

#: 창의 아래끝을 오늘에서 며칠 앞으로 둘지(달력 일수).
#:
#: **이 창 밖으로는 못 갑니다** — 화면이 뒤로 가다 여기에 닿으면 멈춥니다.
#: 넓혀도 한 응답의 줄 수는 그대로라 조회가 늘지 않습니다. 영업일로는 서른
#: 날쯤입니다.
#:
#: 넓은 창도 **첫 두 쪽은 같은 모양**입니다 — 45일 아래끝으로 읽으면 첫 쪽이
#: 495줄 + 시각 커서이고, 그 커서로 받은 둘째 쪽이 이음매까지 맞습니다. 폭은
#: 쓸모로 고른 수입니다 — 화면이 한 달 남짓 뒤로 갈 수 있게.
WINDOW_DAYS = 45

#: 한 칸에 커서와 이미 낸 영업일을 함께 담을 때 쓰는 표시.
#:
#: **화면은 속을 안 봅니다** — 답의 `before` 를 그대로 다시 보내면 됩니다.
#: 나눠 두는 것은 서버가 「어디서부터, 어느 영업일보다 오랜 것을」 모을지
#: 알아야 하기 때문입니다.
CURSOR_MARK = "@"


def as_day(text: str) -> date | None:
    """`YYYYMMDD` 를 날짜로. 여덟 자리 숫자가 아니면 `None` 입니다."""
    body = str(text or "").strip()
    if len(body) != 8 or not body.isdigit():
        return None
    try:
        return datetime.strptime(body, "%Y%m%d").date()
    except ValueError:
        return None


def as_text(day: date) -> str:
    """날짜를 `YYYYMMDD` 로. 요청에 실어 보내는 모양입니다."""
    return day.strftime("%Y%m%d")


def next_day(day: date) -> date:
    """달력으로 하루 뒤. **영업일이 아니라 달력**입니다 — 조회는 달력으로 합니다."""
    return day + timedelta(days=1)


def previous_weekday(day: date) -> date:
    """앞 평일. **답에서 앞 영업일을 못 읽었을 때만** 씁니다.

    휴장일 달력이 없어 주말만 건너뜁니다. 휴장일을 짚으면 그 장이 비어 나오고,
    화면이 한 칸 더 뒤로 갑니다.
    """
    made = day - timedelta(days=1)
    while is_weekend(made):
        made -= timedelta(days=1)
    return made


def as_before(cursor: str, day: date | None) -> str:
    """다음 장에 그대로 실어 보낼 값. `커서@영업일` 입니다.

    **영업일 자리가 비면 「그 커서에서 가장 새 영업일을 이어서」** 라는 뜻입니다.
    한 영업일이 쪽 상한을 넘어 아직 다 못 모았을 때가 그렇습니다 — 다음 장이
    같은 영업일로 옵니다. 값이 있으면 「그 영업일은 다 냈으니 더 오랜 것을」
    입니다.
    """
    return f"{cursor}{CURSOR_MARK}{as_text(day) if day is not None else ''}"


def as_cursor(text: str) -> tuple[str, date | None]:
    """`before` 를 (커서, 이미 낸 영업일) 로 가릅니다.

    비어 있거나 모양이 아니면 `("", None)` — 가장 최근부터 걷습니다.
    """
    body = str(text or "").strip()
    if CURSOR_MARK not in body:
        return "", None
    cursor, _, tail = body.partition(CURSOR_MARK)
    return cursor.strip(), as_day(tail)


def next_key_of(raw: dict[str, Any] | None) -> str:
    """그 응답이 준 이어받기 키. **그대로 되돌려 보냅니다.**"""
    return str((raw or {}).get("nxt_key") or "").strip()


def at_end(key: str) -> bool:
    """창의 아래끝에 닿았는지. **전부 9 인 키**가 그 표시입니다.

    진짜 커서는 시각(`20260923030200999999`)이라 9 로만 이루어질 수 없습니다.
    더 못 가는 것과 **탈이 나서 못 가는 것**은 다르니 여기서 가릅니다 — 창 끝은
    탈이 아닙니다.
    """
    body = str(key or "").strip()
    return bool(body) and set(body) == {"9"}


def days_in(raw: dict[str, Any] | None) -> list[date]:
    """그 응답에 담긴 영업일들. 오래된 것부터입니다."""
    made = {as_day(str(row.get(BIZ_FIELD) or "")) for row in _rows(raw)}
    return sorted(one for one in made if one is not None)


def latest_in(raw: dict[str, Any] | None) -> date | None:
    """그 응답에서 가장 새 영업일. **지금 돌고 있는 영업일**을 이것으로 찾습니다."""
    found = days_in(raw)
    return found[-1] if found else None


def before_in(raws: Iterable[dict[str, Any] | None], day: date) -> date | None:
    """받아 둔 응답들에서 **그 영업일보다 앞선** 가장 새 영업일.

    첫 장이 아닐 때 「이번에 모을 영업일」을 고르는 자리입니다 — 이미 낸
    영업일보다 한 칸 오랜 것을 답에서 읽습니다. 한 줄도 없으면 `None` 입니다.
    """
    found = {one for raw in raws for one in days_in(raw) if one < day}
    return max(found) if found else None


def has_older(raw: dict[str, Any] | None, day: date) -> bool:
    """그 쪽에 **그 영업일보다 오랜 줄**이 있는지. 있으면 그 영업일은 다 찼습니다."""
    return any(one < day for one in days_in(raw))


def pick(raw: dict[str, Any], day: date) -> dict[str, Any]:
    """그 영업일 줄만 남긴 응답. 나머지 칸은 원문 그대로 둡니다.

    **봉으로 바꾸기 전에 거릅니다** — 봉이 된 뒤에는 어느 영업일 것인지 알 수
    없습니다(봉의 시각은 달력 기준입니다).
    """
    wanted = as_text(day)
    kept = [row for row in _rows(raw) if str(row.get(BIZ_FIELD) or "").strip() == wanted]
    return {**raw, ROWS_FIELD: kept}


def _rows(raw: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = (raw or {}).get(ROWS_FIELD)
    return [one for one in rows if isinstance(one, dict)] if isinstance(rows, list) else []
