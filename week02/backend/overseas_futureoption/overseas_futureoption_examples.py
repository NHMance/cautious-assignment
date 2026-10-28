"""해외 선물/옵션 REST 실행 예제.

저장해 둔 응답으로 돌려 보는 예제입니다. **NH선물 서버에 접속하지 않습니다.**

    uv run python -m backend.overseas_futureoption.overseas_futureoption_examples

실서버로 부르려면 설정 파일에 인증 정보가 있어야 하고, 아래 `create_session(replay=True)`
를 `create_session()` 으로 바꾸면 됩니다. 그때는 실제 호출이 나가고 호출 제한도
실제로 걸립니다.
"""
from __future__ import annotations

import json

from backend.nh_api.client import TradeClient
from backend.nh_api.session import create_session
from backend.overseas_futureoption import overseas_futureoption_functions as api
from backend.overseas_futureoption import overseas_products


def show(title: str, value: object) -> None:
    print(f"\n== {title} ==")
    print(json.dumps(value, ensure_ascii=False, indent=1)[:1200])


def main() -> None:
    # 저장해 둔 응답으로 답하는 접속입니다. 실서버 호출이 나가지 않습니다.
    with create_session(replay=True) as session:
        client = TradeClient(session)

        raw = api.product_detail(api=client, environment="real")
        rows = raw.get("Occurs1") or []
        show("품목별세부정보 원문 한 줄", rows[0] if rows else {})
        print(f"\n받은 줄 {len(rows)}개, 이어받기 키 {raw.get('nxt_key')!r}")

        made = overseas_products.details([raw])
        show("품목별세부정보 화면 모양 앞 세 개", made["products"][:3])
        print(f"\n품목 {made['count']}개")


if __name__ == "__main__":
    main()
