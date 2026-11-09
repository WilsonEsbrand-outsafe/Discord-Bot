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
        assert (await clubs.buy_elite(B, "ideal", now, v381=True))["ok"]       # A 는 인수 대금을 받는다
        r = await clubs.buy_elite(A, "ideal", now + 60, v381=True)
        assert r["reason"] == "rebuy" and r["until"] == now + cdb.ELITE_REBUY_381
        # 7일 뒤엔 다시 인수할 수 있다
        bal_ok = await clubs.buy_elite(A, "ideal", now + cdb.ELITE_REBUY_381, v381=True)
        assert bal_ok["ok"] or bal_ok["reason"] != "rebuy"
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
