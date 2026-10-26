# test_340.py — 3.4.0: 남은 시간 글자 · 낚시 100종 · 랜덤 시세 · 확률 · 낚싯대 내구도 · 줄 끊김 · 요리 확률 (테스트 서버 먼저)
# 실행: venv/Scripts/python.exe test_340.py
import asyncio
import random
import tempfile
from pathlib import Path
from types import SimpleNamespace

import release
import services.club_db as cdb
import services.economy_db as edb
import services.player_market_db as pmdb

TMP = Path(tempfile.mkdtemp()) / "t.sqlite3"
edb.DB_PATH = cdb.DB_PATH = pmdb.DB_PATH = TMP

import services.fishing as F   # noqa: E402
from services import ui   # noqa: E402

T0 = 1_800_000_000


def run(c):
    return asyncio.run(c)


def test_switch():
    assert release.preview_340(release.TEST_GUILD)
    assert release.preview_340(123) == release.released_340()


def test_clock():
    assert ui.clock(5) == "5초" and ui.clock(65) == "1분 5초" and ui.clock(3700) == "1시간 1분" and ui.clock(-3) == "0초"


def test_tick_stops():
    edits = []

    class Msg:
        async def edit(self, embed):
            edits.append(embed)

    async def go():
        import time
        alive = iter([True, False])

        async def a():
            return next(alive)
        await ui.tick(Msg(), lambda t: t, time.time() + 30, every=0.01, alive=a)
    run(go())
    assert len(edits) == 1


def test_pool_odds():
    assert len(F.FISH) == 100 and len(F.NEW_340) == 45
    assert not F.NEW_340 & set(sum(F.by_grade(True), []))       # 다른 서버엔 아직 안 잡힌다
    assert not {F.day_fish(T0 + i * 86400) for i in range(300)} & F.NEW_340   # 다른 서버의 오늘의 물고기는 그대로 55종 중에서
    for w in (10, 300, 600):
        old, new = F.grade_odds(w, new=True), F.grade_odds(w, new=True, v340=True)
        assert sum(new[3:]) > sum(old[3:]) and sum(new[2:]) > sum(old[2:])   # 비싼 물고기 확률 소폭 ↑
        assert sum(new[3:]) - sum(old[3:]) < 0.02


def _db():
    return F.FishingDB()


def test_market_random_only():
    async def go():
        db = _db()
        for i in range(3):
            con = db._connect()
            con.execute("INSERT INTO fish_bag(user_id, species, size, caught_ts) VALUES(1,'tuna',200,?)", (T0,))
            con.commit()
            con.close()
        before = (await db.market(T0, True, True))["tuna"]["price"]
        r = await db.sell(1, None, T0, True, True)
        assert r["ok"] and r["gain"] == before * 3                # 판매해도 값이 그대로
        assert (await db.market(T0, True, True))["tuna"]["price"] == before
    run(go())


def test_rod_wear_and_snap():
    async def go():
        db = _db()
        U = 7
        await db.give_item(U, "rod_carbon")
        rng = random.Random(1)
        dur0 = F.ROD_DUR["rod_carbon"]
        s = await db.status(U, T0)
        assert s["rod_dur"] == (dur0, dur0)
        # 한 번 낚을 때마다 1 씩 닳고 0 이 되면 부러진다
        con = db._connect()
        con.execute("INSERT OR REPLACE INTO fish_rod VALUES(?,?,?)", (U, "rod_carbon", 2))
        con.commit()
        con.close()
        outs = []
        for i in range(2):
            await db.cast(U, T0 + i * 1000, None, 1, rng, True, True)
            st = await db.status(U, T0 + i * 1000)
            outs.append(await db.reel(U, st["bite_ts"] + 1, rng, True, True, True, True))
        assert [o["rod"]["broke"] for o in outs] == [False, True]
        assert (await db.status(U, T0))["rod"] is None
        # 대어는 가끔 줄이 끊긴다 (확률 1 로)
        F.LINE_SNAP[None] = 1.0
        try:
            con = db._connect()
            s = db._state(con, U)
            r = None
            for _ in range(500):
                r = db._catch(con, U, s, 600, None, T0, rng, True, True, True, True, wear=True)
                if r["kind"] == "snap":
                    break
            con.close()
            assert r["kind"] == "snap" and r["grade"] >= 3
        finally:
            F.LINE_SNAP[None] = 0.06
    run(go())


def test_cook_chance():
    assert all(F.COOK_CHANCE_340[k] > F.COOK_CHANCE_311[k] for k in F.RECIPES)
    assert all(F.RODS_340[k][0] > F.RODS[k][0] and F.RODS_340[k][1] > F.RODS[k][1] for k in F.RODS)


def test_screens_340():
    import cogs.fishing as cf
    cog = cf.Fishing.__new__(cf.Fishing)
    cog.db, cog.clubs, cog._tasks = F.FishingDB(), cdb.ClubDB(), {}
    cog.bot = SimpleNamespace(get_user=lambda i: None, get_channel=lambda i: None)
    user = SimpleNamespace(id=41, display_name="낚시왕", display_avatar=SimpleNamespace(url="https://x/a.png"),
                           guild=SimpleNamespace(id=release.TEST_GUILD))

    async def go():
        view = cog.menu_view(user)
        for page in range(-1, len(view.pages)):
            view.page = page
            e = await view.build()
            assert len(e) <= 6000 and all(len(f.value) <= 1024 for f in e.fields), page
        me = await cog.market_embed(user)
        assert sum(f.value.count("원") for f in me.fields) == 100
        s = await cog.db.status(41, T0)
        s["cast_ts"] = T0
        assert "던진 지 **" in (await cog._pond_embed(user, s)).description
        for r in ({"kind": "snap", "species": "megalodon", "grade": 4, "wait": 600, "rod": None},
                  {"kind": "junk", "item": "spice", "rod": {"rod": "rod_pro", "dur": 0, "max": 300, "broke": True}}):
            assert cog._result_card(user, r).title
    run(go())
