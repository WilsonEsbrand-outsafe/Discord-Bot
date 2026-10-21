# test_dungeon.py — 2.8 던전: 문 · 힌트(거짓 25%, 횃불이면 0) · 방 6종 · 보스 · 귀환/쓰러짐 정산 · 하루 1회 · 입장권 · 아이템 · 랭킹 · 화면
# 실행: venv/Scripts/python.exe test_dungeon.py
import asyncio
import json
import random
import tempfile
from pathlib import Path
from types import SimpleNamespace

import services.club_db as cdb
import services.economy_db as edb
import services.player_market_db as pmdb

TMP = Path(tempfile.mkdtemp()) / "t.sqlite3"
edb.DB_PATH = cdb.DB_PATH = pmdb.DB_PATH = TMP

import cogs.dungeon as CD   # noqa: E402
import services.dungeon as D   # noqa: E402

DAY = 86400
T0 = 1_800_000_000
AVG = {"atk": 70, "mob": 70, "dfn": 70, "luck": 70}


def test_doors_and_hints():
    rng = random.Random(1)
    st = D.new_run(AVG, 0, rng)
    lies = total = 0
    for _ in range(4000):
        st["floor"] = rng.choice([1, 2, 3, 4, 6, 7, 8, 9])
        doors = D.make_doors(st, rng)
        assert 2 <= len(doors) <= 3
        for d in doors:
            total += 1
            lies += d["hint"] not in D.ROOMS[d["room"]][3]
    assert 0.21 < lies / total < 0.29, lies / total                       # 힌트 약 25% 거짓
    st["torch"] = True
    assert all(d["hint"] in D.ROOMS[d["room"]][3] for _ in range(500) for d in D.make_doors(st, rng))   # 횃불 = 전부 진짜
    for f in D.BOSS_FLOORS:
        st["floor"] = f
        assert [d["room"] for d in D.make_doors(st, rng)] == ["boss"]
    assert "quiz" not in D.ROOMS and len(D.ROOMS) == 6
    assert len(D.THEMES) == 7 and D.theme_of(T0) == (D.theme_of(T0 + DAY) - 1) % 7


def _force(st, room):
    st["doors"] = [{"room": room, "hint": "?"}]
    return 0


def test_rooms():
    rng = random.Random(2)
    st = D.new_run(AVG, 0, rng)
    # 휴식: 체력 회복 후 다음 층
    st["hp"] = 50
    D.enter(st, _force(st, "rest"), rng)
    assert st["hp"] == 80 and st["floor"] == 2 and st["phase"] == "doors"
    # 전투: 싸우면 피해 + 보상, 다음 층
    D.enter(st, _force(st, "battle"), rng)
    assert st["phase"] == "battle"
    D.act(st, "fight", rng)
    assert st["hp"] < 80 and st["coins"] > 0 and st["floor"] == 3
    # 제단: 체력을 바치고 축복 또는 저주
    hp = st["hp"]
    D.enter(st, _force(st, "altar"), rng)
    D.act(st, "pray", rng)
    assert st["hp"] == hp - D.ALTAR_COST and (st["blessed"] or st["cursed"])
    # 상인: 지나가기
    D.enter(st, _force(st, "merchant"), rng)
    assert st["phase"] == "merchant" and len(st["offers"]) == 2
    D.act(st, "leave", rng)
    assert st["floor"] == 5 and st["doors"][0]["room"] == "boss"
    # 보스는 도망칠 수 없다
    D.enter(st, 0, rng)
    assert st["monster"]["boss"]
    D.act(st, "flee", rng)
    assert st["floor"] == 6 or st["phase"] == "dead"
    # 쓰러지면 깃털로 한 번 부활
    st = D.new_run(AVG, 0, rng)
    st["hp"], st["inv_feather"] = 1, True
    D.enter(st, _force(st, "battle"), rng)
    D.act(st, "fight", rng)
    assert st["hp"] == D.FEATHER_HP and st["used"]["feather"] == 1 and st["phase"] == "doors"
    st["hp"] = 1
    D.enter(st, _force(st, "battle"), rng)
    D.act(st, "fight", rng)
    assert st["phase"] == "dead" and st["hp"] == 0
    # 해독제는 함정 피해를 막는다 (기동 0 이라 거의 못 피함)
    st = D.new_run({"atk": 40, "mob": 40, "dfn": 40, "luck": 40}, 3, rng)
    st["inv_antidote"] = True
    while "antidote" not in st["used"]:
        D.enter(st, _force(st, "trap"), rng)
        if st["floor"] in D.BOSS_FLOORS:
            st["floor"] = 1
    assert st["hp"] == st["max_hp"]
    # 쓰러지면 보상 절반
    st = D.new_run(AVG, 0, rng)
    st["loot"], st["coins"], st["xp"], st["phase"] = {"potion": 10}, 100_000, 20, "dead"
    items, coins, xp = D.settle(st, rng)
    assert items["potion"] == 5 and coins == 50_000 and xp == 10
    st["phase"] = "return"
    assert D.settle(st, rng) == ({"potion": 10}, 100_000, 20)


def test_balance():
    res = {o: D.simulate({"atk": o, "mob": o, "dfn": o, "luck": o}, 3000) for o in (55, 70, 85)}
    for o, r in res.items():
        print(f"던전 OVR {o}: 클리어 {r['clear']:.0%} · 쓰러짐 {r['dead']:.0%} · 평균 {r['floor']:.1f}층 · "
              f"아이템 {r['items']:.1f}개 · 돈 {r['coins']:,.0f}원")
    assert res[55]["clear"] < res[70]["clear"] < res[85]["clear"]
    assert res[55]["clear"] < 0.05 and 0.05 < res[70]["clear"] < 0.3 and 0.4 < res[85]["clear"] < 0.75   # 2.10.1: 더 어렵게
    assert res[70]["items"] < 4.5                                          # 2.10.1: 아이템 줄임
    weak = D.simulate({"atk": 20, "mob": 20, "dfn": 20, "luck": 20}, 2000)   # 선수가 없으면 거의 못 깬다
    assert weak["clear"] < 0.01 and weak["floor"] < 4.5
    assert "box" not in D.LOOT_COMMON and "box" not in D.LOOT_RARE and "box" not in D.FIRST_CLEAR
    assert res[85]["coins"] < 400_000                                      # 돈은 적게, 아이템 위주


def test_db():
    db = D.DungeonDB()
    U = 7
    rng = random.Random(3)

    async def flow():
        now = T0
        r = await db.start(U, AVG, now, False, rng)
        assert r["ok"] and r["state"]["floor"] == 1
        assert (await db.start(U, AVG, now, False, rng))["reason"] == "active"
        # 포션: 가방에서 빠진다
        await db.give_item(U, "potion", 2)

        def hurt_and_drink(st, con):
            st["hp"] = 30
            assert st["inv_potion"] == 2
            st["used"]["potion"] = st["used"].get("potion", 0) + 1
            return [f"+{D.drink_potion(st)}"]
        r = await db.step(U, now, hurt_and_drink, rng)
        assert r["state"]["hp"] == 70 and (await db.inventory(U))[0]["potion"] == 1
        r = await db.step(U, now, lambda st, con: [], rng)                  # 다시 저장해도 두 번 빼지 않는다
        assert (await db.inventory(U))[0]["potion"] == 1
        # 귀환 = 보상 전부 · 오늘은 끝
        def rich(st, con):
            st["loot"] = {"spice": 3}
            st["coins"], st["xp"], st["floor"] = 20_000, 8, 3
            return []
        await db.step(U, now, rich, rng)
        bal = await db.get_balance(U)
        r = await db.retreat(U, now, rng)
        assert r["ok"] and r["result"]["items"] == {"spice": 3} and r["result"]["coins"] == 20_000
        assert r["result"]["floor"] == 2 and await db.get_balance(U) == bal + 20_000
        assert (await db.start(U, AVG, now, False, rng))["reason"] == "done"
        # 추가 입장권
        assert not await db.use_ticket(U, now)
        await db.give_item(U, "dungeon_ticket", 1)
        assert await db.use_ticket(U, now)
        info = await db.today(U, now)
        assert info["allowed"] == 2 and info["runs"] == 1
        # 횃불 입장 → 가방에서 1개
        await db.give_item(U, "torch", 1)
        r = await db.start(U, AVG, now, True, rng)
        assert r["state"]["torch"] and (await db.inventory(U))[0].get("torch", 0) == 0
        # 10층 보스를 깨면 클리어 + 첫 클리어 보너스 (테마마다 한 번)
        def clear(st, con):
            st["floor"], st["room"], st["phase"] = 10, "boss", "clear"
            return []
        r = await db.step(U, now, clear, rng)
        assert r["result"]["first"] and r["result"]["items"]["dungeon_ticket"] == 1 and r["result"]["floor"] == 10
        assert await db.clears(U) == {D.theme_of(now)}
        # 다음 날 같은 요일이 아니라도 하루 1회는 새로
        now2 = now + 7 * DAY
        await db.start(U, AVG, now2, False, rng)
        r = await db.step(U, now2, clear, rng)
        assert not r["result"]["first"]                                       # 같은 테마 두 번째는 보너스 없음
        # 방치하면 자동 귀환
        V = 8
        await db.start(V, AVG, now, False, rng)
        assert await db.auto_return(now + D.IDLE_RETURN - 10, rng) == []
        out = await db.auto_return(now + D.IDLE_RETURN + 10, rng)
        assert [u for u, _ in out] == [V] and (await db.today(V, now))["active"] is None
        # 주간 랭킹: 높은 층 먼저
        top = await db.weekly_top(now)
        assert top[0] == (U, 10) and (V, 0) in top
        # 상인: 돈을 내고 바로 가방에, 모자라면 실패
        W = 9
        await db.start(W, AVG, now, False, rng)

        def to_merchant(st, con):
            st["doors"] = [{"room": "merchant", "hint": "?"}]
            return D.enter(st, 0, random.Random(1))
        r = await db.step(W, now, to_merchant, rng)
        item = r["state"]["offers"][0]
        assert (await db.merchant_buy(W, item, now))["reason"] == "balance"
        await db.add_balance(W, 1_000_000)
        r = await db.merchant_buy(W, item, now)
        assert r["ok"] and r["balance"] == 1_000_000 - D.MERCHANT[item] and (await db.inventory(W))[0][item] == 1
        assert (await db.merchant_buy(W, item, now))["reason"] == "invalid"      # 같은 물건은 한 번
        # 유저 초기화 때 던전 기록도 지운다
        res = await db.delete_user(U)
        assert "dungeon_best" in res and "dungeon_daily" in res
    asyncio.run(flow())


def _inter(user, sent, views):
    async def rec(*a, **k):
        sent.append(k.get("embed") or (a[0] if a else None))
        views.append(k.get("view"))
    return SimpleNamespace(user=user, guild_id=None, data={}, message=None,
                           response=SimpleNamespace(send_message=rec, edit_message=rec))


def _limits(v):
    if v is None:
        return
    assert len(v.children) <= 25
    rows = {}
    for c in v.children:
        rows[c.row or 0] = rows.get(c.row or 0, 0) + 1
        assert len(c.label or "") <= 80
    assert all(n <= 5 for n in rows.values()), rows


def test_cog_flow():
    clubs = cdb.ClubDB()
    pm = pmdb.PlayerMarketDB()
    U = 21
    user = SimpleNamespace(id=U, display_name="탐험가", display_avatar=SimpleNamespace(url="https://x/a.png"),
                           guild=SimpleNamespace(id=1))
    cog = CD.Dungeon.__new__(CD.Dungeon)
    cog.bot, cog.db, cog.clubs, cog.rng = None, D.DungeonDB(), clubs, random.Random(5)

    async def flow():
        sent, views = [], []
        inter = _inter(user, sent, views)
        await CD.Dungeon.dungeon.callback(cog, inter)
        assert "구단이 있어야" in sent[-1]                                    # 구단이 없으면 안내
        await clubs.create_club(U, "던전 FC", T0)
        await pm.give_amateur_squad(U)
        await clubs.auto_lineup(U)
        await cog.db.give_item(U, "potion", 1)
        await cog.db.give_item(U, "scroll", 1)
        await cog.db.give_item(U, "dkey", 1)
        await CD.Dungeon.dungeon.callback(cog, inter)
        assert "던전 입구" in sent[-1].title and "`파티`" in sent[-1].description
        entry = views[-1]
        _limits(entry)
        assert [c.label for c in entry.children] == ["입장", "주간 랭킹"]
        await entry.children[0].callback(inter)                              # 입장
        steps = 0
        while views[-1] is not None:
            v = views[-1]
            _limits(v)
            assert sent[-1].description and len(sent[-1].description) < 4096
            labels = [c.label for c in v.children]
            # 두루마리 · 포션 · 열쇠가 있으면 버튼이 보이고, 누르면 하나씩 쓴다
            pick = next((c for c in v.children if c.label.startswith(("두루마리", "포션", "열쇠"))), None)
            if pick is None:
                pick = next(c for c in v.children if not c.label.startswith(("귀환", "도망")))
            await pick.callback(inter)
            steps += 1
            assert steps < 80, labels
        final = sent[-1]
        assert any(w in final.title for w in ("클리어", "쓰러졌어요", "귀환")) and "`아이템`" in final.description
        inv, _ = await cog.db.inventory(U)
        assert "scroll" not in inv or inv["scroll"] == 0
        # 오늘은 끝 → 입구에 버튼은 랭킹만
        await CD.Dungeon.dungeon.callback(cog, inter)
        assert [c.label for c in views[-1].children] == ["주간 랭킹"] and "다 들어갔어요" in sent[-1].description
        assert "이번 주" in (await cog.rank_embed(user)).title
        # 귀환 버튼
        V = 22
        await clubs.create_club(V, "귀환 FC", T0)
        u2 = SimpleNamespace(id=V, display_name="겁쟁이", display_avatar=user.display_avatar, guild=user.guild)
        i2 = _inter(u2, sent, views)
        await CD.Dungeon.dungeon.callback(cog, i2)
        await views[-1].children[0].callback(i2)
        back = next(c for c in views[-1].children if c.label.startswith("귀환"))
        await back.callback(i2)
        assert "귀환" in sent[-1].title and views[-1] is None
        # 추가 입장권은 가방에서 쓴다
        await cog.db.give_item(V, "dungeon_ticket", 1)
        await cog._use_ticket(i2, u2)
        assert "입장권 사용" in sent[-1].title
        await CD.Dungeon.dungeon.callback(cog, i2)
        assert views[-1].children[0].label == "입장"
    asyncio.run(flow())


if __name__ == "__main__":
    test_doors_and_hints()
    test_rooms()
    test_balance()
    test_db()
    test_cog_flow()
    print("OK: dungeon")
