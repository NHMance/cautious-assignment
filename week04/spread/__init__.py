"""전략은 독립 구현하고 인증·REST 전송만 week02/backend 를 재사용합니다."""
from pathlib import Path
import sys

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "week02"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.append(str(BACKEND_ROOT))
