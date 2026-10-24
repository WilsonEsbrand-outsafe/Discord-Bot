# test_311_misc.py — 3.1.1: 명령어 연타 막기 · 양궁 아이템 삭제/환불 · 훈련 레벨 간격 · 배율 · 던전 너프 · F1 손실 · 상점
# 실행: venv/Scripts/python.exe test_311_misc.py
import asyncio
import random
import tempfile
from pathlib import Path

import services.economy_db as edb

TMP = Path(tempfile.mkdtemp()) / "t.sqlite3"
edb.DB_PATH = TMP

import release   # noqa: E402
from services import spam   # noqa: E402

T, OTHER = release.TEST_GUILD, 757761125403066419


def test_spam():
    u = 1
    assert spam.check(u, "출석", 100.0) and not spam.check(u, "출석", 100.5)      # 같은 명령어 1.5초 안 ✕
    assert spam.check(u, "출석", 102.0)
    assert all(spam.check(u, f"c{i}", 103.0 + i * 0.1) for i in range(4))         # 10초 안 6번까지
    assert not spam.check(u, "c9", 104.0)
    assert spam.check(u, "c9", 112.5)                                              # 창이 지나면 다시
    assert all(spam.check(2, "페널티킥", 200.0) for _ in range(10))                # 자체 도배 방지가 있는 게임은 뺀다


def test_archery_items():
    assert not any(edb.shop_open(k, guild_id=T) for k in edb.ARCHERY_ITEMS)
    if release.RELEASE_311_TS is None:
        assert all(edb.shop_open(k, guild_id=OTHER) for k in edb.ARCHERY_ITEMS)
    db = edb.EconomyDB()

    async def flow():
        await edb.EconomyDB().add_balance(5, 0)
        await db._tx(lambda con: [edb.give_item(con, 5, "bow_pro", 2), edb.give_item(con, 5, "focus"),
                                  edb.give_item(con, 6, "windbreaker"), edb.give_item(con, 6, "bait")])
        assert await db.refund_archery() == [(5, 2 * 300_000 + 500_000), (6, 300_000)]
        assert await db.get_balance(5) == 1_100_000 and await db.get_balance(6) == 300_000
        assert await db.refund_archery() == []                                    # 한 번만
        inv, _ = await db.inventory(6)
        assert inv == {"bait": 1}
    asyncio.run(flow())
    import services.dungeon as D
    assert not set(edb.ARCHERY_ITEMS) & set(D.LOOT_RARE_311)


def test_training():
    from cogs.economy import Economy
    assert edb.grind_xp_need("training", 1, True) == 2 * edb.grind_xp_need("training", 1)
    assert edb.grind_xp_need("scouting", 1, True) == edb.grind_xp_need("scouting", 1)   # 스카우트는 그대로
    assert edb.grind_add_xp("training", 1, 0, 100) == (2, 40, 1) and edb.grind_add_xp("training", 1, 0, 100, True) == (1, 100, 0)
    assert Economy.train_money_mult(10) == 10 and Economy.train_money_mult(10, True) == 5.5
    assert Economy.train_money_mult(1, True) == 1


def test_dungeon_nerf():
    import services.dungeon as D
    st = {"atk": 75, "mob": 75, "dfn": 75, "luck": 75}
    old, new = D.simulate(st, 1500), D.simulate(st, 1500, v311=True)
    assert new["items"] < old["items"] * 0.6 and new["clear"] < old["clear"]


def test_f1_loss():
    import cogs.race as R
    grid = R.f1_qualify(False, random.Random(1))
    c = R.f1_counts(grid, R.CIRCUITS[0], False, 2000, random.Random(2))
    deep = R.f1_ladder(c, deep=True)
    assert all(lad[4] == -R.F1_RETIRE_311 for lad in deep)                        # 리타이어 3배 손실
    top = deep[grid[0]]
    assert min(top) <= -2                                                          # 강팀이 크게 밀리면 2배 손실
    assert "💥 -3" in R.f1_ladder_text(top) and "💥" not in R.f1_ladder_text(R.f1_ladder(c)[grid[0]])


def test_lawyer_anytime():
    db = edb.EconomyDB()

    async def flow():
        seen = set()
        for k in range(60):
            uid = 100 + k
            await db.add_balance(uid, -1_000_000)
            await db._tx(lambda con: edb.give_item(con, uid, "lawyer"))
            assert (await db.hire_lawyer(uid, uid))["ok"]
            r = await db.declare_bankruptcy(uid, 1_000_000 + k, random.Random(k), new=True, v311=True)
            # 변호사가 있으면 100%가 아닌 결과는 언제나 다시 변론 → 0% 는 남지 않는다 · 더 나은 쪽
            assert r["rate"] > 0
            if r["lawyer"] is True:
                assert r["rate"] >= r["before"] and r["before"] < 1
                seen.add(r["before"] > 0)
                assert await db.lawyers(uid) == 0
            else:
                assert r["rate"] == 1.0 and await db.lawyers(uid) == 1   # 처음부터 100% 면 변호사는 남는다
        assert seen == {True, False}   # 0% 에서도, 30~70% 에서도 변론했다
    asyncio.run(flow())


def test_fishing_311():
    import services.fishing as F
    db = F.FishingDB()
    U = 900

    class Rng(random.Random):
        def __init__(self, x):
            super().__init__(1)
            self.x = x

        def random(self):
            return self.x

    async def flow():
        def put(con, species, n):
            return [con.execute("INSERT INTO fish_bag(user_id, species, size, caught_ts) VALUES(?,?,?,?)",
                                (U, species, 20.0, 1_000)).lastrowid for _ in range(n)]
        ids = await db._tx(lambda con: put(con, "crucian", 5) + put(con, "jacopever", 1))
        # 예전: 흔함 2마리 자동 · 3.1.1: 4마리를 직접 골라야
        assert (await db.cook(U, "grill", 2_000, ids=ids[:2], new=True))["reason"] == "pick"
        assert (await db.cook(U, "grill", 2_000, ids=ids[:3] + [ids[5]], new=True))["reason"] == "pick"   # 등급이 다른 물고기
        r = await db.cook(U, "grill", 2_000, ids=ids[:4], new=True, rng=Rng(0.99))   # 실패 → 재료만 사라진다
        assert r["ok"] and r["burnt"] and len(r["used"]) == 4 and "grill" not in await db.buffs(U, 2_000)
        ids2 = await db._tx(lambda con: put(con, "crucian", 4))
        r = await db.cook(U, "grill", 2_000, ids=ids2, new=True, rng=Rng(0.0))
        assert r["ok"] and not r["burnt"] and "grill" in await db.buffs(U, 2_000)
        left = await db.bag(U, 2_000)
        assert sorted(f["id"] for f in left["fish"]) == [ids[4], ids[5]]
        # 화면: ① 요리 → ② 물고기 4마리 고르기 (정확히 4마리)
        import cogs.fishing as cf
        import services.club_db as cdb
        from types import SimpleNamespace
        cdb.DB_PATH = edb.DB_PATH
        cog = cf.Fishing.__new__(cf.Fishing)
        cog.db, cog.clubs, cog._tasks = db, cdb.ClubDB(), {}
        user = SimpleNamespace(id=U, display_name="요리사", display_avatar=SimpleNamespace(url="https://x/a.png"),
                               guild=SimpleNamespace(id=T))
        await db._tx(lambda con: put(con, "crucian", 4))
        view = cog.menu_view(user, [p.title for p in cog.menu_view(user).pages].index("요리"))
        e = await view.build()
        assert "성공" in e.description and len([c for c in view.children if isinstance(c, cf.discord.ui.Select)]) == 1
        view.ctx["recipe"] = "grill"
        await view.build()
        sel = [c for c in view.children if isinstance(c, cf.discord.ui.Select)]
        assert len(sel) == 2 and sel[1].min_values == sel[1].max_values == 4
    asyncio.run(flow())
    assert F.ROD_RARE_311["rod_legend"] > F.ROD_RARE_311["rod_pro"] > F.ROD_RARE_311["rod_carbon"] > 0


if __name__ == "__main__":
    test_fishing_311()
    test_lawyer_anytime()
    test_spam()
    test_archery_items()
    test_training()
    test_dungeon_nerf()
    test_f1_loss()
    print("✅ test_311_misc 통과")
