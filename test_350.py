# test_350.py — 3.5.0: 파산 탕감 확률 0% 10% · 30~70% 75% · 100% 15% (테스트 서버 먼저)
# 실행: venv/Scripts/python.exe test_350.py
import asyncio
import random
import tempfile
from pathlib import Path

import services.club_db as cdb
import services.economy_db as edb
import services.player_market_db as pmdb

TMP = Path(tempfile.mkdtemp()) / "t.sqlite3"
edb.DB_PATH = cdb.DB_PATH = pmdb.DB_PATH = TMP

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


def test_fishing_rare_up():
    import services.fishing as F
    for w in (10, 120, 300, 600):
        old, new = F.grade_odds(w, 0, True, True), F.grade_odds(w, 0, True, True, True)
        assert sum(new[2:]) > sum(old[2:]) and new[4] > old[4]


def test_prospect_mf():
    """3.5.0: 미드필더 유망주 — 도움 경험치 = 골 경험치 · 약팀 상대엔 이기기만 하면 감점 없음."""
    import sqlite3
    import time

    class NoHurt:
        @staticmethod
        def random():
            return 1.0

    async def flow():
        now = int(time.time())
        db, clubs = edb.EconomyDB(), cdb.ClubDB()
        out = {}
        for uid, pos in ((501, "CM"), (502, "ST")):
            await db.add_balance(uid, 10_000_000)
            info, _ = cdb.prospect_input("테스트", "대한민국", pos, 8, "1-1")
            out[pos] = (await clubs.create_prospect(uid, info, now))["pid"]
        con = sqlite3.connect(TMP)
        con.execute("UPDATE prospects SET pro=10, proneness=1, pot=99")
        con.commit(); con.close()

        async def play(pid, opp, gap, goals=(), v350=True):
            return (await clubs.record_prospects([([{"player_id": pid}], 1, 0, opp, gap)], list(goals), now,
                                                 rng=NoHurt, v350=v350))[0]["xp"]
        mf, fw = out["CM"], out["ST"]
        assist = [{"scorer_id": "x", "assist_id": mf}]
        assert await play(mf, 601, 0, assist, v350=False) == 10 + 5 + 4
        assert await play(mf, 602, 0, assist) == 10 + 5 + 6           # 도움 = 골만큼
        assert await play(mf, 603, 45) == 0                           # 이겼으면 공격 포인트 없어도 감점 없음
        assert await play(mf, 604, 45, v350=False) == -10             # 예전 서버는 그대로
        assert await play(fw, 605, 45) == -10                         # 공격수는 그대로
    asyncio.run(flow())


if __name__ == "__main__":
    test_prospect_mf()
    test_fishing_rare_up()
    test_release_switch()
    test_table()
    test_bankrupt_rates()
    print("✅ test_350 통과")
