"""3주차 봇. 2주차와 겹치는 코드(backend·clock·journal 등)는 week02 에서 그대로 불러 씁니다.

week02 의 `bot`·`backend` 패키지를 찾을 수 있도록 week02 폴더를 import 경로에 넣습니다.
3주차 패키지 이름을 `bot` 이 아닌 `trader` 로 둔 까닭도 이것입니다 — 이름이 같으면 week02 의 `bot` 을 가립니다.
"""
from __future__ import annotations

import sys
from pathlib import Path

WEEK02 = Path(__file__).resolve().parents[2] / "week02"
if str(WEEK02) not in sys.path:
    sys.path.append(str(WEEK02))
