# test_antifarm.py — 2.6 유망주 경험치 악용 방지 · 명문 구단 강화 · 11명 꽉 차야 경기
# 실행: venv/Scripts/python.exe test_antifarm.py
import asyncio
import tempfile
import time
from pathlib import Path

import services.club_db as cdb
import services.economy_db as edb
import services.player_market_db as pmdb

TMP = Path(tempfile.mkdtemp()) / "t.sqlite3"
edb.DB_PATH = cdb.DB_PATH = pmdb.DB_PATH = TMP

from cogs.club import Club  # noqa: E402

U = 1


async def _flow():
    now = int(time.time())
    db, clubs = edb.EconomyDB(), cdb.ClubDB()
    await db.add_balance(U, 10_000_000)
    info, _ = cdb.prospect_input("파머", "대한민국", "ST", 9, "1-1")
    p = await clubs.create_prospect(U, info, now)
    pid = p["pid"]
    import sqlite3
    con = sqlite3.connect(TMP)
    con.execute("UPDATE prospects SET pro=10, proneness=1, pot=99 WHERE user_id=?", (U,))   # 배율 1 · 부상 거의 없음
    con.commit(); con.close()
    me = {"player_id": pid}
    goal = [{"scorer_id": pid}]

    class NoHurt:
        @staticmethod
        def random():
            return 1.0

    async def play(gf, ga, opp, gap, goals=(), friendly=False):
        out = await clubs.record_prospects([([me], gf, ga, opp, gap)], list(goals), now, rng=NoHurt,
                                           friendly=friendly, antifarm=True)
        return out[0]

    # 기준: 출전 10 + 승 5 + 골 6 = 21 · 출전 10 + 무 2 = 12
    x = await play(1, 0, 101, 0, goal)
    assert x["xp"] == 21 and x["mods"] == [] and x["minus"] == []
    assert (await play(0, 0, 102, 0, friendly=True))["xp"] == 6                    # 친선 ×0.5
    x = await play(1, 0, 103, 25, goal)
    assert x["xp"] == 8 and x["mods"] == ["전력 차 +25 ×0.4"]                       # 21 × 0.4
    assert (await play(1, 0, 104, 45, goal))["xp"] == 0                            # 40 이상 ×0 · 잘했으면 감점 없음
    x = await play(1, 0, 105, 45)                                                  # 이겼지만 공격수가 공격 포인트 없음
    assert x["xp"] == -10 and "poor" in x["minus"]
    assert (await play(0, 0, 106, 33, goal))["xp"] == round(18 * 0.1) - 5          # 30 이상 · 못 이김 → -5
    assert (await play(0, 0, 107, -15))["xp"] == 14                                # 강팀 상대 ×1.2 (12 → 14)
    # 같은 상대: 1~3번째 그대로 · 4~6번째 ×0.5 · 7번째부터 ×0
    got = [(await play(0, 0, 108, 0))["xp"] for _ in range(7)]
    assert got == [12, 12, 12, 6, 6, 6, 0], got
    # antifarm 이 꺼진 서버는 예전 그대로
    out = await clubs.record_prospects([([me], 1, 0, 109, 45)], goal, now, rng=NoHurt)
    assert out[0]["xp"] == 21 and out[0]["mods"] == []

    # 명문 구단 강화판: 이름은 같고 능력치 · 케미 · 주장
    for k in cdb.ELITE_CLUBS:
        old, new = cdb.elite_team(k), cdb.elite_team(k, True)
        assert [s["name"] for s in old["lineup"]] == [s["name"] for s in new["lineup"]]
        assert new["chem"] == 3 and new["captain_bonus"] == 1 and new["rating"] >= old["rating"] + 9
    assert cdb.elite_team("royal", True)["rating"] >= 95

    # 11명 꽉 차야 경기 (2.6 서버만)
    assert Club._ready({"filled": 11}, True) and not Club._ready({"filled": 10}, True)
    assert Club._ready({"filled": 10}, False) and not Club._ready({"filled": 0}, False)
    assert "10/11명" in Club._not_ready_msg({"filled": 10}, True, "내")


def test_flow():
    asyncio.run(_flow())


if __name__ == "__main__":
    test_flow()
    print("OK: antifarm")
