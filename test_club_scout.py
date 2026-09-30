# test_club_scout.py — 구단(포메이션·선발·친선경기) / 스카우트 검증.
# 실행: venv/Scripts/python.exe test_club_scout.py
import asyncio
import random
import sqlite3
import tempfile
from pathlib import Path

import services.club_db as cdb
import services.economy_db as edb
import services.player_market_db as pmdb

TMP = Path(tempfile.mkdtemp()) / "t.sqlite3"
edb.DB_PATH = cdb.DB_PATH = pmdb.DB_PATH = TMP

from cogs.economy import Economy  # noqa: E402
from services.club_db import (  # noqa: E402
    FORMATIONS, SLOT_GROUP, ClubDB, auto_assign, effective_ovr, simulate_match, team_rating,
)

NOW = 1_800_000_000
U1, U2 = 111, 222


def run(coro):
    return asyncio.run(coro)


async def _setup():
    eco = edb.EconomyDB()
    pm = pmdb.PlayerMarketDB()
    clubs = ClubDB()
    await pm.ensure_bootstrap(NOW)
    await pm.ensure_active_pool(NOW)
    return eco, pm, clubs


def test_formations_and_ratings():
    for name, slots in FORMATIONS.items():
        assert len(slots) == 11 and slots[0] == "GK", name
        assert all(s in SLOT_GROUP for s in slots), name
    assert effective_ovr(80, "FW", "ST") == 80
    assert effective_ovr(80, "MF", "ST") == 74 and effective_ovr(80, "DF", "ST") == 68
    assert effective_ovr(80, "FW", "GK") == 50 and effective_ovr(80, "GK", "CB") == 50

    players = [{"player_id": f"p{i}", "pos": pos, "ovr": 60 + i, "nation": "대한민국"}
               for i, pos in enumerate(["GK"] * 2 + ["DF"] * 5 + ["MF"] * 5 + ["FW"] * 4)]
    placed = auto_assign(players, "4-4-2")
    assert len(placed) == 11 and len(set(placed)) == 11 and None not in placed
    by_id = {p["player_id"]: p for p in players}
    for pid, slot in zip(placed, FORMATIONS["4-4-2"]):
        assert by_id[pid]["pos"] == SLOT_GROUP[slot], (pid, slot)       # 인원이 충분하면 전원 제 포지션

    lineup = [{"slot": s, "player_id": pid, **by_id[pid]} for s, pid in zip(FORMATIONS["4-4-2"], placed)]
    r = team_rating(lineup, captain=placed[-1])
    assert r["filled"] == 11 and r["captain_bonus"] == 1 and r["chem"] == 1   # 같은 국적 11명 = 묶음 1개
    empty = team_rating([{"slot": s, "player_id": None} for s in FORMATIONS["4-4-2"]], None)
    assert empty["rating"] == 30 and empty["filled"] == 0


def test_simulate_match_favors_stronger_team():
    xi = lambda ovr: [{"name": f"p{i}", "pos": p, "ovr": ovr}                       # noqa: E731
                      for i, p in enumerate(["GK", "DF", "DF", "MF", "MF", "FW", "FW"])]
    strong, weak = {"name": "A", "rating": 85, "xi": xi(85)}, {"name": "B", "rating": 55, "xi": xi(55)}
    rng = random.Random(1)
    wins = sum(1 for _ in range(400) if (m := simulate_match(strong, weak, rng))["home"] > m["away"])
    assert wins > 280, wins
    m = simulate_match(strong, weak, random.Random(2))
    assert m["home"] + m["away"] == len(m["goals"])
    assert [g["minute"] for g in m["goals"]] == sorted(g["minute"] for g in m["goals"])


async def _club_flow():
    eco, pm, clubs = await _setup()

    ok, _, bonus = await clubs.create_club(U1, "  테스트 FC  ", NOW)
    assert ok and bonus
    assert (await clubs.get_club(U1))["name"] == "테스트 FC"
    assert not (await clubs.create_club(U1, "또", NOW))[0]                       # 중복 생성 불가
    assert not (await clubs.create_club(U2, "가" * 31, NOW))[0]                  # 30자 제한
    # 2.00 이전에 만든 구단처럼 선발 명단이 비어 있고 주장도 없는 상태 — 화면이 깨지면 안 된다
    from types import SimpleNamespace
    from cogs.club import _team_embed
    owner = SimpleNamespace(id=U1, display_name="구단주", display_avatar=SimpleNamespace(url="https://x/a.png"))
    team = await clubs.get_team(U1)
    assert team["filled"] == 0 and team["captain"] is None
    e = _team_embed(team, owner)
    assert "주장` 없음" in e.description and "/자동편성" in e.description
    assert (await clubs.set_formation(U1, "4-3-3"))[0]                          # 빈 명단 포메이션 변경
    _team_embed(await clubs.get_team(U1), owner)
    assert (await clubs.set_formation(U1, "4-4-2"))[0]

    await pm.give_amateur_squad(U1)
    assert (await clubs.auto_lineup(U1))[0]
    team = await clubs.get_team(U1)
    assert team["filled"] == 11 and team["formation"] == "4-4-2"
    assert (await clubs.set_slot(U1, 5, None))[0]                               # 빈자리 + 주장 없음
    _team_embed(await clubs.get_team(U1), owner)
    assert (await clubs.auto_lineup(U1))[0]

    ok, _ = await clubs.set_formation(U1, "3-5-2")
    team = await clubs.get_team(U1)
    assert ok and team["formation"] == "3-5-2" and team["filled"] == 11        # 선발 11명은 그대로 재배치

    gk = team["lineup"][0]["player_id"]
    ok, _ = await clubs.set_slot(U1, 10, gk)                                     # GK 를 ST 자리로 옮기면
    team = await clubs.get_team(U1)
    assert ok and team["lineup"][0]["player_id"] is None and team["lineup"][10]["player_id"] == gk
    assert not (await clubs.set_slot(U1, 3, "없는선수"))[0]
    assert (await clubs.set_slot(U1, 3, None))[0]

    assert not (await clubs.set_captain(U1, "없는선수"))[0]
    assert (await clubs.set_captain(U1, gk))[0]
    assert (await clubs.get_team(U1))["captain_bonus"] == 1

    # 명단에 있던 선수를 잃으면(팔거나 은퇴) 자리는 자동으로 비고, 주장도 풀린다.
    con = sqlite3.connect(TMP)
    con.execute("UPDATE pm_holdings SET qty=0 WHERE user_id=? AND player_id=?", (U1, gk))
    con.commit(); con.close()
    team = await clubs.get_team(U1)
    assert gk not in {s.get("player_id") for s in team["lineup"]} and team["captain"] is None

    await clubs.record_match(U1, U2, 2, 1)
    assert (await clubs.get_club(U1))["wins"] == 1

    # 삭제 → 재창단: 보너스는 다시 안 준다, 아마추어 스쿼드는 새로 받는다
    assert await clubs.delete_club(U1)
    assert await clubs.get_club(U1) is None and await clubs.get_team(U1) is None
    assert not await clubs.delete_club(U1)
    con = sqlite3.connect(TMP)
    amt = con.execute("SELECT COUNT(*) FROM pm_holdings WHERE user_id=? AND player_id LIKE 'AMT_%'", (U1,)).fetchone()[0]
    con.close()
    assert amt == 0
    ok, _, bonus = await clubs.create_club(U1, "다시 FC", NOW)
    assert ok and not bonus


def test_club_flow():
    run(_club_flow())


async def _scout_flow():
    eco, pm, clubs = await _setup()
    econ = Economy.__new__(Economy)

    # 발굴 등급은 레벨이 높을수록 희귀 쪽으로
    con = sqlite3.connect(TMP)
    rng = random.Random(3)
    rare_rate = []
    for lv in (1, 5):
        hits = [pmdb.scout_find_player(con, lv, rng) for _ in range(600)]
        assert all(h for h in hits)
        for h in hits:
            _, lo, hi = pmdb.SCOUT_TIERS[h["tier_index"]]
            assert h["price"] >= lo and (hi is None or h["price"] < hi)
        rare_rate.append(sum(h["tier_index"] >= 2 for h in hits) / 600)
    con.close()
    assert rare_rate[1] > rare_rate[0], rare_rate
    assert list(pmdb.SCOUT_FIND_PROB) == sorted(pmdb.SCOUT_FIND_PROB)

    # 발굴되면 같은 트랜잭션에서 선수 카드가 지급된다
    old = pmdb.SCOUT_FIND_PROB
    import cogs.economy as ce
    ce.SCOUT_FIND_PROB = (1.0,) * 5
    try:
        for i in range(20):
            last_ts = NOW + 60 * i
            r = await eco.play_scout(U2, last_ts, lambda lv, con: econ._scout_roll(lv, con, U2))
            assert r["ok"]
            if r["info"]["ok"]:
                assert r["info"]["found"] and r["delta"] > 0
                break
        found = r["info"]["found"]
        assert await pm.get_holding(U2, found["player_id"]) >= 1
    finally:
        ce.SCOUT_FIND_PROB = old

    # 하루 15회 · 쿨타임 60초 · 최대 Lv.5
    assert edb.SCOUT_DAILY_LIMIT == 15 and edb.SCOUT_MAX_LEVEL == 5
    day2 = NOW + 86400 * 3
    for i in range(15):
        assert (await eco.play_scout(U1, day2 + 60 * i, lambda lv, con: (0, 0, None)))["ok"], i
    assert (await eco.play_scout(U1, day2 + 60 * 15, lambda lv, con: (0, 0, None)))["reason"] == "limit"
    assert (await eco.play_scout(U2, last_ts + 10, lambda lv, con: (0, 0, None)))["reason"] == "cooldown"
    r = await eco.play_scout(U1, day2 + 86400, lambda lv, con: (0, 10_000, None))
    assert r["level"] == 5 and r["xp"] == 0
    assert Economy.scout_money_mult(5) == 5 and Economy.scout_tier(5).endswith("전설의 스카우트")
    # Lv.2→3, Lv.4→5 는 앞 구간보다 확실히 길다
    need = edb.SCOUT_XP_NEED
    assert need == tuple(sorted(need)) and need[1] >= 600 and need[3] >= 3000, need


def test_scout_flow():
    run(_scout_flow())


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
