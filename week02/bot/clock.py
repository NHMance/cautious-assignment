"""시각. 실행은 실제 시계, 리플레이·봇 테스트·테스트는 가상 시계를 씁니다.

가상 시계는 `sleep` 하면 그만큼 시각만 앞으로 갑니다. 리플레이로 09:00~15:20 을
몇 초 만에 끝까지 돌려 볼 수 있습니다.
"""
from __future__ import annotations

import time as _time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def at(day: date, hhmm: str) -> datetime:
    """그날의 `HH:MM` 시각(KST)."""
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=KST)


def floor_to(moment: datetime, minutes: int) -> datetime:
    """`minutes` 분 경계로 내림."""
    return moment.replace(minute=moment.minute - moment.minute % minutes, second=0, microsecond=0)


class RealClock:
    def now(self) -> datetime:
        return datetime.now(tz=KST)

    def sleep(self, seconds: float) -> None:
        _time.sleep(max(0.0, seconds))


class SimClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += timedelta(seconds=max(0.0, seconds))
