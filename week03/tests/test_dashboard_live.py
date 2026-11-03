"""대시보드 실시간 허브 — NH 접속 없이 가짜 starter 로 확인합니다."""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")

from frontend.live import LiveHub, Started  # noqa: E402


class FakeStarter:
    def __init__(self) -> None:
        self.emit = None
        self.started = 0
        self.stopped = 0

    def __call__(self, emit):
        self.emit = emit
        self.started += 1
        return Started(self.stop, {"symbol": "MKUX26", "real_sym": "MKUX26", "rest": {"price": 0.7438, "time": "015417"}})

    def stop(self) -> None:
        self.stopped += 1


async def _drain(q: asyncio.Queue) -> list[dict]:
    await asyncio.sleep(0.05)
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_starts_on_first_client_and_broadcasts_ticks():
    async def run():
        fake = FakeStarter()
        hub = LiveHub(fake, idle_stop=0.05, enabled_=True)
        q1 = await hub.join()
        q2 = await hub.join()
        await asyncio.sleep(0.05)
        assert fake.started == 1 and hub.running
        assert hub.info["rest"]["price"] == 0.7438

        fake.emit({"type": "status", "status": "connected", "detail": ""})
        fake.emit({"type": "tick", "price": 0.7440, "sym": "MKUX26"})
        e1, e2 = await _drain(q1), await _drain(q2)
        assert [e["type"] for e in e1][-2:] == ["status", "tick"]
        assert e1[-1] == e2[-1]
        assert hub.status == "connected" and hub.snapshot()["ticks"][-1]["price"] == 0.7440

        await hub.leave(q1)
        await hub.leave(q2)
        await asyncio.sleep(0.15)
        assert fake.stopped == 1 and not hub.running

    asyncio.run(run())


def test_rejoin_before_idle_timeout_keeps_connection():
    async def run():
        fake = FakeStarter()
        hub = LiveHub(fake, idle_stop=0.1, enabled_=True)
        q = await hub.join()
        await asyncio.sleep(0.05)
        await hub.leave(q)
        q = await hub.join()
        await asyncio.sleep(0.2)
        assert fake.started == 1 and fake.stopped == 0 and hub.running
        await hub.shutdown()
        assert fake.stopped == 1

    asyncio.run(run())


def test_missing_key_is_reported_not_raised():
    class ConfigError(Exception):
        pass

    def starter(_emit):
        raise ConfigError("해외 모의 인증 정보가 없습니다")

    async def run():
        hub = LiveHub(starter, enabled_=True)
        q = await hub.join()
        events = await _drain(q)
        assert hub.status == "no_key"
        assert events[-1]["status"] == "no_key"

    asyncio.run(run())


def test_disabled_never_connects():
    async def run():
        fake = FakeStarter()
        hub = LiveHub(fake, enabled_=False)
        await hub.join()
        await asyncio.sleep(0.05)
        assert fake.started == 0 and hub.snapshot()["status"] == "disabled"

    asyncio.run(run())
