"""football-data.org 전역 레이트 리미터 회귀 테스트.

실행: .venv/Scripts/python.exe test_rate_limit.py
"""
import asyncio
import time

import services.football_api as fa


def _reset():
    fa._call_times.clear()


def test_allows_burst_up_to_limit():
    """한도까지는 즉시 통과해야 한다 (불필요하게 느려지면 안 됨)."""
    _reset()
    async def go():
        t0 = time.monotonic()
        for _ in range(fa.RATE_LIMIT_PER_MIN):
            await fa._throttle()
        return time.monotonic() - t0
    assert asyncio.run(go()) < 0.5


def test_blocks_past_the_limit():
    """한도를 넘는 호출은 창이 열릴 때까지 대기해야 한다."""
    _reset()
    async def go():
        for _ in range(fa.RATE_LIMIT_PER_MIN):
            await fa._throttle()
        # 창이 거의 다 지난 것처럼 시간을 되돌린다 (실제로 60초 기다리지 않기 위해)
        fa._call_times[0] -= 59.7
        t0 = time.monotonic()
        await fa._throttle()
        return time.monotonic() - t0
    waited = asyncio.run(go())
    assert 0.1 < waited < 2.0, waited


def test_limiter_is_shared_across_instances():
    """인스턴스가 달라도 같은 한도를 공유해야 한다.

    정산 루프·자동 등록 루프·수동 명령이 각자 FootballAPI 를 만들기 때문에,
    인스턴스 단위로 세면 서로의 사용량을 몰라 429가 터진다.
    """
    _reset()
    a = fa.FootballAPI.__new__(fa.FootballAPI)   # 토큰 없이 리미터만 검사
    b = fa.FootballAPI.__new__(fa.FootballAPI)
    assert a is not b

    async def go():
        for _ in range(5):
            await fa._throttle()
        used_after_a = len(fa._call_times)
        for _ in range(4):
            await fa._throttle()
        return used_after_a, len(fa._call_times)

    first, total = asyncio.run(go())
    assert first == 5 and total == 9, (first, total)
    assert total == fa.RATE_LIMIT_PER_MIN


def test_limit_leaves_headroom():
    """무료 플랜은 분당 10회. 여유를 남겨야 한다."""
    assert fa.RATE_LIMIT_PER_MIN < 10


def test_old_calls_expire_from_window():
    _reset()
    async def go():
        await fa._throttle()
        fa._call_times[0] -= 61          # 61초 전 호출로 만든다
        await fa._throttle()
        return len(fa._call_times)
    assert asyncio.run(go()) == 1        # 만료된 건 창에서 빠져야 한다


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
