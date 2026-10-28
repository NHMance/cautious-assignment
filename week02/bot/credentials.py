"""인증 정보를 환경변수(또는 week02/.env)에서 읽습니다.

    NH_PAPER_APP_KEY=...        국내 모의 APP Key
    NH_PAPER_APP_SECRET=...     국내 모의 APP Secret
    NH_PAPER_BASE_URL=...       (선택) 비우면 https://apidemo.futures.co.kr

참고 저장소 코드(`backend/config.py`)는 `~/NHFutures/config/config.yaml` 만 읽으므로,
backend 를 고치지 않고 여기서 같은 `Config` 를 만들어 `create_session(config=...)` 에 넘깁니다.
환경변수가 없으면 지금처럼 config.yaml 을 읽습니다.

**모의(paper)만 만듭니다.** 실전 키는 환경변수로 받지 않아, 잘못 설정해도 실전으로 주문이 나갈 수 없습니다.
"""
from __future__ import annotations

import os
from pathlib import Path

from backend.config import DEFAULT_BASE_URLS, Config, ConfigError, EnvironmentConfig, MarketConfig, load_config

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
KEY = "NH_PAPER_APP_KEY"
SECRET = "NH_PAPER_APP_SECRET"
BASE_URL = "NH_PAPER_BASE_URL"


def load_env_file(path: Path = ENV_FILE) -> None:
    """`KEY=VALUE` 줄을 읽어 환경변수에 넣습니다. 이미 설정된 환경변수가 우선합니다."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip().removeprefix("export ").strip()
        os.environ.setdefault(name, value.strip().strip('"').strip("'"))


def _empty(environment: str) -> EnvironmentConfig:
    return EnvironmentConfig(
        name=environment,
        base_url=DEFAULT_BASE_URLS[environment],
        domestic=MarketConfig(environment, "domestic", "", ""),
        overseas=MarketConfig(environment, "overseas", "", ""),
    )


def from_env() -> Config | None:
    """환경변수에 모의 키가 있으면 그것으로 만든 설정. 없으면 None."""
    load_env_file()
    key = os.environ.get(KEY, "").strip()
    secret = os.environ.get(SECRET, "").strip()
    if not key or not secret:
        return None
    base_url = (os.environ.get(BASE_URL, "").strip() or DEFAULT_BASE_URLS["paper"]).rstrip("/")
    paper = EnvironmentConfig(
        name="paper",
        base_url=base_url,
        domestic=MarketConfig("paper", "domestic", key, secret),
        overseas=MarketConfig("paper", "overseas", "", ""),
    )
    return Config(default_environment="paper", environments={"paper": paper, "real": _empty("real")})


def load() -> tuple[Config, str]:
    """(설정, 출처). 환경변수가 우선이고 없으면 config.yaml."""
    config = from_env()
    if config is not None:
        return config, "env"
    try:
        return load_config(), "config.yaml"
    except ConfigError as exc:
        raise ConfigError(
            f"인증 정보가 없습니다. 둘 중 하나를 채워 주세요.\n"
            f"  1) {ENV_FILE} 의 {KEY}, {SECRET} 값 (또는 같은 이름의 환경변수)\n"
            f"  2) 설정 파일 — {exc}"
        ) from exc
