"""해외 모의 인증 정보를 week03/.env(또는 환경변수)에서 읽습니다.

    NH_PAPER_OVERSEAS_APP_KEY=...      해외 모의 APP Key
    NH_PAPER_OVERSEAS_APP_SECRET=...   해외 모의 APP Secret
    NH_PAPER_BASE_URL=...              (선택) 비우면 https://apidemo.futures.co.kr
    NH_PAPER_WS_URL=...                (선택) 비우면 wss://apidemo.futures.co.kr

week02 는 국내 키를 `NH_PAPER_APP_KEY` 로 받습니다. 해외 키는 다른 계좌이므로 이름부터 나누고,
파일도 week03/.env 로 따로 둡니다. `.env` 읽기는 week02 `bot.credentials.load_env_file` 을 그대로 씁니다.

**모의(paper)만 만듭니다.** 실전 키를 넣을 자리가 없어 잘못 설정해도 실전으로 주문이 나갈 수 없습니다.
환경변수가 없으면 week02 와 같이 `~/NHFutures/config/config.yaml` 을 읽습니다.
"""
from __future__ import annotations

import os
from pathlib import Path

import trader  # noqa: F401  (week02 경로 등록)
from backend.config import DEFAULT_BASE_URLS, Config, ConfigError, EnvironmentConfig, MarketConfig, load_config
from bot.credentials import load_env_file  # week02

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
KEY = "NH_PAPER_OVERSEAS_APP_KEY"
SECRET = "NH_PAPER_OVERSEAS_APP_SECRET"
BASE_URL = "NH_PAPER_BASE_URL"
WS_URL = "NH_PAPER_WS_URL"

# 개발자센터 명세(해외_시세체결 등)의 simulDomain·realDomain.
DEFAULT_WS_URLS = {
    "paper": "wss://apidemo.futures.co.kr",
    "real": "wss://api.futures.co.kr",
}

MARKET = "overseas"


def _empty(environment: str) -> EnvironmentConfig:
    return EnvironmentConfig(
        name=environment,
        base_url=DEFAULT_BASE_URLS[environment],
        domestic=MarketConfig(environment, "domestic", "", ""),
        overseas=MarketConfig(environment, "overseas", "", ""),
    )


def from_env(path: Path = ENV_FILE) -> Config | None:
    """환경변수에 해외 모의 키가 있으면 그것으로 만든 설정. 없으면 None."""
    load_env_file(path)
    key = os.environ.get(KEY, "").strip()
    secret = os.environ.get(SECRET, "").strip()
    if not key or not secret:
        return None
    base_url = (os.environ.get(BASE_URL, "").strip() or DEFAULT_BASE_URLS["paper"]).rstrip("/")
    paper = EnvironmentConfig(
        name="paper",
        base_url=base_url,
        domestic=MarketConfig("paper", "domestic", "", ""),
        overseas=MarketConfig("paper", "overseas", key, secret),
    )
    return Config(default_environment="paper", environments={"paper": paper, "real": _empty("real")})


def load() -> tuple[Config, str]:
    """(설정, 출처). 환경변수가 우선이고 없으면 config.yaml."""
    config = from_env()
    if config is not None:
        return config, "env"
    try:
        config = load_config()
    except ConfigError as exc:
        raise ConfigError(
            f"해외 모의 인증 정보가 없습니다. 둘 중 하나를 채워 주세요.\n"
            f"  1) {ENV_FILE} 의 {KEY}, {SECRET} 값 (또는 같은 이름의 환경변수)\n"
            f"  2) 설정 파일 — {exc}"
        ) from exc
    if not config.environment("paper").overseas.app_key:
        raise ConfigError(f"config.yaml 의 paper.overseas 키가 비어 있습니다. {ENV_FILE} 에 {KEY} 를 넣어 주세요.")
    return config, "config.yaml"


def ws_url(environment: str = "paper") -> str:
    if environment == "paper":
        override = os.environ.get(WS_URL, "").strip()
        if override:
            return override.rstrip("/")
    return DEFAULT_WS_URLS[environment]
