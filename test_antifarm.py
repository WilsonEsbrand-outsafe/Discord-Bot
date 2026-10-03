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
                                           friendly=friendly, v26=True)
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
    # 2.6 전 서버는 예전 그대로
    out = await clubs.record_prospects([([me], 1, 0, 109, 45)], goal, now, rng=NoHurt)
    assert out[0]["xp"] == 21 and out[0]["mods"] == []

    # 부상 결장은 공식경기 판수 (2.6) — 내 구단이 공식경기를 치를 때마다(건 쪽 · 상대 쪽) 1경기씩
    import random

    class Hurt(random.Random):
        def random(self):
            return 0.0   # 부상 확률 통과 · 등급은 첫 번째(경미)
    x = (await clubs.record_prospects([([me], 1, 0, 110, 0)], [], now, rng=Hurt(1), v26=True))[0]
    games = x["injury"]["games"]
    assert x["injury"]["grade"] == "경미" and 2 <= games <= 4 and x["injury"]["until"] == 0
    p = (await clubs.prospects(U, now + 10 ** 6))["active"]                     # 시간이 아무리 지나도
    assert p["injured"] and p["injury_games"] == games
    r = await clubs.record_official(999, U, 0, 1, now, 1_000, "W", 2.0)        # 상대로 뛴 공식경기도 센다
    assert r["rehab"] is None                                                   # (건 쪽 999 는 부상 없음)
    for left in range(games - 2, -1, -1):
        r = await clubs.record_official(U, 999, 1, 0, now, 1_000, "W", 2.0)
        assert r["rehab"] == {"name": "파머", "left": left}
    p = (await clubs.prospects(U, now))["active"]
    assert not p["injured"] and p["injury_games"] == 0
    assert (await clubs.record_official(U, 999, 1, 0, now, 1_000, "W", 2.0))["rehab"] is None

    # 부상 유망주 자리는 경기 때만 벤치 최고 선수가 대신 (선발 명단 · 주장은 그대로)
    await clubs.create_club(U, "파머 FC", now)
    await pmdb.PlayerMarketDB().give_amateur_squad(U)
    await clubs.auto_lineup(U)
    team = await clubs.get_team(U)
    slot = next((s["index"] for s in team["lineup"] if s.get("player_id") == pid), None)
    if slot is None:   # 자동편성에 안 뽑혔으면 공격수 자리에
        slot = next(s["index"] for s in team["lineup"] if s["pos"] == "FW")
        assert (await clubs.set_slot(U, slot, pid))[0]
    await clubs.set_captain(U, pid)
    con = sqlite3.connect(TMP)
    con.execute("UPDATE prospects SET injury_games=3, injury='발목 염좌 (경미)' WHERE user_id=?", (U,))
    con.commit(); con.close()
    plain = await clubs.get_team(U)
    assert plain["filled"] == 10 and plain["lineup"][slot]["injured"] == "파머"
    m = await clubs.match_team(U, True)
    s = m["lineup"][slot]
    assert m["filled"] == 11 and s["sub_for"] == "파머" and s["player_id"] not in {x["player_id"] for x in plain["lineup"]}
    assert m["rating"] > plain["rating"]
    again = await clubs.get_team(U)
    assert again["lineup"][slot].get("injured") == "파머" and again["captain"] == pid      # 저장은 안 바뀐다
    assert (await clubs.match_team(U))["filled"] == 10                                     # 2.6 전 서버는 빈자리

    # 신규 보호: 2.5 뒤 새 유저는 7일 · 공식경기 10판까지 (기존 유저는 없음 · 시작 기록 없는 새 유저는 보호)
    VET, NEW, BLANK = 201, 202, 203
    con = sqlite3.connect(TMP)
    con.execute("INSERT INTO rookie(user_id, start_ts) VALUES(?, 0), (?, ?)", (VET, NEW, now))
    con.commit(); con.close()
    D = 86400
    assert not await clubs.protected(VET, now) and await clubs.protected(BLANK, now)
    assert await clubs.protected(NEW, now) and await clubs.protected(NEW, now + 8 * D)       # 7일 지나도 10판 전
    for _ in range(cdb.NEWBIE_OFFICIAL):
        await clubs.record_official(NEW, VET, 1, 0, now, 1_000, "W", 2.0)
    assert await clubs.protected(NEW, now) and not await clubs.protected(NEW, now + 8 * D)

    from types import SimpleNamespace
    sent = []

    async def rec(*a, **k):
        sent.append((a, k))
    cog = Club.__new__(Club)
    cog.clubs = clubs
    member = lambda i: SimpleNamespace(id=i, display_name=f"u{i}")                              # noqa: E731
    inter = lambda g: SimpleNamespace(guild_id=g, response=SimpleNamespace(send_message=rec))   # noqa: E731
    import release
    T = release.TEST_GUILD
    assert await cog._shielded(inter(T), member(VET), member(BLANK)) and "신규 보호" in sent[-1][0][0]
    assert not await cog._shielded(inter(T), member(BLANK), member(VET))          # 신규가 먼저 거는 건 된다
    assert not await cog._shielded(inter(T), member(NEW), member(BLANK))          # 신규끼리는 된다
    assert not await cog._shielded(inter(None), member(VET), member(BLANK))       # 2.6 전 서버는 그대로

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
