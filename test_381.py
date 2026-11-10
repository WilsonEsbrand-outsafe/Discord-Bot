"""3.8.1: 명문 구단 인수 제안 · 적정 인수 금액 · 2군 쿠폰 테스트는 잔액 마이너스여도 · 낚시 확률 대폭 상향 (테스트 서버 먼저). /농사 는 test_farm.py."""
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
import services.fishing as FI  # noqa: E402
import services.youth as Y  # noqa: E402


def test_switch():
    assert release.preview_381(release.TEST_GUILD)
    assert not release.preview_381(1)
    assert "농사" in release.hidden_commands(1) and "농사" not in release.hidden_commands(release.TEST_GUILD)


def test_elite_ask():
    """3.8.1 적정 인수 금액: 안 정하면 지금 가격 × 3 · 구단주가 × 1.5 ~ × 5 안에서 정한다 · 전 주인도 바로 되살 수 있다 (7일 제한 없음)."""
    db, clubs = edb.EconomyDB(), cdb.ClubDB()
    pmdb.PlayerMarketDB()

    async def flow():
        now = int(time.time())
        A, B = 3811, 3812
        for u in (A, B):
            await clubs.create_club(u, f"FC{u}", now)
        price = cdb.ELITE_SIZES["미드"][0]
        await db.add_balance(A, price * 100)
        await db.add_balance(B, price * 100)
        assert (await clubs.buy_elite(A, "ideal", now, v381=True))["ok"]
        c = next(x for x in await clubs.elite_list() if x["key"] == "ideal")
        assert c["fair"] == price * 3 and c["ask"] is None
        assert (await clubs.offer_elite(B, "ideal", price * 3 - 1, now))["auto"] is False
        r = await clubs.set_elite_ask(A, price * 6)
        assert r["reason"] == "range" and (r["lo"], r["hi"]) == (price * 3 // 2, price * 5)
        assert (await clubs.set_elite_ask(B, price * 2))["reason"] == "none"
        assert (await clubs.set_elite_ask(A, price * 2))["ok"]
        assert next(x for x in await clubs.elite_list() if x["key"] == "ideal")["fair"] == price * 2
        r = await clubs.offer_elite(B, "ideal", price * 2, now)
        assert r["auto"] and r["cost"] == price * 2
        c = next(x for x in await clubs.elite_list() if x["key"] == "ideal")
        assert c["owner_id"] == B and c["ask"] is None and c["fair"] == price * 6      # 새 주인 · 기본값으로
        r = await clubs.offer_elite(A, "ideal", price * 6, now + 60)                      # 전 주인도 바로 (7일 제한 없음)
        assert r["ok"] and r["auto"]
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
        fair = round(price * cdb.ELITE_FAIR_381)
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
        # 적정 인수 금액 이상 → 바로 인수 (돈은 B 에게)
        fair2 = round(fair // 2 * cdb.ELITE_FAIR_381)
        b_bal = await db.get_balance(B)
        r = await clubs.offer_elite(C, "ruhr", fair2, now + 120)
        assert r["ok"] and r["auto"] and r["prev_owner"] == B and await db.get_balance(B) == b_bal + fair2
        # 잔액보다 큰 제안은 못 낸다 · 만료된 제안은 수락 못 한다
        assert (await clubs.offer_elite(B, "ruhr", 10**15, now + 130))["reason"] == "balance"
        await db.add_balance(A, 0)
        r = await clubs.offer_elite(A, "ruhr", 1, now + 200)
        assert r["ok"] and not r["auto"], r
        late = now + 200 + cdb.ELITE_OFFER_HOURS * 3600 + 1
        assert (await clubs.answer_elite_offer(C, r["offer_id"], True, late))["reason"] == "gone"
    asyncio.run(flow())
