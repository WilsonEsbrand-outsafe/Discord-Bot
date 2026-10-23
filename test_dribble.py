# test_dribble.py — 3.1.0 ⚽ /드리블: 배당 사다리(어디서 멈춰도 기대값 약 -5%) · 버튼 흐름 · 정산 · 테스트 서버 먼저
import asyncio
import random
import tempfile
from pathlib import Path
from types import SimpleNamespace

import services.economy_db as edb

edb.DB_PATH = Path(tempfile.mkdtemp()) / "t.sqlite3"
import cogs.dribble as CD   # noqa: E402
import release   # noqa: E402
import services.dribble as D   # noqa: E402

TEST = release.TEST_GUILD


def test_table():
    assert len(D.MULTS) == len(D.STAGES) == 7 and D.MULTS[-1] == 120
    for n, m in enumerate(D.MULTS, 1):   # n명 제치고 멈추면 기대값 = m / 2^n — 모두 0.93~0.95
        assert 0.93 <= m / 2 ** n <= 0.95
    assert D.payout(1000, 0) == 1000 and D.payout(1000, 1) == 1900 and D.payout(1000, 7) == 120_000
    assert D.payout(10 ** 12, 7) == 10 ** 12 + D.MAX_PAYOUT
    rng = random.Random(1)
    assert abs(sum(D.step(rng) for _ in range(20000)) / 20000 - 0.5) < 0.02
    assert "⚽" in CD.field(2) and CD.ladder_text(0).count("배") == 7


class Resp:
    def __init__(self, log):
        self.log = log

    async def edit_message(self, embed=None, view=None):
        self.log.append((embed, view))

    async def defer(self):
        self.log.append(("defer", None))

    async def send_message(self, *a, **k):
        self.log.append(("msg", a))


def test_flow(monkeypatch=None):
    cog = CD.Dribble.__new__(CD.Dribble)
    cog.db = edb.EconomyDB()
    user = SimpleNamespace(id=5, display_name="드리블러", display_avatar=SimpleNamespace(url="https://x/a.png"),
                           guild=SimpleNamespace(id=TEST))

    async def flow():
        await cog.db.set_balance(5, 100_000)
        log = []
        it = SimpleNamespace(user=user, response=Resp(log))
        # 1) 두 명 제치고 슛 → 3.8배
        await cog.db.add_balance(5, -10_000)
        v = CD.DribbleView(cog, user, 10_000)
        assert v.children[2].disabled   # 한 명도 못 제치면 슛 불가
        seq = iter([1, 1])              # 수비수는 오른쪽을 막는다 → 왼쪽(0)으로 제침
        CD.D.step = lambda rng=None: next(seq)
        await v._go(0)(it)
        await v._go(0)(it)
        assert v.passed == 2 and not v.children[2].disabled and "38,000" in v.children[2].label
        await v._shoot(it)
        assert v.done and await cog.db.get_balance(5) == 100_000 - 10_000 + 38_000
        assert "+28,000" in log[-1][0].description
        # 2) 태클 → 베팅금 잃음
        await cog.db.add_balance(5, -10_000)
        v = CD.DribbleView(cog, user, 10_000)
        CD.D.step = lambda rng=None: 0
        await v._go(0)(it)
        assert v.done and await cog.db.get_balance(5) == 118_000 and "태클" in log[-1][0].title
        await v._go(1)(it)   # 끝난 판은 무시
        assert log[-1][0] == "defer"
        # 3) 끝까지 → 골 120배
        await cog.db.add_balance(5, -1_000)
        v = CD.DribbleView(cog, user, 1_000)
        CD.D.step = lambda rng=None: 1
        for _ in range(7):
            await v._go(0)(it)
        assert v.done and await cog.db.get_balance(5) == 117_000 + 120_000 and "솔로 골" in log[-1][0].title
        # 4) 시간 초과: 0명이면 원금 돌려줌 · 제쳤으면 자동 슛
        await cog.db.add_balance(5, -1_000)
        v = CD.DribbleView(cog, user, 1_000)
        await v.on_timeout()
        assert await cog.db.get_balance(5) == 237_000
        await cog.db.add_balance(5, -1_000)
        v = CD.DribbleView(cog, user, 1_000)
        await v._go(0)(it)
        await v.on_timeout()
        assert await cog.db.get_balance(5) == 237_000 - 1_000 + 1_900
        assert CD.play_card(user, 1_000, 3, "x").title

    real = CD.D.step
    try:
        asyncio.run(flow())
    finally:
        CD.D.step = real


def test_release_gate():
    other = 1
    assert "드리블" in release.hidden_commands(other) and "드리블" not in release.hidden_commands(TEST)
    if release.RELEASE_310_TS:
        assert "드리블" not in release.hidden_commands(other, release.RELEASE_310_TS)
