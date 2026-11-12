"""3.8.1 🌾 /농사: 땅 고르기 · 심기 · 수확 · 동물 · 합치기 · 확장 · 낫 강화 · 상자 · 환전 (테스트 서버 먼저)."""
import asyncio
import random
import sqlite3
import tempfile
from pathlib import Path

import services.economy_db as edb

edb.DB_PATH = Path(tempfile.mkdtemp()) / "t.sqlite3"

import services.farm as F  # noqa: E402

T = 1_800_000_000


class Fixed:
    """정해진 값만 내는 rng (random() · randint · choice · choices)."""

    def __init__(self, x: float):
        self.x = x

    def random(self):
        return self.x

    def randint(self, a, b):
        return a

    def choice(self, seq):
        return seq[0]

    def choices(self, seq, weights=None):
        return [seq[0]]


def run(coro):
    return asyncio.run(coro)


def test_tables():
    assert all(sum(v[3].values()) == 12 for v in F.LANDS.values())          # 땅마다 자원 합이 같다
    assert F.time_mult(5) < F.time_mult(3) == 1 < F.time_mult(1)
    assert F.CROPS["carrot"][2] == 0                                         # 무료 씨앗이 있어야 코인 0 에서도 안 막힌다
    for k in F.ITEMS:                                                        # 합치면 3개 값보다 비싸다
        assert all(F.value(k, t + 1) > 3 * F.value(k, t) for t in range(1, F.TIER_MAX))
    assert [F.expand_cost(p) for p in (6, 7, 8)] == [150, 400, 750]
    assert F.extra_p(10) > F.extra_p(0) and F.high_p(10) > F.high_p(0) and F.chest_p(5) > F.chest_p(1)
    for tier in F.CHESTS:
        assert len(F.roll_chest(tier, False, random.Random(1))) == F.CHEST_REWARDS[tier]
    rng = random.Random(7)                                                   # 행운 열기는 평균 값이 더 크다
    plain = sum(r["worth"] for _ in range(400) for r in F.roll_chest("silver", False, rng))
    lucky = sum(r["worth"] for _ in range(400) for r in F.roll_chest("silver", True, rng))
    assert lucky > plain * 1.15
    for tier, (t, pool) in F.CHEST_ITEMS.items():
        assert 1 <= t <= F.TIER_MAX and all(k in F.ITEMS for k in pool)


def test_start_and_free_play():
    """돈 · 코인이 0 이어도: 무료 당근 → 수확 → 판매 → 코인."""
    db = F.FarmDB()

    async def flow():
        U = 9001
        assert await db.farms_of(U, T) == [] and await db.get(12345, T) is None
        assert (await db.create(U, "moon", T))["reason"] == "land"
        c = await db.create(U, "plain", T)
        G = c["id"]
        again = await db.create(U, "river", T)                                # 첫 농장만 /농사 로 연다 (더는 인수로)
        assert c["ok"] and again["reason"] == "exists" and again["id"] == G
        assert [x["id"] for x in await db.farms_of(U, T)] == [G]
        assert (await db.plant(U + 1, G, "carrot", T))["reason"] == "none"    # 주인만 심는다
        f = await db.get(G, T)
        assert (f["coins"], f["energy"], f["plots"], f["balance"], f["chests"]["wood"]) == (0, 100, 6, 0, 1)
        assert (await db.plant(U, G, "corn", T))["reason"] == "seeds"         # 씨앗은 농사 상점에서 산다
        assert (await db.buy(U, G, "corn", 1, T))["reason"] == "coins"
        r = await db.plant(U, G, "carrot", T)                                 # 당근은 씨앗 없이
        assert r["ok"] and r["count"] == 6 and r["left"] is None
        assert r["ready_ts"] == T + int(F.CROPS["carrot"][3] * F.time_mult(5))   # 평야: 비옥도 5 → 빨리 자란다
        assert (await db.plant(U, G, "carrot", T))["reason"] == "full"
        assert (await db.harvest(U, G, T + 1, Fixed(0.99)))["reason"] == "nothing"
        h = await db.harvest(U, G, r["ready_ts"], Fixed(0.99))
        assert h["ok"] and h["got"] == {("carrot", 1): 6} and not h["chests"] and h["extra"] == h["high"] == 0
        assert h["daily"] and (await db.get(G, r["ready_ts"]))["chests"]["wood"] == 2      # 하루 첫 거두기 선물
        await db.plant(U, G, "carrot", r["ready_ts"])
        assert not (await db.harvest(U, G, r["ready_ts"] + 600, Fixed(0.99)))["daily"]       # 같은 날엔 한 번만
        assert (await db.harvest(U, G, r["ready_ts"] + 600, Fixed(0.99)))["reason"] == "nothing"
        s = await db.sell(U, G, T)
        assert s["ok"] and s["coins"] == 12 * 4 and (await db.sell(U, G, T))["reason"] == "empty"
        assert (await db.get(G, T))["balance"] == 0                           # 축구 봇 돈은 건드리지 않는다
    run(flow())


def test_plant_n_energy_and_luck():
    db = F.FarmDB()

    async def flow():
        U = 9002
        G = (await db.create(U, "mountain", T))["id"]
        await db.set_plant_n(U, G, 2)
        assert (await db.plant(U, G, "carrot", T))["count"] == 2
        await db.set_plant_n(U, G, 0)
        con = sqlite3.connect(edb.DB_PATH)
        con.execute("UPDATE farms SET energy=3, energy_ts=?, sickle=10 WHERE id=?", (T, G))
        con.commit(); con.close()
        assert (await db.plant(U, G, "carrot", T))["count"] == 3                 # 에너지가 되는 만큼만
        assert (await db.plant(U, G, "carrot", T))["reason"] == "energy"
        late = T + 100                                                        # 100초 뒤: 에너지가 다시 찼다 (5초에 1)
        f = await db.get(G, late)
        assert f["energy"] == int(100 / (F.ENERGY_SEC * F.time_mult(3))) == 20 and f["energy_next"] > late
        h = await db.harvest(U, G, late, Fixed(0.0))                             # 운이 전부 터지면: ★2 · 2개씩 · 상자
        assert h["got"] == {("carrot", 2): 10} and h["extra"] == h["high"] == 5 and h["chests"] == ["wood"] * 5
        assert h["daily"] and (await db.get(G, late))["chests"]["wood"] == 1 + 5 + 1
    run(flow())


def test_animals_merge_expand():
    db = F.FarmDB()

    async def flow():
        U = 9003
        G = (await db.create(U, "meadow", T))["id"]
        con = sqlite3.connect(edb.DB_PATH)
        con.execute("UPDATE farms SET coins=100000 WHERE id=?", (G,))
        con.commit(); con.close()
        assert (await db.plant(U, G, "chicken", T))["reason"] == "seeds"      # 동물도 상점에서 사 둔 것만
        b = await db.buy(U, G, "chicken", 1, T)
        assert b["ok"] and b["cost"] == 300 and b["coins"] == 100000 - 300 and b["have"] == 1
        for k in ("cow", "sheep"):
            await db.buy(U, G, k, 1, T)
        a = await db.plant(U, G, "chicken", T)
        assert a["ok"] and a["animal"] and a["count"] == 1 and a["left"] == 0
        assert a["ready_ts"] == T + int(F.ANIMALS["chicken"][4] * F.time_mult(5))   # 목초지: 동물이 빨리 만든다
        assert (await db.plant(U, G, "cow", T))["ok"]
        assert (await db.plant(U, G, "sheep", T))["reason"] == "cap"             # 땅 3칸당 1마리 (6칸 → 2마리)
        assert (await db.get(G, T))["seeds"] == {"sheep": 1}                  # 못 넣은 양은 그대로 남는다
        h = await db.harvest(U, G, a["ready_ts"], Fixed(0.99))
        assert h["got"] == {("egg", 1): 1}
        f = await db.get(G, a["ready_ts"])
        assert f["fields"][0]["kind"] == "animal" and not f["fields"][0]["ready"]   # 동물은 남아서 다시 만든다
        # 합치기: 3개 → 한 등급 위 1개
        con = sqlite3.connect(edb.DB_PATH)
        con.execute("INSERT OR REPLACE INTO farm_items VALUES(?,?,?,?)", (G, "corn", 1, 7))
        con.commit(); con.close()
        m = await db.merge(U, G, "corn", 1, T)
        assert m["ok"] and m["made"] == 2 and m["tier"] == 2
        items = {(k, t): q for k, t, q in (await db.get(G, T))["items"]}
        assert items[("corn", 1)] == 1 and items[("corn", 2)] == 2
        assert (await db.merge(U, G, "corn", 2, T))["reason"] == "few" and (await db.merge(U, G, "corn", 5, T))["reason"] == "max"
        s = await db.sell(U, G, T, [("corn", 1), ("corn", 2)], 1)               # 11/12 몇 개 팔지 고르기: 고른 것마다 1개씩
        assert s["coins"] == F.value("corn", 1) + F.value("corn", 2) and sorted(s["sold"]) == [("corn", 1, 1), ("corn", 2, 1)]
        items = {(k, t): q for k, t, q in (await db.get(G, T))["items"]}
        assert ("corn", 1) not in items and items[("corn", 2)] == 1             # 1개뿐이던 건 다 팔렸다
        s = await db.sell(U, G, T, [("corn", 2)], 10)                           # 가진 것보다 많이 고르면 가진 만큼
        assert s["coins"] == F.value("corn", 2) and s["sold"] == [("corn", 2, 1)]
        # 확장: 코인 → 한 칸 + 은 상자 · 동물 한도도 같이
        before = (await db.get(G, T))["coins"]
        for _ in range(3):
            e = await db.expand(U, G, T)
        f = await db.get(G, T)
        assert e["plots"] == 9 and f["animal_cap"] == 3 and f["chests"]["silver"] == 3
        assert before - f["coins"] == 150 + 400 + 750
        x = await db.sell_animal(U, G, 0, T)
        assert x["ok"] and x["coins"] == 150 and (await db.sell_animal(U, G, 0, T))["reason"] == "slot"
    run(flow())


def test_sickle_chest_exchange():
    db = F.FarmDB()

    async def flow():
        U = 9004
        G = (await db.create(U, "river", T))["id"]
        assert (await db.enhance(U, G, T))["reason"] == "coins"
        assert (await db.enhance(U, G, T, use_whet=True))["reason"] == "whet"
        con = sqlite3.connect(edb.DB_PATH)
        con.execute("UPDATE farms SET coins=5000, whet=2, c_gold=2 WHERE id=?", (G,))
        con.commit(); con.close()
        assert (await db.enhance(U, G, T, charm=True))["reason"] == "money"      # 행운 부적은 축구 봇 돈
        r = await db.enhance(U, G, T, rng=Fixed(0.0))
        assert r["ok"] and r["success"] and r["level"] == 1
        r = await db.enhance(U, G, T, rng=Fixed(0.999))                          # 실패해도 단계는 그대로
        assert r["ok"] and not r["success"] and r["level"] == 1
        f = await db.get(G, T)
        assert f["sickle"] == 1 and f["coins"] == 5000 - 150 - 600
        await db.add_balance(U, 10_000_000)
        r = await db.enhance(U, G, T, use_whet=True, charm=True, rng=Fixed(0.95))   # 90% + 10%p → 성공
        assert r["success"] and r["fee"] == F.CHARM_FEE * 2
        f = await db.get(G, T)
        assert f["whet"] == 0 and f["balance"] == 10_000_000 - F.CHARM_FEE * 2
        # 상자: 그냥 열기 · 행운 열기(돈)
        c = await db.open_chest(U, G, "gold", T, rng=random.Random(3))
        assert c["ok"] and len(c["rewards"]) == 3 and c["fee"] == 0
        c = await db.open_chest(U, G, "gold", T, lucky=True, rng=random.Random(4))
        assert c["ok"] and c["fee"] == F.LUCKY_FEE["gold"]
        assert (await db.open_chest(U, G, "gold", T))["reason"] == "empty"
        f = await db.get(G, T)
        assert f["balance"] == 10_000_000 - F.CHARM_FEE * 2 - F.LUCKY_FEE["gold"]
        # 환전: 코인 → 돈
        assert (await db.exchange(U, G, f["coins"] + 1, T))["reason"] == "coins"
        assert (await db.exchange(U, G, 0, T))["reason"] == "amount"
        x = await db.exchange(U, G, 100, T)
        assert x["ok"] and x["won"] == 100 * F.RATE and x["balance"] == f["balance"] + 100 * F.RATE
    run(flow())


def test_threads_and_old_table():
    db = F.FarmDB()

    async def flow():
        U = 9005
        G = (await db.create(U, "plain", T))["id"]
        await db.set_thread(G, 10, 555)
        assert await db.get_thread(G, 10) == 555 and await db.get_thread(G, 11) is None
        assert await db.thread_farm(555) == G and await db.thread_owner(555) == U and await db.thread_owner(556) is None
        await db.set_thread(G, 10, 777)                                       # 스레드를 새로 열면 바뀐다
        assert await db.thread_owner(555) is None and await db.thread_owner(777) == U
        await db.set_thread(G, 11, 888)
        assert sorted(await db.threads_of(G)) == [777, 888]
    run(flow())
    # PR #118 의 옛 밭 표가 있으면: 심어 둔 씨앗 값을 돌려주고 지운다
    con = sqlite3.connect(edb.DB_PATH)
    con.execute("CREATE TABLE farm_plots (user_id INTEGER, slot INTEGER, crop TEXT, planted_ts INTEGER, PRIMARY KEY(user_id, slot))")
    con.execute("INSERT INTO farm_plots VALUES(42, 0, 'corn', 1), (42, 1, 'carrot', 1)")
    con.commit(); con.close()
    db2 = F.FarmDB()
    assert run(db2.get_balance(42)) == 110_000
    con = sqlite3.connect(edb.DB_PATH)
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='farm_plots'").fetchone()
    con.close()


def test_old_single_farm_tables():
    """사람마다 농장 하나였던 옛 표 (user_id) → 농장 번호(id) + 주인(owner): 농장 · 밭 · 창고 · 스레드가 그대로 남는다."""
    keep = edb.DB_PATH
    edb.DB_PATH = Path(tempfile.mkdtemp()) / "old.sqlite3"
    try:
        con = sqlite3.connect(edb.DB_PATH)
        con.execute("CREATE TABLE farms (user_id INTEGER PRIMARY KEY, land TEXT NOT NULL, coins INTEGER NOT NULL DEFAULT 0, "
                    "energy REAL NOT NULL, energy_ts INTEGER NOT NULL, plots INTEGER NOT NULL, sickle INTEGER NOT NULL DEFAULT 0, "
                    "whet INTEGER NOT NULL DEFAULT 0, c_wood INTEGER NOT NULL DEFAULT 0, c_silver INTEGER NOT NULL DEFAULT 0, "
                    "c_gold INTEGER NOT NULL DEFAULT 0, plant_n INTEGER NOT NULL DEFAULT 0, daily_day INTEGER NOT NULL DEFAULT 0, "
                    "created_ts INTEGER NOT NULL)")
        con.execute("CREATE TABLE farm_fields (user_id INTEGER NOT NULL, slot INTEGER NOT NULL, kind TEXT NOT NULL, "
                    "key TEXT NOT NULL, ready_ts INTEGER NOT NULL, PRIMARY KEY(user_id, slot))")
        con.execute("CREATE TABLE farm_items (user_id INTEGER NOT NULL, key TEXT NOT NULL, tier INTEGER NOT NULL, "
                    "qty INTEGER NOT NULL, PRIMARY KEY(user_id, key, tier))")
        con.execute("CREATE TABLE farm_threads (user_id INTEGER NOT NULL, guild_id INTEGER NOT NULL, thread_id INTEGER NOT NULL, "
                    "PRIMARY KEY(user_id, guild_id))")
        con.execute("CREATE INDEX farm_threads_tid ON farm_threads(thread_id)")
        A, B = 673864024030445569, 757761125403066419          # 진짜 id 처럼 큰 수
        con.execute("INSERT INTO farms(user_id, land, coins, energy, energy_ts, plots, sickle, created_ts) VALUES(?,?,?,?,?,?,?,?)",
                    (A, "river", 321, 50, T, 8, 2, T))
        con.execute("INSERT INTO farms(user_id, land, energy, energy_ts, plots, created_ts) VALUES(?,?,?,?,?,?)",
                    (B, "plain", 100, T, 6, T + 1))
        con.execute("INSERT INTO farm_fields VALUES(?, 3, 'animal', 'cow', ?)", (A, T + 99))
        con.execute("INSERT INTO farm_items VALUES(?, 'corn', 2, 4)", (A,))
        con.execute("INSERT INTO farm_threads VALUES(?, 10, 555)", (A,))
        con.execute("CREATE TABLE farm_plots (user_id INTEGER, slot INTEGER, crop TEXT, planted_ts INTEGER, PRIMARY KEY(user_id, slot))")
        con.execute("INSERT INTO farm_plots VALUES(42, 0, 'corn', 1)")       # 더 옛 표와 같이 있어도
        con.commit(); con.close()
        db = F.FarmDB()
        F.FarmDB()                                               # 두 번 열어도 그대로
        con = sqlite3.connect(edb.DB_PATH)
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
        assert {"farms", "farm_fields", "farm_items", "farm_threads", "farm_offers", "farm_threads_tid", "farms_owner"} <= names
        assert not any(n.endswith("_one") for n in names) and "farm_plots" not in names
        assert "user_id" not in {r[1] for t in ("farms", "farm_fields", "farm_items", "farm_threads")
                                 for r in con.execute(f"PRAGMA table_info({t})")}
        con.close()

        async def flow():
            mine = await db.farms_of(A, T)
            assert len(mine) == 1 and len(await db.farms_of(B, T)) == 1
            f = mine[0]
            assert (f["owner"], f["land"], f["coins"], f["plots"], f["sickle"]) == (A, "river", 321, 8, 2)
            assert f["fields"][3]["key"] == "cow" and f["items"] == [("corn", 2, 4)]
            assert await db.thread_owner(555) == A and await db.get_thread(f["id"], 10) == 555
            assert (await db.sell(A, f["id"], T))["coins"] == 4 * F.value("corn", 2)
            new = await db.create(9, "plain", T)                 # 새 농장은 새 번호
            assert new["ok"] and new["id"] not in (f["id"], (await db.farms_of(B, T))[0]["id"])
        run(flow())
    finally:
        edb.DB_PATH = keep


def test_takeover():
    """인수 (농장 코인): 적정 인수 금액 이상이면 바로 · 아니면 주인이 수락 · 산 사람은 내 농장 코인으로 내고
    판 사람도 코인으로 받는다 (남은 농장 → 없으면 다음에 여는 농장) · 농장은 여러 개 가질 수 있다."""
    db = F.FarmDB()

    def coins(fid, c):
        con = sqlite3.connect(edb.DB_PATH)
        con.execute("UPDATE farms SET coins=? WHERE id=?", (c, fid))
        con.commit(); con.close()

    async def flow():
        A, B, C = 9201, 9202, 9203
        G = (await db.create(A, "river", T))["id"]
        H = (await db.create(B, "plain", T))["id"]
        K = (await db.create(C, "meadow", T))["id"]
        f = await db.get(G, T)
        assert f["worth"] == F.FARM_WORTH_MIN and f["fair"] == F.FARM_WORTH_MIN * 3 and f["offers"] == 0
        con = sqlite3.connect(edb.DB_PATH)
        con.execute("UPDATE farms SET coins=1000, plots=8, sickle=2 WHERE id=?", (G,))
        con.execute("INSERT INTO farm_fields VALUES(?, 0, 'animal', 'chicken', ?)", (G, T + 99))
        con.execute("INSERT INTO farm_items VALUES(?, 'corn', 2, 2)", (G,))
        con.commit(); con.close()
        worth = 1000 + 300 + 2 * F.value("corn", 2) + 150 + 400 + 150 + 600
        f = await db.get(G, T)
        fair = worth * 3
        assert f["worth"] == worth and f["fair"] == fair
        assert (await db.offer_farm(A, G, fair, T, H))["reason"] == "mine"
        assert (await db.offer_farm(B, G, 0, T, H))["reason"] == "amount"
        assert (await db.offer_farm(B, G, 1000, T, None))["reason"] == "pay"
        assert (await db.offer_farm(B, G, 1000, T, K))["reason"] == "pay"            # 남의 농장 코인으로는 못 낸다
        bad = await db.offer_farm(B, G, 1000, T, H)
        assert bad["reason"] == "coins" and (bad["cost"], bad["have"]) == (1000, 0)
        assert (await db.offer_farm(B, 999999, 1000, T, H))["reason"] == "gone"
        coins(H, fair * 10)
        coins(K, fair * 10)
        bal_a = await db.get_balance(A)
        # 적정 인수 금액보다 적으면 제안 — 코인은 아직 그대로, 다시 내면 새 제안으로 바뀐다
        o = await db.offer_farm(B, G, fair - 1, T, H)
        assert o["ok"] and not o["auto"] and o["owner_id"] == A and o["expires"] == T + 24 * 3600
        o2 = await db.offer_farm(B, G, fair // 2, T + 10, H)
        o3 = await db.offer_farm(C, G, fair // 3, T + 20, K)
        assert (await db.get(H, T + 20))["coins"] == fair * 10
        offers = await db.farm_offers(A, G, T + 30)
        assert [x["id"] for x in offers] == [o2["offer_id"], o3["offer_id"]] and (await db.get(G, T + 30))["offers"] == 2
        assert await db.farm_offers(B, G, T + 30) == []                       # 주인만 본다
        assert (await db.answer_farm_offer(B, o2["offer_id"], True, T + 30))["reason"] == "gone"
        assert (await db.answer_farm_offer(A, o["offer_id"], True, T + 30))["reason"] == "gone"     # 바뀐 옛 제안
        r = await db.answer_farm_offer(A, o3["offer_id"], False, T + 30)
        assert r["ok"] and not r["accepted"] and r["buyer_id"] == C
        assert await db.farm_offers(A, G, T + 24 * 3600 + 11) == []           # 24시간 뒤 만료
        assert (await db.answer_farm_offer(A, o2["offer_id"], True, T + 24 * 3600 + 11))["reason"] == "gone"
        # 수락 → 농장이 통째로 넘어가고 코인이 오간다. A 는 남은 농장이 없어서 코인을 맡아 뒀다가 새 농장에 넣는다
        r = await db.answer_farm_offer(A, o2["offer_id"], True, T + 40)
        assert r["ok"] and r["accepted"] and r["id"] == G and r["to_fid"] is None and r["to_coins"] == fair // 2
        assert (await db.get(H, T + 40))["coins"] == fair * 10 - fair // 2 and await db.get_balance(A) == bal_a
        assert [x["id"] for x in await db.farms_of(B, T + 40)] == [G, H] and await db.farms_of(A, T + 40) == []
        f = await db.get(G, T + 40)
        assert f["owner"] == B and f["coins"] == 1000 and f["plots"] == 8 and f["items"] == [("corn", 2, 2)]
        assert (await db.plant(A, G, "carrot", T + 40))["reason"] == "none" and (await db.plant(B, G, "carrot", T + 40))["ok"]
        n = await db.create(A, "plain", T + 40)                               # 판 사람은 새로 열 수 있다 → 맡아 둔 코인이 들어간다
        assert n["ok"] and n["held"] == fair // 2 and (await db.get(n["id"], T + 40))["coins"] == fair // 2
        assert (await db.create(9299, "plain", T + 40))["held"] == 0
        # 주인이 적정 인수 금액을 정한다 (가치 × 1.5 ~ × 5) → 그 이상이면 바로 인수
        lo, hi = F.farm_ask_range(worth)
        assert (lo, hi) == (worth * 3 // 2, worth * 5)
        assert (await db.set_farm_ask(A, G, lo, T + 50))["reason"] == "none"
        bad = await db.set_farm_ask(B, G, lo - 1, T + 50)
        assert bad["reason"] == "range" and (bad["lo"], bad["hi"]) == (lo, hi)
        assert (await db.set_farm_ask(B, G, lo, T + 50))["ok"] and (await db.get(G, T + 50))["fair"] == lo
        h_before = (await db.get(H, T + 60))["coins"]
        r = await db.offer_farm(C, G, lo, T + 60, K)
        assert r["ok"] and r["auto"] and r["prev_owner"] == B and r["cost"] == lo and r["left"] == fair * 10 - lo
        assert r["to_fid"] == H and r["to_land"] == "plain" and r["to_coins"] == h_before + lo   # 판 사람 남은 농장에 코인으로
        assert (await db.get(H, T + 60))["coins"] == h_before + lo
        f = await db.get(G, T + 60)
        assert f["owner"] == C and f["ask"] is None and f["fair"] == fair and f["offers"] == 0     # 주인이 바뀌면 처음 값으로
        assert [x["id"] for x in await db.farms_of(B, T + 60)] == [H]
        # 수락할 때 제안한 사람 농장 코인이 모자라면 닫힌다
        o = await db.offer_farm(B, G, lo, T + 70, H)
        assert o["ok"] and not o["auto"]
        coins(H, 0)
        r = await db.answer_farm_offer(C, o["offer_id"], True, T + 80)
        assert not r["ok"] and r["reason"] == "coins" and (await db.get(G, T + 80))["owner"] == C
        assert await db.farm_offers(C, G, T + 80) == []
    run(flow())


def test_shop():
    """농사 상점: 서버마다 스레드 하나 (패치노트 채널 → 안 되면 농장을 여는 채널) · 씨앗 · 동물을 사서 농장에서 심는다."""
    from unittest.mock import AsyncMock, MagicMock

    import discord

    from collections import Counter

    import cogs.farm as C
    import release

    class Resp:
        def __init__(self):
            self.calls = []

        def is_done(self):
            return bool(self.calls)

        async def send_message(self, *a, **kw):
            self.calls.append(("send", {"content": a[0] if a else None, **kw}))

        async def edit_message(self, **kw):
            self.calls.append(("edit", kw))

    def inter(user, guild, values=None):
        i = MagicMock()
        i.user, i.guild, i.guild_id, i.data, i.response = user, guild, guild.id, {"values": values or []}, Resp()
        return i

    def text_channel(cid, ok=True):
        ch = MagicMock(spec=discord.TextChannel, id=cid)
        ch.name, ch.is_news = f"ch{cid}", MagicMock(return_value=False)
        ch.permissions_for = MagicMock(return_value=MagicMock(view_channel=True, create_public_threads=ok,
                                                              send_messages_in_threads=True))
        return ch

    def shop_thread(tid, archived=False):
        th = MagicMock(spec=discord.Thread, id=tid, archived=archived, locked=False)

        async def edit(**kw):
            th.archived, th.locked = kw.get("archived", th.archived), kw.get("locked", th.locked)
        th.send, th.edit = AsyncMock(return_value=MagicMock(id=tid + 1000)), AsyncMock(side_effect=edit)
        return th

    async def flow():
        C.SHOPS.clear()
        bot = MagicMock()
        cog = C.Farm(bot)
        kids = cog.shop.children
        # 11/12 '확실하게 분류': 씨앗 · 동물 · 아이템 메뉴 하나씩 + 내 코인
        assert [(x.custom_id, x.placeholder, x.row) for x in kids[:3]] == [
            ("farm:shop:seed", "씨앗 사기", 0), ("farm:shop:animal", "동물 사기", 1), ("farm:shop:tool", "아이템 사기", 2)]
        assert [o.value for o in kids[0].options] == [k for k, v in F.CROPS.items() if v[2]] and "carrot" not in [o.value for o in kids[0].options]
        assert [o.value for o in kids[1].options] == list(F.ANIMALS) and [o.value for o in kids[2].options] == list(F.TOOLS)
        assert kids[0].options[0].label == "감자" and kids[0].options[0].description == "5 코인"
        assert kids[-1].custom_id == "farm:shop:coins" and kids[-1].label == "내 코인" and len(kids) == 4   # 11/11 상점에서 내 코인 보기
        assert not any(getattr(x, "emoji", None) for x in kids)
        es = C.shop_embeds()
        assert [e.title for e in es[1:]] == ["씨앗", "동물", "아이템"]
        seeds, animals, tools = ({f.name: f.value for f in e.fields} for e in es[1:])
        assert seeds["감자"] == "5 코인" and animals["닭"] == "300 코인" and tools["비료"].startswith("**20 코인**")
        assert "당근은 씨앗 없이" in es[1].description and "감자" not in animals and "닭" not in seeds
        for e in es:
            assert "·" not in "".join(f.value for f in e.fields) + e.description                # 값을 · 로 이어 붙이지 않는다
            assert len(e.fields) <= 25

        # 패치노트가 가는 채널에 만든다 → 다시 부르면 있던 스레드
        notes_ch, th = text_channel(70), shop_thread(6001)
        notes_ch.create_thread = AsyncMock(return_value=th)
        notes = MagicMock(_pick_channel=MagicMock(return_value=notes_ch))
        bot.get_cog = MagicMock(return_value=notes)
        guild = MagicMock(id=release.TEST_GUILD)
        guild.get_thread = MagicMock(return_value=None)
        guild.fetch_channel = AsyncMock(return_value=th)
        assert await cog.ensure_shop(guild) is th
        kw = notes_ch.create_thread.await_args.kwargs
        assert kw["name"] == "농사 상점" and kw["type"] == discord.ChannelType.public_thread
        assert isinstance(th.send.await_args.kwargs["view"], C.ShopPanel)
        assert not th.edit.await_count                                                      # 잠그지 않는다 (잠그면 버튼이 막힌다)
        assert C.SHOPS == {guild.id: 6001} and await cog.db.shop_threads() == {guild.id: (6001, 7001, C.SHOP_VER)}
        assert await cog.ensure_shop(guild) is th and notes_ch.create_thread.await_count == 1
        # 상점 화면이 바뀌면 (버전이 다르면) 이미 올라간 상점 글을 새 화면으로 고친다 — 글 번호를 모르는 옛 상점은 스레드 첫 봇 글
        bot.user = MagicMock(id=999)
        old = MagicMock(author=MagicMock(id=999), embeds=[1], edit=AsyncMock(), id=7777)

        async def history(**kw):
            yield MagicMock(author=MagicMock(id=5), embeds=[1])
            yield old
        th.history = history
        th.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "x"))
        await cog.db.set_shop_thread(guild.id, 6001)
        C.SHOP_MSGS[guild.id] = (None, 0)
        assert await cog.ensure_shop(guild) is th and old.edit.await_count == 1
        assert isinstance(old.edit.await_args.kwargs["view"], C.ShopPanel) and th.send.await_count == 1
        assert (await cog.db.shop_threads())[guild.id] == (6001, 7777, C.SHOP_VER)
        await cog.ensure_shop(guild)
        assert old.edit.await_count == 1                         # 한 번 고치면 끝
        th.archived = True                                       # 보관되면 다시 연다
        await cog.ensure_shop(guild)
        assert th.edit.await_args.kwargs == {"archived": False} and th.edit.await_count == 1
        th.locked = True                                         # #134 로 잠근 상점 스레드는 푼다 (권한 없는 사람은 버튼이 막혔다)
        await cog.ensure_shop(guild)
        assert th.edit.await_args.kwargs == {"locked": False} and not th.locked and th.edit.await_count == 2
        await cog.ensure_shop(guild)
        assert th.edit.await_count == 2                          # 풀렸으면 그대로
        guild.fetch_channel = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "x"))   # 잠깐 안 될 땐 새로 안 만든다
        assert await cog.ensure_shop(guild) is None and notes_ch.create_thread.await_count == 1
        th2 = shop_thread(6002)                                  # 지워졌으면 새로 만든다
        notes_ch.create_thread = AsyncMock(return_value=th2)
        guild.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "x"))
        assert await cog.ensure_shop(guild) is th2 and C.SHOPS[guild.id] == 6002

        # 패치노트가 스레드로 가는 서버는 그 스레드의 채널에 · 권한이 없으면 농장을 여는 채널에
        gid = next(iter(C.PATCH_THREADS))
        g3 = MagicMock(id=gid)
        parent, th3 = text_channel(80), shop_thread(6003)
        parent.create_thread = AsyncMock(return_value=th3)
        patch_thread = MagicMock(spec=discord.Thread)
        patch_thread.parent = parent
        g3.get_channel_or_thread = MagicMock(return_value=patch_thread)
        assert await cog.ensure_shop(g3) is th3 and notes_ch.create_thread.await_count == 1
        g4, near, th4 = MagicMock(id=44), text_channel(90), shop_thread(6004)
        notes._pick_channel = MagicMock(return_value=text_channel(71, ok=False))
        near.create_thread = AsyncMock(return_value=th4)
        assert await cog.ensure_shop(g4) is None and await cog.ensure_shop(g4, near) is th4
        g5, news, th5 = MagicMock(id=55), text_channel(95), shop_thread(6005)         # 공지 채널은 글을 올리고 그 글에서 연다
        first = MagicMock(create_thread=AsyncMock(return_value=th5))
        news.is_news, news.send = MagicMock(return_value=True), AsyncMock(return_value=first)
        notes._pick_channel = MagicMock(return_value=news)
        assert await cog.ensure_shop(g5) is th5 and first.create_thread.await_args.kwargs["name"] == "농사 상점"
        assert isinstance(th5.send.await_args.kwargs["view"], C.ShopPanel)

        # 공개 전: 테스트 서버에만 만든다
        bot.guilds = [MagicMock(id=1374213619793006704), guild]
        cog.ensure_shop = AsyncMock()
        await C.Farm.shop_loop.coro(cog)
        assert [c.args[0].id for c in cog.ensure_shop.await_args_list] == [guild.id]

        # 사기: 농장이 없으면 안내 · 있으면 코인으로 사서 사 둔 씨앗에 들어간다
        user = MagicMock(id=9301, display_name="규완", guild=guild)
        i = inter(user, guild)
        await cog.shop._pick("potato")(i)
        assert "/농사" in i.response.calls[0][1]["content"] and i.response.calls[0][1]["ephemeral"]
        i = inter(user, MagicMock(id=1374213619793006704))
        await cog.shop._pick("potato")(i)
        assert "아직" in i.response.calls[0][1]["content"]
        i = inter(user, guild)
        await cog.shop._coins(i)
        assert "/농사" in i.response.calls[0][1]["content"] and i.response.calls[0][1]["ephemeral"]
        i = inter(user, MagicMock(id=1374213619793006704))
        await cog.shop._coins(i)
        assert "아직" in i.response.calls[0][1]["content"]
        G = (await cog.db.create(9301, "plain", T))["id"]
        con = sqlite3.connect(edb.DB_PATH)
        con.execute("UPDATE farms SET coins=32 WHERE id=?", (G,))
        con.commit(); con.close()
        i = inter(user, guild)
        await cog.shop._coins(i)                                  # 내 코인: 누른 사람에게만 · 농장마다 한 칸
        e = i.response.calls[0][1]["embed"]
        assert i.response.calls[0][1]["ephemeral"] and e.title.endswith("내 코인")
        assert (e.fields[0].name, e.fields[0].value) == (f"{F.LANDS['plain'][1]} 농장", "32 코인") and len(e.fields) % 3 == 0
        i = inter(user, guild, ["potato"])                      # 씨앗 메뉴에서 고르면 → 사기 화면 · 상점 글의 메뉴는 비워 둔다
        i.message = MagicMock(edit=AsyncMock())
        await cog.shop._menu(i)
        buy = i.response.calls[0][1]["view"]
        assert isinstance(buy, C.BuyView) and i.response.calls[0][1]["ephemeral"] and buy.key == "potato"
        assert i.message.edit.await_args.kwargs == {"view": cog.shop}
        assert [(b.label, b.disabled) for b in buy.children] == [
            ("1개 (5 코인)", False), ("5개 (25 코인)", False), ("10개 (50 코인)", True), ("살 수 있는 만큼 (6개)", False)]
        i = inter(user, guild)
        await buy._buy(5)(i)
        assert "감자 5개" in i.response.calls[0][1]["embed"].description
        i = inter(user, guild)
        await buy._buy(0)(i)                                     # 살 수 있는 만큼 (7 코인 → 1개)
        f = await cog.db.get(G, T)
        assert f["seeds"] == {"potato": 6} and f["coins"] == 2 and f["worth"] == F.FARM_WORTH_MIN
        i = inter(user, guild)
        await buy._buy(1)(i)
        assert "모자라요" in i.response.calls[0][1]["embed"].description
        assert (await cog.db.buy(9301, G, "carrot", 1, T))["reason"] == "key"         # 당근은 안 판다
        assert (await cog.db.buy(1, G, "potato", 1, T))["reason"] == "none"
        r = await cog.db.plant(9301, G, "potato", T)             # 사 둔 씨앗만큼 심는다 (빈 땅 6칸)
        assert r["ok"] and r["count"] == 6 and r["left"] == 0 and (await cog.db.get(G, T))["seeds"] == {}
        i = inter(user, guild)                                   # 동물은 한 마리씩
        await cog.shop._pick("cow")(i)
        assert [b.label for b in i.response.calls[0][1]["view"].children] == [f"1마리 ({F.ANIMALS['cow'][2]:,} 코인)"]
        desc = i.response.calls[0][1]["embed"].description
        assert "값 **" in desc and "우유" in desc and "·" not in desc.split("-#")[0]          # 한 줄에 하나
        # 농장이 여러 개면 어느 농장 코인으로 살지 고른다
        H = (await cog.db.create(9302, "river", T))["id"]
        con = sqlite3.connect(edb.DB_PATH)
        con.execute("UPDATE farms SET owner=9301, coins=1000 WHERE id=?", (H,))
        con.commit(); con.close()
        i = inter(user, guild)
        await cog.shop._pick("corn")(i)
        buy = i.response.calls[0][1]["view"]
        sel = [c for c in buy.children if isinstance(c, discord.ui.Select)]
        assert len(sel) == 1 and len(sel[0].options) == 2
        i = inter(user, guild, [str(H)])
        await buy._farm(i)
        i = inter(user, guild)
        await buy._buy(10)(i)
        assert (await cog.db.get(H, T))["seeds"] == {"corn": 10} and (await cog.db.get(H, T))["coins"] == 800
        C.SHOPS.clear()
    run(flow())


def test_ui_smoke():
    """농장 화면: 땅 고르기 → 스레드 → 심기 · 거두기 · 작은 화면들이 오류 없이 그려진다 (디스코드 없이 가짜 상호작용으로)."""
    from unittest.mock import AsyncMock, MagicMock

    import discord

    import cogs.farm as C

    # discord.py 가 상호작용마다 부르는 View 내부 함수를 덮어쓰지 않는다 (덮으면 'never awaited' 경고)
    for v in (C.FarmPanel, C.ShopPanel, C.BuyView):
        assert v._refresh is discord.ui.View._refresh, v

    assert [C._dur(x) for x in (10, 60, 150, 4800)] == ["10초", "1분", "2분 30초", "1시간 20분"]   # 1분 안쪽은 초로

    class Resp:
        def __init__(self):
            self.done, self.calls = False, []

        def is_done(self):
            return self.done

        async def _do(self, name, **kw):
            self.done = True
            self.calls.append((name, kw))

        async def send_message(self, *a, **kw):
            await self._do("send", content=a[0] if a else None, **kw)

        async def edit_message(self, **kw):
            await self._do("edit", **kw)

        async def defer(self, **kw):
            await self._do("defer", **kw)

        async def send_modal(self, modal):
            await self._do("modal", modal=modal)

    def inter(user, channel, guild, values=None, message=None):
        i = MagicMock()
        i.user, i.channel, i.guild, i.guild_id, i.channel_id = user, channel, guild, guild.id, channel.id
        i.data = {"values": values or []}
        i.response, i.followup, i.message = Resp(), MagicMock(send=AsyncMock()), message
        return i

    async def flow():
        bot = MagicMock()
        cog = C.Farm(bot)
        assert bot.add_view.called and all(x.custom_id.startswith("farm:") for x in cog.panel.children)   # 재시작 뒤에도 눌린다
        guild = MagicMock(id=release_guild())
        user = MagicMock(id=9100, display_name="규완", mention="<@9100>", guild=guild)
        thread = MagicMock(spec=discord.Thread, id=4242, archived=False, mention="<#4242>")
        thread.send, thread.edit = AsyncMock(), AsyncMock()
        channel = MagicMock(spec=discord.TextChannel, id=77)
        channel.create_thread = AsyncMock(return_value=thread)
        guild.get_thread = MagicMock(return_value=None)
        guild.fetch_channel = AsyncMock(return_value=thread)
        C.SHOPS[guild.id] = 5151                                 # 이 서버엔 농사 상점 스레드가 이미 있다

        # 처음: 땅 고르기 화면 → 고르고 → 시작 → 스레드가 열리고 농장 화면이 올라간다
        i = inter(user, channel, guild)
        await C.Farm.farm.callback(cog, i)
        pick = i.response.calls[0][1]["view"]
        assert isinstance(pick, C.LandPick) and i.response.calls[0][1]["ephemeral"]
        i = inter(user, channel, guild, ["mountain"])
        await pick._picked(i)
        i = inter(user, channel, guild)
        await pick._start(i)
        channel.create_thread.assert_awaited_once()
        assert channel.create_thread.await_args.kwargs["type"] == discord.ChannelType.public_thread
        assert await cog.db.thread_owner(4242) == 9100
        sent = thread.send.await_args.kwargs
        pe = sent["embed"]
        ptext = pe.description + "".join(x.name + x.value for x in pe.fields) + pe.footer.text
        assert isinstance(sent["view"], C.FarmPanel) and "바위 산지" in [x.name for x in pe.fields]
        assert "·" not in ptext and "×" not in ptext                        # 11/11 가독성: 값을 · 로 이어 붙이지 않는다
        assert [x.name for x in pe.fields[:3]] == ["에너지", "코인", "낫"] and len(pe.fields) % 3 == 0
        le = C.land_embed(user, "mountain")
        land = le.description + "".join(f.name + f.value for f in le.fields)
        assert "광맥 ⭐⭐⭐⭐⭐" in land and "★" not in land and "☆" not in land and "·" not in land   # 노란 별 · 한 줄에 하나
        assert le.fields[4].name == "바위 산지 (선택)"
        assert "<#5151>" in sent["embed"].description            # 씨앗 · 동물은 상점에서
        assert "<#4242>" in i.followup.send.await_args.args[0]

        # 다른 채널에서 다시 /농사 → 있던 스레드로 안내 · 스레드 안에서 /농사 → 화면을 새로 올린다
        posts = thread.send.await_count
        i = inter(user, channel, guild)
        await C.Farm.farm.callback(cog, i)
        assert channel.create_thread.await_count == 1 and "<#4242>" in i.followup.send.await_args.args[0]
        assert thread.send.await_count == posts                   # 있던 스레드에 농장 화면을 다시 올리지 않는다
        tch = MagicMock(spec=discord.Thread, id=4242)
        i = inter(user, tch, guild)
        await C.Farm.farm.callback(cog, i)
        assert i.response.calls[0][0] == "send" and isinstance(i.response.calls[0][1]["view"], C.FarmPanel)

        # 주인만 누를 수 있다
        panel = cog.panel
        other = MagicMock(id=1, display_name="남", guild=guild)
        i = inter(other, tch, guild)
        assert not await panel.interaction_check(i) and i.response.calls[0][1]["ephemeral"]
        assert await panel.interaction_check(inter(user, tch, guild))

        # 심기 → 거두기 → 넓히기(코인 부족) → 칸 수 바꾸기
        msg = MagicMock(edit=AsyncMock())
        i = inter(user, tch, guild, ["carrot"], msg)
        await panel._plant(i)
        assert "6칸" in i.response.calls[0][1]["embed"].description
        import sqlite3
        con = sqlite3.connect(edb.DB_PATH)
        G = (await cog.db.farms_of(9100, T))[0]["id"]
        con.execute("UPDATE farm_fields SET ready_ts=0 WHERE farm_id=?", (G,))
        con.execute("UPDATE farms SET coins=50000, whet=3, c_silver=2 WHERE id=?", (G,))
        con.execute("INSERT OR REPLACE INTO farm_items VALUES(?, 'egg', 1, 9)", (G,))
        con.execute("INSERT INTO farm_seeds VALUES(?, 'chicken', 1)", (G,))
        con.commit(); con.close()
        i = inter(user, tch, guild, message=msg)
        await panel._harvest(i)
        assert "거뒀어요" in i.response.calls[0][1]["embed"].description
        for cb, vals in ((panel._expand, None), (panel._qty, ["2"]), (panel._plant, ["chicken"]), (panel._reload, None),
                         (panel._help, None)):
            i = inter(user, tch, guild, vals, msg)
            await cb(i)
            assert i.response.calls
        assert (await cog.db.get(G, T))["animals"] == 1          # 사 둔 닭을 입양했다
        i = inter(user, tch, guild, ["potato"], msg)             # 씨앗이 없으면 상점으로 안내
        await panel._plant(i)
        assert "<#5151>" in i.response.calls[0][1]["embed"].description and "감자 씨앗" in i.response.calls[0][1]["embed"].description
        # 작은 화면들
        for opener, cls in ((panel._merge, C.MergeView), (panel._store, C.StoreView), (panel._chest, C.ChestView),
                            (panel._sickle, C.SickleView), (panel._items, C.ItemView)):
            i = inter(user, tch, guild, message=msg)
            await opener(i)
            view = i.response.calls[0][1]["view"]
            assert isinstance(view, cls) and i.response.calls[0][1]["ephemeral"]
            if cls is C.MergeView:
                i = inter(user, tch, guild, ["egg:1"])
                await view._pick(i)
                assert "★2" in i.response.calls[0][1]["embed"].description
            elif cls is C.StoreView:                        # 11/12 물건을 고르면 팔 개수를 직접 적는다
                egg = next(o.value for o in view.children[0].options if o.value.startswith("egg:2:"))
                have = int(egg.split(":")[2])

                async def sell_n(raw):
                    i = inter(user, tch, guild, [egg])
                    await view._sell(i)
                    modal = i.response.calls[0][1]["modal"]
                    assert isinstance(modal, C.SellModal) and "전부" in modal.count.placeholder
                    modal.count._value = raw
                    i = inter(user, tch, guild)
                    await modal.on_submit(i)
                    return i.response.calls[0][1]
                assert "숫자" in (await sell_n("많이"))["content"] and "숫자" in (await sell_n("0"))["content"]
                done = await sell_n("1")
                assert "1개를 팔았어요" in done["embed"].description
                left = {(k, t): q for k, t, q in (await cog.db.get(G, T))["items"]}.get(("egg", 2), 0)
                assert left == have - 1
                i = inter(user, tch, guild)
                await view._sell_all(i)
                i = inter(user, tch, guild, ["0"])
                await view._release(i)
            elif cls is C.ItemView:                          # 11/12 아이템: 비료 켜고 끄기 · 없는 건 못 쓴다 · 사서 쓰기
                assert view.children[0].label.endswith("켜짐") and all(b.disabled for b in view.children[1:])
                i = inter(user, tch, guild)
                await view._fert(i)
                assert "쓰지 않아요" in i.response.calls[0][1]["embed"].description and view.children[0].label.endswith("꺼짐")
                i = inter(user, tch, guild)
                await view._use("drink")(i)
                assert "없어요" in i.response.calls[0][1]["embed"].description
                await cog.db.buy(9100, G, "scarecrow", 1, T)
                i = inter(user, tch, guild)
                await view._use("scarecrow")(i)
                assert "허수아비를 세웠어요" in i.response.calls[0][1]["embed"].description
                assert any("허수아비 세워 둠" in x.value for x in msg.edit.await_args.kwargs["embed"].fields)   # 농장 화면도
            elif cls is C.ChestView:
                i = inter(user, tch, guild)
                await view._toggle(i)                       # 행운 열기 켬 → 돈이 없어서 못 연다
                i = inter(user, tch, guild)
                await view._open("silver")(i)
                assert "모자라요" in i.response.calls[0][1]["embed"].description
                i = inter(user, tch, guild)
                await view._toggle(i)
                i = inter(user, tch, guild)
                await view._open("silver")(i)
                assert "열었어요" in i.response.calls[0][1]["embed"].description
            else:
                i = inter(user, tch, guild)
                await view._go(True)(i)
                i = inter(user, tch, guild)
                await view._toggle(i)
                i = inter(user, tch, guild)
                await view._go(False)(i)
            assert msg.edit.await_count                     # 농장 화면도 같이 고쳐진다
        # 환전
        i = inter(user, tch, guild, message=msg)
        await panel._exchange(i)
        modal = i.response.calls[0][1]["modal"]
        modal.amount._value = "전부"
        i = inter(user, tch, guild, message=msg)
        await modal.on_submit(i)
        assert "원" in i.response.calls[0][1]["embed"].description
        f = await cog.db.get(G, T)
        assert f["coins"] == 0 and f["balance"] > 0

        # ── 인수: 다른 사람은 [인수 제안]만 누를 수 있다 → 금액 제안 → 주인이 거절 · 수락 → 농장 화면 · 스레드 이름이 새 주인으로
        dm = MagicMock(send=AsyncMock(), display_name="누군가")
        bot.get_user = MagicMock(return_value=dm)
        bot.get_channel = MagicMock(return_value=thread)
        guild.get_member = MagicMock(side_effect=lambda uid: {9100: user, 1: other}.get(uid))
        i = inter(other, tch, guild)
        i.data["custom_id"] = "farm:offer"
        assert await panel.interaction_check(i)
        i = inter(user, tch, guild)
        i.data["custom_id"] = "farm:offer"
        assert not await panel.interaction_check(i) and "내 농장" in i.response.calls[0][1]["content"]
        i = inter(other, tch, guild, message=msg)
        await panel._offer(i)                                                    # 인수 금액은 내 농장 코인 → 농장이 있어야 한다
        assert "/농사" in i.response.calls[0][1]["content"] and i.response.calls[0][1]["ephemeral"]
        O = (await cog.db.create(1, "meadow", T))["id"]
        i = inter(other, tch, guild, message=msg)
        await panel._offer(i)
        start = i.response.calls[0][1]["view"]
        assert isinstance(start, C.OfferStart) and i.response.calls[0][1]["ephemeral"] and start.pay == O
        assert "적정 인수 금액" in i.response.calls[0][1]["embed"].description
        assert not any(getattr(x, "emoji", None) for x in start.children)

        async def propose(amount):
            i = inter(other, tch, guild, message=msg)
            await start._propose(i)
            modal = i.response.calls[0][1]["modal"]
            modal.amount._value = amount
            i = inter(other, tch, guild, message=msg)
            await modal.on_submit(i)
            return i.response.calls[0][1]

        assert "숫자" in (await propose("많이"))["content"]
        assert "코인이 모자라요" in (await propose("1,000 코인"))["embed"].description   # 코인이 없으면 제안도 못 한다
        con = sqlite3.connect(edb.DB_PATH)
        con.execute("UPDATE farms SET coins=? WHERE id=?", (10 ** 9, O))
        con.commit(); con.close()
        sent = await propose("1,000 코인")
        assert "보냈어요" in sent["embed"].title and sent["ephemeral"] and dm.send.await_count == 1     # 주인에게 DM
        assert "1,000 코인" in dm.send.await_args.kwargs["embed"].description and "원" not in dm.send.await_args.kwargs["embed"].description
        i = inter(user, tch, guild, message=msg)
        await panel._offers(i)
        box = i.response.calls[0][1]["view"]
        assert isinstance(box, C.OwnerOffers) and "1,000 코인" in i.response.calls[0][1]["embed"].description
        i = inter(user, tch, guild, message=msg)
        await box._ask(i)                                                        # 적정 인수 금액 정하기
        ask = i.response.calls[0][1]["modal"]
        ask.amount._value = "1"
        i = inter(user, tch, guild)
        await ask.on_submit(i)
        assert "까지 정할 수 있어요" in i.response.calls[0][1]["embed"].description
        i = inter(user, tch, guild, message=msg)
        await box._answer(False)(i)
        assert "거절" in i.response.calls[0][1]["embed"].title
        await propose("2000")
        i = inter(user, tch, guild, message=msg)
        await panel._offers(i)
        box = i.response.calls[0][1]["view"]
        edits = msg.edit.await_count
        i = inter(user, tch, guild, message=msg)
        await box._answer(True)(i)
        done = i.response.calls[0][1]["embed"]
        assert "넘겼어요" in done.title and "+2,000" in done.description and "맡아 뒀어요" in done.description   # 판 사람도 코인으로
        assert await cog.db.thread_owner(4242) == 1 and await cog.db.farms_of(9100, T) == []
        assert (await cog.db.get(O, T))["coins"] == 10 ** 9 - 2000
        assert msg.edit.await_count == edits + 1 and "남의 농장" in msg.edit.await_args.kwargs["embed"].title
        assert thread.edit.await_args.kwargs["name"] == "남의 농장"
        assert not await panel.interaction_check(inter(user, tch, guild))           # 이제 전 주인은 못 누른다
        assert await panel.interaction_check(inter(other, tch, guild))

        # 적정 인수 금액으로 바로 인수 → 농장이 둘 → /농사 는 고르는 화면
        G2 = (await cog.db.create(9100, "plain", T))["id"]
        assert (await cog.db.get(G2, T))["coins"] == 2000                          # 맡아 둔 인수 대금이 새 농장에
        await cog.db.set_thread(G2, guild.id, 4343)
        tch2 = MagicMock(spec=discord.Thread, id=4343)
        i = inter(other, tch2, guild, message=msg)
        await panel._offer(i)
        start = i.response.calls[0][1]["view"]
        i = inter(other, tch2, guild, message=msg)
        await start._now(i)
        assert "인수 완료" in i.response.calls[0][1]["embed"].title and i.response.calls[0][0] == "edit"
        assert sorted(x["id"] for x in await cog.db.farms_of(1, T)) == sorted([O, G, G2])
        i = inter(other, channel, guild)
        await C.Farm.farm.callback(cog, i)
        pick = i.response.calls[0][1]["view"]
        assert isinstance(pick, C.FarmPick) and len(pick.children[0].options) == 3
        i = inter(other, channel, guild, [str(G)])
        await pick._picked(i)
        assert "<#4242>" in i.followup.send.await_args.args[0]
        i = inter(other, tch2, guild)                                              # 내 농장 스레드 안에서는 그 농장
        await C.Farm.farm.callback(cog, i)
        assert isinstance(i.response.calls[0][1]["view"], C.FarmPanel) and "황금 평야" in [x.name for x in i.response.calls[0][1]["embed"].fields]
    run(flow())


def test_tools():
    """11/12 농사 상점 아이템: 비료(심을 때 · 절반 시간) · 에너지 음료(+30) · 동물 사료(남은 시간 절반) · 허수아비(10분 ★2 확률 +)."""
    db = F.FarmDB()

    async def flow():
        U = 9401
        G = (await db.create(U, "river", T))["id"]                            # 강변: 비옥도 · 목초 별 3 → 시간 그대로
        con = sqlite3.connect(edb.DB_PATH)
        con.execute("UPDATE farms SET coins=10000 WHERE id=?", (G,))
        con.commit(); con.close()
        assert F.shop_price("fert") == F.TOOLS["fert"][1] and F.shop_price("scarecrow") == F.TOOLS["scarecrow"][1]
        for k, n in (("fert", 2), ("drink", 1), ("feed", 1), ("scarecrow", 2), ("chicken", 1)):
            assert (await db.buy(U, G, k, n, T))["ok"]
        f = await db.get(G, T)
        assert f["tools"] == {"fert": 2, "drink": 1, "feed": 1, "scarecrow": 2} and f["seeds"] == {"chicken": 1}
        assert f["fert_on"] and f["scare_until"] == 0
        assert (await db.plant(U, G, "fert", T))["reason"] == "key"           # 아이템은 심는 것이 아니다
        # 비료: 켜져 있으면 한 칸에 하나씩 (가진 만큼) → 그 칸은 절반 시간
        await db.set_plant_n(U, G, 3)
        p = await db.plant(U, G, "wheat", T)
        assert not p["ok"] and p["reason"] == "seeds"
        await db.buy(U, G, "wheat", 5, T)
        p = await db.plant(U, G, "wheat", T)
        sec = F.CROPS["wheat"][3]
        assert p["ok"] and p["count"] == 3 and p["fert"] == 2 and p["fert_left"] == 0
        assert p["fert_ready"] == T + int(sec * F.FERT_MULT) and p["ready_ts"] == T + sec
        assert sorted(x["ready_ts"] for x in (await db.get(G, T))["fields"] if x) == [T + int(sec * F.FERT_MULT)] * 2 + [T + sec]
        await db.buy(U, G, "fert", 1, T)
        assert await db.set_fert(U, G, False, T) and not (await db.get(G, T))["fert_on"]
        assert not await db.set_fert(9499, G, True, T)                         # 주인만
        assert (await db.plant(U, G, "wheat", T))["fert"] == 0 and (await db.get(G, T))["tools"]["fert"] == 1   # 끄면 안 쓴다
        # 에너지 음료: +30 (최대까지만) · 가득이면 못 마신다
        assert (await db.use_tool(U, G, "fert", T))["reason"] == "key"
        e0 = (await db.get(G, T))["energy"]
        d = await db.use_tool(U, G, "drink", T)
        assert d["ok"] and d["energy"] == min(F.ENERGY_MAX, e0 + F.DRINK_ENERGY) and d["left"] == 0
        assert (await db.use_tool(U, G, "drink", T))["reason"] == "have"
        await db.buy(U, G, "drink", 1, T)
        con = sqlite3.connect(edb.DB_PATH)
        con.execute("UPDATE farms SET energy=?, energy_ts=? WHERE id=?", (F.ENERGY_MAX, T, G))
        con.commit(); con.close()
        assert (await db.use_tool(U, G, "drink", T))["reason"] == "energy" and (await db.get(G, T))["tools"]["drink"] == 1
        # 동물 사료: 기다리는 동물이 없으면 못 쓴다 → 남은 시간 절반
        assert (await db.use_tool(U, G, "feed", T))["reason"] == "animals"
        a = await db.plant(U, G, "chicken", T)
        slot = next(i for i, x in enumerate((await db.get(G, T))["fields"]) if x and x["kind"] == "animal")
        r = await db.use_tool(U, G, "feed", T + 4)
        assert r["ok"] and r["count"] == 1
        assert (await db.get(G, T + 4))["fields"][slot]["ready_ts"] == T + 4 + int((a["ready_ts"] - T - 4) * F.FEED_MULT)
        # 허수아비: 10분 동안 ★2 확률 + · 또 세우면 10분 늘어난다 · 끝나면 그대로
        s1 = await db.use_tool(U, G, "scarecrow", T)
        s2 = await db.use_tool(U, G, "scarecrow", T + 60)
        assert s1["until"] == T + F.SCARE_SEC and s2["until"] == T + 2 * F.SCARE_SEC and s2["left"] == 0
        luck = Fixed(F.high_p(0) + F.SCARE_BONUS / 2)                          # 낫만으로는 ★1, 허수아비가 있으면 ★2
        h = await db.harvest(U, G, T + 300, luck)
        assert h["ok"] and h["scare"] and h["high"] == h["count"] and all(t == 2 for (_, t) in h["got"])
        await db.plant(U, G, "carrot", T + 300)
        h = await db.harvest(U, G, T + 2 * F.SCARE_SEC + 10, luck)
        assert h["ok"] and not h["scare"] and h["high"] == 0
        f = await db.get(G, T + 2 * F.SCARE_SEC + 10)
        assert f["tools"].get("scarecrow", 0) == 0 and f["worth"] >= f["coins"]
    run(flow())


def test_new_farm():
    """11/12 규완님 '100만 코인 모으면 밭 하나 더' (카드: 100만 코인을 내고 만든다) — 농장 하나의 코인으로 새 농장을 연다."""
    import time
    from unittest.mock import AsyncMock, MagicMock

    import release
    import cogs.farm as C
    db = F.FarmDB()

    async def flow():
        U = 9501
        G = (await db.create(U, "plain", T))["id"]
        assert (await db.create(U, "river", T))["reason"] == "exists"         # /농사 로는 하나만
        bad = await db.create_more(U, "river", G, T)
        assert bad["reason"] == "coins" and (bad["cost"], bad["have"]) == (F.NEW_FARM_COST, 0)
        con = sqlite3.connect(edb.DB_PATH)
        con.execute("UPDATE farms SET coins=? WHERE id=?", (F.NEW_FARM_COST + 5, G))
        con.commit(); con.close()
        assert (await db.create_more(9502, "river", G, T))["reason"] == "pay"   # 남의 농장 코인으로는 못 연다
        assert (await db.create_more(U, "nowhere", G, T))["reason"] == "land"
        r = await db.create_more(U, "river", G, T)
        assert r["ok"] and r["cost"] == F.NEW_FARM_COST and r["left"] == 5
        assert [x["id"] for x in await db.farms_of(U, T)] == [G, r["id"]] and (await db.get(r["id"], T))["land"] == "river"
        assert (await db.get(G, T))["coins"] == 5 and (await db.get(r["id"], T))["coins"] == 0
        assert (await db.create_more(U, "river", G, T))["reason"] == "coins"   # 또 모아야 또 연다

        # 농장 화면 [새 밭 만들기]: 코인이 모자라면 안내 · 넉넉하면 땅 고르기 → 만들고 새 스레드
        class Resp:
            def __init__(self):
                self.calls, self._done = [], False

            def is_done(self):
                return self._done

            async def send_message(self, *a, **kw):
                self.calls.append(("send", {"content": a[0] if a else kw.get("content"), **kw})); self._done = True

            async def edit_message(self, **kw):
                self.calls.append(("edit", kw)); self._done = True

            async def defer(self, **kw):
                self._done = True

        guild = MagicMock(id=release.TEST_GUILD)
        user = MagicMock(id=U, display_name="규완", mention=f"<@{U}>", guild=guild)
        cog = C.Farm(MagicMock())
        cog.open = AsyncMock()
        await db.set_thread(G, guild.id, 7171)
        panel = C.FarmPanel(cog, await db.get(G, T))
        assert any(getattr(x, "custom_id", "") == "farm:newfarm" and x.label == "새 밭 만들기" for x in panel.children)

        def inter(values=None):
            i = MagicMock()
            i.user, i.guild, i.guild_id, i.channel_id, i.response = user, guild, guild.id, 7171, Resp()
            i.data, i.message = {"values": values or []}, MagicMock(edit=AsyncMock())
            return i
        i = inter()
        await panel._newfarm(i)
        assert f"{F.NEW_FARM_COST:,}" in i.response.calls[0][1]["content"] and i.response.calls[0][1]["ephemeral"]
        con = sqlite3.connect(edb.DB_PATH)
        con.execute("UPDATE farms SET coins=? WHERE id=?", (F.NEW_FARM_COST * 2, G))
        con.commit(); con.close()
        i = inter()
        await panel._newfarm(i)
        pick = i.response.calls[0][1]["view"]
        assert isinstance(pick, C.LandPick) and pick.pay_fid == G and "새 밭" in i.response.calls[0][1]["embed"].title
        i2 = inter(["meadow"])
        await pick._picked(i2)
        assert pick.children[1].label == "푸른 목초지에 새 밭 만들기"
        i3 = inter()
        await pick._start(i3)
        assert "새 밭을 열었어요" in i3.response.calls[0][1]["embed"].description
        new = max(x["id"] for x in await db.farms_of(U, int(time.time())))     # 방금 연 농장 (테스트의 T 는 지금보다 뒤)
        assert cog.open.await_args.args[2] == new and (await db.get(G, T))["coins"] == F.NEW_FARM_COST
        assert i.message.edit.await_count == 1                                 # 코인을 낸 농장 화면도 고친다
    run(flow())


def test_screens_readable():
    """11/11 규완님 '모든 농사 화면에서 가독성을 다 높여' — 어느 화면에도 값을 · 나 × 로 이어 붙이지 않는다."""
    from unittest.mock import MagicMock

    import cogs.farm as C

    def text(e):   # 봇 공통 꼬리말(키코 · …)은 빼고 본다
        foot = e.footer.text or ""
        return (e.title or "") + (e.description or "") + "".join(x.name + x.value for x in e.fields) + ("" if "키코" in foot else foot)

    def vtext(v):
        out = []
        for c in v.children:
            out.append(getattr(c, "label", None) or getattr(c, "placeholder", None) or "")
            for o in getattr(c, "options", []):
                out.append(o.label + (o.description or ""))
        return "".join(out)

    async def flow():
        user = MagicMock(id=1, display_name="규완", guild=MagicMock(id=1))
        f = dict(id=1, owner=1, land="plain", energy=98, energy_next=None, coins=1234, sickle=3, whet=2, plant_n=0,
                 chests={"wood": 2, "silver": 1, "gold": 0}, plots=12, animals=2, animal_cap=4, balance=50_000, fair=3_000_000,
                 worth=1_000, offers=1, items=[("carrot", 1, 5), ("carrot", 2, 3), ("egg", 1, 1)], seeds={"potato": 5, "chicken": 1},
                 tools={"fert": 3, "drink": 1}, fert_on=False, scare_until=T + 90,
                 fields=[{"kind": "crop", "key": "carrot", "ready": True, "ready_ts": T}] * 3
                 + [{"kind": "crop", "key": "potato", "ready": False, "ready_ts": T + 15}] * 4
                 + [{"kind": "animal", "key": "chicken", "ready": False, "ready_ts": T + 30}] * 2 + [None] * 3)
        cog = MagicMock()
        cog.name_of = MagicMock(return_value="다른 사람")
        embeds = [C.panel_embed(user, f, T, "당근 6개를 합쳐서 **당근 ★2 2개**가 됐어요."), C.help_embed(user),
                  C.land_embed(user, "plain"), C.land_embed(user, "plain", True), *C.shop_embeds()]
        views = [C.FarmPanel(cog, f), C.ShopPanel(cog), C.LandPick(cog, user), C.FarmPick(cog, user, [f])]
        for cls in (C.MergeView, C.StoreView, C.ChestView, C.SickleView, C.ItemView):
            v = cls(cog, user, None, f)
            embeds.append(v.embed(f))
            views.append(v)
        o = C.OfferStart(cog, user, f, None, [dict(f, id=2, land="river"), dict(f, id=3, land="meadow", coins=99)])
        ow = C.OwnerOffers(cog, user, f, [dict(id=1, buyer_id=2, amount=5_000, ts=T)], None)
        b = C.BuyView(cog, user, [f, dict(f, id=2, land="river")], "potato")
        embeds += [o.embed(), ow.embed(), b.embed(), C.coins_embed(user, [f, dict(f, id=2, land="river")])]
        views += [o, ow, b]
        assert "<t:" not in text(embeds[0]) and "감자 4개 (15초 남음)" in text(embeds[0]) and "닭 2마리 (30초 남음)" in text(embeds[0])
        tools = next(x.value for x in embeds[0].fields if x.name == "아이템")             # 11/12 아이템
        assert tools.split("\n") == ["비료 3개 (심을 때 안 써요)", "에너지 음료 1개", "허수아비 세워 둠 (1분 30초 남음)"]
        for e in embeds:
            t = text(e)
            assert "·" not in t and "×" not in t, t
            assert len(e.fields) <= 25 and all(len(x.value) <= 1024 for x in e.fields)
        for v in views:
            t = vtext(v)
            assert "·" not in t and "×" not in t, t
    run(flow())


def test_sticky_panel():
    """11/11 농장 스레드에 다른 글이 올라오면, 조용해진 뒤 밭 화면을 맨 밑에 새로 올리고 위의 옛 밭 화면은 지운다."""
    from unittest.mock import AsyncMock, MagicMock

    import discord

    import release
    import cogs.farm as C

    def panel_msg(mid):
        row = MagicMock(children=[MagicMock(custom_id="farm:qty"), MagicMock(custom_id="farm:harvest")])
        return MagicMock(id=mid, author=MagicMock(id=999), components=[row], delete=AsyncMock())

    def chat_msg(mid, ch, author=5):
        return MagicMock(id=mid, author=MagicMock(id=author), components=[], channel=ch)

    async def flow():
        bot = MagicMock()
        bot.user = MagicMock(id=999)
        cog = C.Farm(bot)
        G = (await cog.db.create(9300, "river", T))["id"]
        guild = MagicMock(id=release.TEST_GUILD)
        guild.get_member = MagicMock(return_value=MagicMock(id=9300, display_name="규완", guild=guild))
        th = MagicMock(spec=discord.Thread, id=4545)
        th.guild, th.send = guild, AsyncMock()
        await cog.db.set_thread(G, guild.id, th.id)
        msgs = []   # 최근 글이 앞

        async def history(limit=100, **kw):
            for m in msgs[:limit]:
                yield m
        th.history = history
        wait, C.STICKY_WAIT = C.STICKY_WAIT, 0
        try:
            old, chat = panel_msg(1), chat_msg(2, th)
            msgs[:] = [chat, chat_msg(3, th), old]
            await cog.on_message(chat)
            await cog._sticky[th.id]
            kw = th.send.await_args.kwargs
            assert th.send.await_count == 1 and isinstance(kw["view"], C.FarmPanel) and "content" not in kw   # 다시 부르지 않는다
            assert "규완의 농장" in kw["embed"].title and old.delete.await_count == 1 and th.id not in cog._sticky

            msgs[:] = [panel_msg(4), chat]                                    # 이미 맨 밑이 밭이면 그대로
            assert not await cog.repost(th, G) and th.send.await_count == 1

            other = MagicMock(spec=discord.Thread, id=4646)                   # 농장 스레드가 아닌 곳 · 봇 자신의 글은 그대로
            other.guild = guild
            tch = MagicMock(spec=discord.TextChannel, id=77, guild=guild)
            for m in (chat_msg(5, other), chat_msg(6, th, author=999), chat_msg(7, tch)):
                await cog.on_message(m)
            off = MagicMock(spec=discord.Thread, id=th.id)                    # /농사 가 아직 안 열린 서버
            off.guild = MagicMock(id=release.TEST_GUILD + 1)
            await cog.on_message(chat_msg(8, off))
            assert not cog._sticky

            C.STICKY_WAIT = 0.05                                              # 글이 이어지면 마지막 글 뒤에 한 번만
            msgs[:] = [chat, panel_msg(9)]
            await cog.on_message(chat)
            first = cog._sticky[th.id]
            await cog.on_message(chat)
            await cog._sticky[th.id]
            assert first.done() and th.send.await_count == 2                 # 앞의 예약은 끊겨서 한 번만 올라갔다
        finally:
            C.STICKY_WAIT = wait
    run(flow())


def test_beta_reset():
    """11/12 규완님 정식 출시: '베타테스트 동안 쌓인 농사 db를 초기화 시켜줘. 모두가 처음부터' ·
    보상 '베타 농장 가치의 50% + 금상자 은상자 나무상자 각각 한 개, 축구 봇 돈 3000만원'."""
    from unittest.mock import MagicMock

    import release
    import cogs.farm as C
    keep_path = edb.DB_PATH
    edb.DB_PATH = Path(tempfile.mkdtemp()) / "beta.sqlite3"
    try:
        db = F.FarmDB()

        async def flow():
            A, B, D, N = 9601, 9602, 9603, 9604
            a1 = (await db.create(A, "plain", T))["id"]
            con = sqlite3.connect(edb.DB_PATH)
            con.execute("UPDATE farms SET coins=? WHERE id=?", (F.NEW_FARM_COST + 4_000, a1))
            con.commit(); con.close()
            a2 = (await db.create_more(A, "river", a1, T))["id"]
            await db.plant(A, a1, "carrot", T)
            await db.set_thread(a1, release.TEST_GUILD, 8181)
            await db.set_shop_thread(release.TEST_GUILD, 8282, 1, C.SHOP_VER)
            con = sqlite3.connect(edb.DB_PATH)
            con.execute("INSERT INTO farm_items(farm_id, key, tier, qty) VALUES(?, 'carrot', 1, 7)", (a2,))
            con.execute("INSERT INTO farm_held(owner, coins) VALUES(?, 500)", (B,))      # 농장을 팔고 맡아 둔 코인만 남은 사람
            con.commit(); con.close()
            assert (await db.redeem_coupon(D, "READYORNAT", release.RELEASE_381_TS - 60, release.TEST_GUILD))["ok"]   # 베타 농장 없이 쿠폰 숫돌만
            worth_a = sum(F.farm_worth(f) for f in await db.farms_of(A, T))
            bal = {u: await db.get_balance(u) for u in (A, B, D)}

            r = await db.beta_reset(T)
            assert r["ok"] and r["people"] == 2 and r["coins"] == round(worth_a * F.BETA_RATE) + 250
            assert (await db.beta_reset(T + 9))["reason"] == "done"                     # 한 번만
            con = sqlite3.connect(edb.DB_PATH)
            for t in ("farms", "farm_fields", "farm_items", "farm_seeds", "farm_threads", "farm_offers"):
                assert con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, t
            assert con.execute("SELECT COUNT(*) FROM farm_beta_farms").fetchone()[0] == 2      # 백업
            assert con.execute("SELECT COUNT(*) FROM farm_beta_farm_items").fetchone()[0] >= 1
            assert con.execute("SELECT thread_id FROM farm_shops").fetchone()[0] == 8282       # 상점은 그대로
            con.close()
            assert await db.farms_of(A, T) == [] and await db.thread_owner(8181) is None
            assert await db.get_balance(A) == bal[A] + F.BETA_WON and await db.get_balance(B) == bal[B] + F.BETA_WON
            assert await db.get_balance(D) == bal[D]

            ra = await db.create(A, "meadow", T + 10)                                     # 새 농장을 열면 보상
            assert ra["beta"] and ra["held"] == round(worth_a * F.BETA_RATE) and ra["chests"] == {"gold": 1, "silver": 1, "wood": 1}
            f = await db.get(ra["id"], T + 10)
            assert f["coins"] == ra["held"] and f["chests"] == {"wood": 2, "silver": 1, "gold": 1} and f["items"] == []
            assert f["plots"] == F.PLOTS_BASE and f["sickle"] == 0
            line = C._held_line(ra)
            assert "베타 테스트" in line and f"{ra['held']:,} 코인" in line and "금 상자 1개" in line and "30,000,000원" in line
            rb = await db.create(B, "meadow", T + 10)
            assert rb["beta"] and rb["held"] == 250
            rd = await db.create(D, "meadow", T + 10)
            assert not rd["beta"] and rd["held"] == 0 and rd["whet"] == 1 and (await db.get(rd["id"], T + 10))["whet"] == 1
            assert "숫돌 1개" in C._held_line(rd) and "베타" not in C._held_line(rd)
            rn = await db.create(N, "meadow", T + 10)                                     # 베타에 없던 사람은 보상 없이
            assert not rn["beta"] and rn["held"] == 0 and (await db.get(rn["id"], T + 10))["chests"] == {"wood": 1, "silver": 0, "gold": 0}
            assert C._held_line(rn) == ""
            # 초기화 뒤 쿠폰 숫돌은 바로 내 농장에
            assert (await db.redeem_coupon(N, "READYORNAT", release.RELEASE_381_TS + 60, 1))["ok"]
            assert (await db.get(rn["id"], T + 10))["whet"] == 1

            # 지운 베타 농장 스레드의 옛 화면을 누르면 안내
            class Resp:
                def __init__(self):
                    self.calls = []

                async def send_message(self, *a, **kw):
                    self.calls.append(a[0] if a else kw.get("content"))
            cog = C.Farm(MagicMock())
            i = MagicMock()
            i.user, i.guild_id, i.channel_id, i.response = MagicMock(id=A), release.TEST_GUILD, 8181, Resp()
            i.data = {"custom_id": "farm:harvest"}
            assert not await C.FarmPanel(cog).interaction_check(i) and "이제 없어요" in i.response.calls[0]

            # 15:00 ~ 15:20 테스트 서버: 베타 끝 안내 · 공개되면 초기화 한 번
            keep = release.RELEASE_381_TS, release.FARM_BETA_END_TS
            try:
                release.RELEASE_381_TS, release.FARM_BETA_END_TS = 2 ** 40, 0
                assert C._closed(release.TEST_GUILD) == C.BETA_OVER and "아직" in C._closed(1)
                i = MagicMock()
                i.user, i.guild_id, i.channel_id, i.response = MagicMock(id=A), release.TEST_GUILD, 8181, Resp()
                assert not await C.FarmPanel(cog).interaction_check(i) and i.response.calls == [C.BETA_OVER]
                cog.db.beta_reset = MagicMock()
                await cog.beta_loop.coro(cog)                                              # 공개 전: 그대로
                cog.db.beta_reset.assert_not_called()
            finally:
                release.RELEASE_381_TS, release.FARM_BETA_END_TS = keep
        run(flow())
    finally:
        edb.DB_PATH = keep_path


def release_guild():
    import release
    return release.TEST_GUILD
