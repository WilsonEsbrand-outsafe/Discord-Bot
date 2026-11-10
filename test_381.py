"""3.8.1: 명문 구단 전 주인 재인수 7일 제한 · 2군 쿠폰 테스트는 잔액 마이너스여도 · 낚시 확률 대폭 상향 · /농사 (테스트 서버 먼저)."""
import asyncio
import random
import tempfile
import time
from pathlib import Path

import services.club_db as cdb
import services.economy_db as edb
import services.player_market_db as pmdb

TMP = Path(tempfile.mkdtemp()) / "t.sqlite3"
edb.DB_PATH = cdb.DB_PATH = pmdb.DB_PATH = TMP

import release  # noqa: E402
import services.farm as F  # noqa: E402
import services.fishing as FI  # noqa: E402
import services.youth as Y  # noqa: E402


def test_switch():
    assert release.preview_381(release.TEST_GUILD)
    assert not release.preview_381(1)
    assert "농사" in release.hidden_commands(1) and "농사" not in release.hidden_commands(release.TEST_GUILD)


def test_elite_rebuy_lock():
    db, clubs = edb.EconomyDB(), cdb.ClubDB()
    pmdb.PlayerMarketDB()

    async def flow():
        now = int(time.time())
        A, B = 3811, 3812
        for u in (A, B):
            await clubs.create_club(u, f"FC{u}", now)
        price = cdb.ELITE_SIZES["미드"][0]
        await db.add_balance(A, price * 10)
        await db.add_balance(B, price * 10)
        assert (await clubs.buy_elite(A, "ideal", now, v381=True))["ok"]
        assert (await clubs.offer_elite(B, "ideal", round(price * cdb.ELITE_TAKEOVER), now))["auto"]   # A 는 인수 대금을 받는다
        r = await clubs.offer_elite(A, "ideal", price * 3, now + 60)
        assert r["reason"] == "rebuy" and r["until"] == now + cdb.ELITE_REBUY_381
        # 7일 뒤엔 다시 인수할 수 있다
        assert (await clubs.offer_elite(A, "ideal", price * 3, now + cdb.ELITE_REBUY_381))["ok"]
    asyncio.run(flow())


def test_coupon_tryout_negative_balance():
    db = Y.YouthDB()
    clubs = cdb.ClubDB()

    async def flow():
        U, T = 3813, 1_800_000_000
        await clubs.create_club(U, "유스 FC", 1)
        await db.add_balance(U, -5_000_000)
        await db.give_item(U, "scout_coupon")
        r = await db.open_tryout(U, T, random.Random(1), coupon=True)
        assert r["ok"] and r["applicants"][0]["free"]
        assert (await db.tryout(U, 0, T, random.Random(2)))["reason"] == "balance"   # 다른 서버는 예전 그대로
        r = await db.tryout(U, 0, T, random.Random(2), v381=True)
        assert r["ok"] and r["fee"] == 0
        assert await db.get_balance(U) == -5_000_000
    asyncio.run(flow())


def test_paid_tryout_still_needs_money():
    db = Y.YouthDB()
    clubs = cdb.ClubDB()

    async def flow():
        U, T = 3814, 1_800_000_000
        await clubs.create_club(U, "유스 FC2", 1)
        await db.add_balance(U, -1)
        assert (await db.open_tryout(U, T, random.Random(1)))["ok"]
        assert (await db.tryout(U, 0, T, random.Random(2), v381=True))["reason"] == "balance"
    asyncio.run(flow())


def test_fishing_odds_up():
    for wait in (10, 300, 600):
        old = FI.grade_odds(wait, 0, True, True, True)
        new = FI.grade_odds(wait, 0, True, True, True, v381=True)
        assert sum(new[2:]) > sum(old[2:]) and sum(new[3:]) >= sum(old[3:]) * 1.9
        assert abs(sum(new) - 1) < 1e-9


def test_farm():
    db = F.FarmDB()

    async def flow():
        U, T = 3815, 1_800_000_000
        assert (await db.plant(U, "carrot", T))["reason"] == "balance"
        await db.add_balance(U, 25_000)
        r = await db.plant(U, "carrot", T, all_empty=True)
        assert r["ok"] and r["count"] == 2 and r["balance"] == 5_000
        assert (await db.harvest(U, T + 60))["reason"] == "none"
        f = await db.field(U, T + 60)
        assert sum(p is not None for p in f["plots"]) == 2 and not f["plots"][0]["ready"]
        r = await db.harvest(U, T + F.CROPS["carrot"][3])
        assert r["ok"] and r["total"] == 2 * F.CROPS["carrot"][4] and r["balance"] == 5_000 + 30_000
        await db.add_balance(U, 10**9)
        assert (await db.plant(U, "corn", T, all_empty=True))["count"] == F.PLOTS
        assert (await db.plant(U, "corn", T))["reason"] == "full"
    asyncio.run(flow())


def test_elite_offer():
    """3.8.1 인수 제안: 적정 인수 금액 이상이면 바로 · 아니면 구단주가 수락 · 거절."""
    db, clubs = edb.EconomyDB(), cdb.ClubDB()
    pmdb.PlayerMarketDB()

    async def flow():
        now = int(time.time())
        A, B, C = 3821, 3822, 3823
        for u in (A, B, C):
            await clubs.create_club(u, f"FC{u}", now)
        price = cdb.ELITE_SIZES["미드"][0]
        for u in (A, B, C):
            await db.add_balance(u, price * 10)
        assert (await clubs.buy_elite(A, "ruhr", now, v381=True))["ok"]               # 주인 없으면 그냥 인수
        assert (await clubs.buy_elite(B, "ruhr", now, v381=True))["reason"] == "offer"  # 주인 있으면 제안으로만
        fair = round(price * cdb.ELITE_TAKEOVER)
        # 적게 제안 → 구단주에게 남는다
        r = await clubs.offer_elite(B, "ruhr", fair // 2, now)
        assert r["ok"] and not r["auto"] and r["owner_id"] == A
        r2 = await clubs.offer_elite(C, "ruhr", fair // 3, now)
        offers = await clubs.elite_offers(A, now)
        assert [o["buyer_id"] for o in offers] == [B, C]                                 # 금액 큰 순
        assert (await clubs.answer_elite_offer(B, r["offer_id"], True, now))["reason"] == "gone"   # 구단주만
        x = await clubs.answer_elite_offer(A, r2["offer_id"], False, now)
        assert x["ok"] and not x["accepted"]
        a_bal = await db.get_balance(A)
        x = await clubs.answer_elite_offer(A, r["offer_id"], True, now + 60)
        assert x["ok"] and x["accepted"] and await db.get_balance(A) == a_bal + fair // 2
        c = next(c for c in await clubs.elite_list() if c["key"] == "ruhr")
        assert c["owner_id"] == B and c["price"] == fair // 2
        assert await clubs.elite_offers(A, now) == []
        # 적정 인수 금액 이상 → 바로 인수 (B 가 받은 돈 · A 는 7일 동안 못 되삼)
        fair2 = round(fair // 2 * cdb.ELITE_TAKEOVER)
        assert (await clubs.offer_elite(A, "ruhr", fair2, now + 120))["reason"] == "rebuy"
        b_bal = await db.get_balance(B)
        r = await clubs.offer_elite(C, "ruhr", fair2, now + 120)
        assert r["ok"] and r["auto"] and r["prev_owner"] == B and await db.get_balance(B) == b_bal + fair2
        # 잔액보다 큰 제안은 못 낸다 · 만료된 제안은 수락 못 한다
        assert (await clubs.offer_elite(B, "ruhr", 10**15, now + 130))["reason"] in ("balance", "rebuy")
        await db.add_balance(A, 0)
        r = await clubs.offer_elite(A, "ruhr", 1, now + cdb.ELITE_REBUY_381 + 1000)
        assert r["ok"] and not r["auto"], r
        late = now + cdb.ELITE_REBUY_381 + 1000 + cdb.ELITE_OFFER_HOURS * 3600 + 1
        assert (await clubs.answer_elite_offer(C, r["offer_id"], True, late))["reason"] == "gone"
    asyncio.run(flow())
