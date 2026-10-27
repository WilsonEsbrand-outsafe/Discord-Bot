# test_350.py — 3.5.0: 파산 탕감 확률 0% 10% · 30~70% 75% · 100% 15% (테스트 서버 먼저)
# 실행: venv/Scripts/python.exe test_350.py
import asyncio
import random
import tempfile
from pathlib import Path

import services.economy_db as edb

TMP = Path(tempfile.mkdtemp()) / "t.sqlite3"
edb.DB_PATH = TMP

import release   # noqa: E402

T, OTHER = release.TEST_GUILD, 757761125403066419


def test_release_switch():
    assert release.preview_350(T) and not release.preview_350(OTHER, 0)


def test_table():
    assert [p for p, _ in edb.BANKRUPT_350] == [0.10, 0.75, 0.15]
    assert abs(sum(p for p, _ in edb.BANKRUPT_350) - 1) < 1e-9


def test_bankrupt_rates():
    db = edb.EconomyDB()

    async def flow():
        n, zero, full = 600, 0, 0
        rng = random.Random(350)
        for k in range(n):
            u = 10_000 + k
            await db.add_balance(u, -1_000_000)
            r = await db.declare_bankruptcy(u, 1_000_000, rng, new=True, v311=True, v350=True)
            zero += r["rate"] == 0.0
            full += r["rate"] >= 1
        assert 0.06 < zero / n < 0.14 and 0.11 < full / n < 0.19
    asyncio.run(flow())


if __name__ == "__main__":
    test_release_switch()
    test_table()
    test_bankrupt_rates()
    print("✅ test_350 통과")
