"""해외 품목 정보를 받아 두는 자리. 앱에 하나입니다.

계약단위·틱사이즈·틱값은 **품목마다 하루에 한 번이면 충분한 값**입니다. 화면은
손익을 셈할 때마다 이 표를 보는데, 그때마다 NH선물에 물으면 계좌 등급 호출
제한을 이 표 하나가 다 씁니다. 그래서 날마다 한 번만 받아 두고 같은 날 두
번째 요청부터는 받아 둔 것을 그대로 냅니다(`source` 가 `api` 인지 `cache` 인지로
어느 쪽인지 알 수 있습니다). 화면이 「다시 받기」를 시키면 `force` 로 새로 받습니다.

날짜는 **한국시간의 달력 날짜**입니다. 이 표가 바뀌는 날은 적용일자
(`appliedDate`)가 바뀌는 날뿐이라 거래일의 경계를 따질 값이 아닙니다.

**이 표는 실전 서버에서 받습니다.** 계약 규격은 계좌가 아니라 거래소가 정하는
시장의 값이라 거래시간표와 같은 규칙을 따릅니다 — 거래시간표는 모의 서버의 품목
목록·시간이 실전과 다릅니다. 해외 실전 인증 정보가 없을 때만 모의 서버에서 받고, 어느 쪽에서
받았는지는 표의 `environment` 에 적습니다. 화면에서 고른 환경과 무관한 한 벌이라 환경을 바꿔도 버리지 않습니다
(`backend.nh_api.reference`).

한 번에 100줄까지 오므로 `nxt_key` 로 이어 받아 한 벌로 합칩니다.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any

from backend.nh_api.client import TradeClient
from backend.nh_api.reference import reference_environment
from backend.overseas_futureoption import overseas_futureoption_functions as overseas_api
from backend.overseas_futureoption import overseas_products
from backend.sessions.hours import KST, iso, now_kst

logger = logging.getLogger(__name__)

#: 방금 NH선물에서 받은 것.
SOURCE_API = "api"
#: 오늘 받아 두었던 것.
SOURCE_CACHE = "cache"

#: 이어 받기 상한. 잘못된 키로 끝없이 도는 것을 막습니다. 한 쪽이 100 줄이고
#: 품목 수는 서버가 정하므로 넉넉히 둡니다.
MAX_PAGES = 10


class ProductService:
    """품목 표 한 벌을 들고 있습니다. 한 번에 하나만 받도록 잠급니다."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._made: dict[str, Any] | None = None
        # 받아 둔 표가 어느 날 것인지. `YYYY-MM-DD`(한국시간).
        self._day = ""

    def products(
        self,
        *,
        api: TradeClient | None = None,
        force: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """품목 표. 오늘 받아 둔 것이 있으면 그것을, 없으면 새로 받습니다.

        Raises:
            ConfigError: 실전·모의 어디에도 해외 인증 정보가 없는 경우.
            NHApiError 계열: 받아야 하는데 받지 못한 경우. 받아 둔 것이 있어도
                `force` 로 부르면 그렇습니다 — 화면이 「다시 받기」를 시킨 자리라
                옛 표를 새 표인 양 답하지 않습니다.
        """
        now = now or now_kst()
        today = now.astimezone(KST).strftime("%Y-%m-%d")
        if not force:
            kept = self._kept(today)
            if kept is not None:
                return kept
        with self._lock:
            # 잠금을 기다리는 동안 다른 쪽이 받아 두었을 수 있습니다.
            if not force:
                kept = self._kept(today)
                if kept is not None:
                    return kept
            api = api or overseas_api.default_client()
            with reference_environment(api, overseas_products.MARKET) as environment:
                made = overseas_products.details(self._pages(api, environment))
            made["environment"] = environment
            made["asOf"] = iso(now)
            self._made = made
            self._day = today
            return dict(made, source=SOURCE_API)

    def cached(self) -> dict[str, Any] | None:
        """받아 둔 표. 날짜와 상관없이 있으면 그것입니다. 없으면 `None`."""
        return self._made

    def reset(self) -> None:
        """받아 둔 것을 버립니다. 테스트가 씁니다."""
        with self._lock:
            self._made = None
            self._day = ""

    def _kept(self, today: str) -> dict[str, Any] | None:
        """오늘 받아 둔 표. 어제 것이거나 아직 없으면 `None`."""
        if self._made is None or self._day != today:
            return None
        return dict(self._made, source=SOURCE_CACHE)

    @staticmethod
    def _pages(api: TradeClient, environment: str) -> list[dict[str, Any]]:
        """이어 받기 키가 빌 때까지 받은 쪽들.

        **같은 키가 다시 오면 멈춥니다.** 앞으로 나아가지 못한 것이라, 그대로
        두면 같은 쪽을 상한까지 다시 받습니다. 멈추면 그 뒤 품목이 표에서 빠지고
        그 표를 그날 내내 쓰게 되므로 경고를 남깁니다.
        """
        pages: list[dict[str, Any]] = []
        next_key = ""
        asked = {next_key}
        for _ in range(MAX_PAGES):
            raw = overseas_api.product_detail(
                next_key=next_key, api=api, environment=environment
            )
            pages.append(raw)
            next_key = str(raw.get("nxt_key") or "").strip()
            if not next_key:
                break
            if next_key in asked:
                logger.warning(
                    "해외 품목 정보의 이어받기 키(%s)가 되돌아와 %d 쪽에서 멈췄습니다. "
                    "그 뒤 품목은 이 표에 없습니다.",
                    next_key,
                    len(pages),
                )
                break
            asked.add(next_key)
        else:
            logger.warning(
                "해외 품목 정보를 %d 쪽까지만 받았습니다. 그 뒤 품목은 이 표에 없습니다.",
                MAX_PAGES,
            )
        return pages


#: 앱이 쓰는 하나.
service = ProductService()
