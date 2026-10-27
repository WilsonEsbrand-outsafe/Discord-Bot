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


def test_prospect_positions():
    """3.5.0: 포지션별 경험치 — 킬패스 · 태클 · 선방 장면 · 무실점 · 미드필더 도움 = 골 · 부진 판정."""
    import sqlite3
    import time

    class NoHurt:
        @staticmethod
        def random():
            return 1.0

    async def flow():
        now = int(time.time())
        db, clubs = edb.EconomyDB(), cdb.ClubDB()
        pids = {}
        for uid, pos in ((501, "CM"), (502, "ST"), (503, "CB"), (504, "GK")):
            await db.add_balance(uid, 10_000_000)
            info, _ = cdb.prospect_input("테스트", "대한민국", pos, 8, "1-1")
            pids[pos] = (await clubs.create_prospect(uid, info, now))["pid"]
        con = sqlite3.connect(TMP)
        con.execute("UPDATE prospects SET pro=10, proneness=1, pot=99")
        con.commit(); con.close()
        opp = iter(range(600, 700))

        async def play(pid, gf=1, ga=0, gap=0, goals=(), events=(), v350=True):
            return (await clubs.record_prospects([([{"player_id": pid}], gf, ga, next(opp), gap)], list(goals), now,
                                                 list(events), rng=NoHurt, v350=v350))[0]
        mf, fw, df, gk = pids["CM"], pids["ST"], pids["CB"], pids["GK"]
        X = cdb.PROSPECT_XP_350
        assist = [{"scorer_id": "x", "assist_id": mf}]
        assert (await play(mf, goals=assist, v350=False))["xp"] == 10 + 5 + 4
        assert (await play(mf, goals=assist))["xp"] == 10 + 5 + 6                     # 도움 = 골만큼
        x = await play(mf, events=[{"player_id": mf, "kind": "key_pass"}] * 2)
        assert x["xp"] == 15 + 2 * X["key_pass"] and x["plus"] == ["key_pass"]
        assert (await play(mf, gap=45))["xp"] == -10                                  # 약팀 상대 아무것도 못 함
        assert (await play(mf, gap=45, events=[{"player_id": mf, "kind": "key_pass"}]))["xp"] == 0   # 킬패스면 부진 아님
        x = await play(df, events=[{"player_id": df, "kind": "tackle"}])
        assert x["xp"] == 15 + X["tackle"] + X["clean_DF"] and x["plus"] == ["tackle", "clean"]
        assert (await play(df, ga=1, gf=2))["xp"] == 15                               # 실점하면 무실점 보너스 없음
        x = await play(gk, events=[{"player_id": gk, "kind": "save"}] * 3)
        assert x["xp"] == 15 + 3 * X["save"] + X["clean_GK"]
        assert (await play(gk, v350=False))["xp"] == 15                               # 예전 서버는 그대로
        assert (await play(fw))["xp"] == 15 and (await play(fw, gap=45))["xp"] == -10   # 공격수는 그대로
    asyncio.run(flow())


def test_highlights_plays():
    team = lambda: {"name": "t", "xi": [{"name": f"{g}{i}", "pos": g, "player_id": f"{g}{i}", "ovr": 60}
                                         for i, g in enumerate(("GK", "DF", "DF", "MF", "MF", "FW"))]}
    res = {"goals": []}
    hl = cdb.match_highlights(res, team(), team(), random.Random(1), v350=True)
    kinds = {h["kind"] for h in hl if h.get("kind") in ("key_pass", "tackle", "save")}
    assert kinds
    for h in hl:
        if h.get("kind") in ("key_pass", "tackle", "save"):
            assert h["player_id"].startswith({"key_pass": "MF", "tackle": "DF", "save": "GK"}[h["kind"]])
    old = cdb.match_highlights(res, team(), team(), random.Random(1))
    assert not any(h.get("kind") in ("key_pass", "tackle", "save") for h in old)

if __name__ == "__main__":
    test_prospect_positions()
    test_highlights_plays()
    test_fishing_rare_up()
    test_release_switch()
    test_table()
    test_bankrupt_rates()
    print("✅ test_350 통과")
