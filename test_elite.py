# test_elite.py — 2.5 명문 구단(스쿼드 B) · 시설 · 꾸미기 · 송금 조건 · 점검 보상 상자 · 단계 배포
# 실행: venv/Scripts/python.exe test_elite.py
import asyncio
import random
import sqlite3
import tempfile
import time
from pathlib import Path

import services.club_db as cdb
import services.economy_db as edb
import services.player_market_db as pmdb

TMP = Path(tempfile.mkdtemp()) / "t.sqlite3"
edb.DB_PATH = cdb.DB_PATH = pmdb.DB_PATH = TMP

import release  # noqa: E402

D = 86400
A, B, V = 1, 2, 3   # 새 유저 둘 · 2.5 전부터 있던 유저


def sql(q, *a):
    con = sqlite3.connect(TMP)
    rows = con.execute(q, a).fetchall()
    con.commit(); con.close()
    return rows


async def _flow():
    now = int(time.time())
    con = sqlite3.connect(TMP)
    con.execute("CREATE TABLE wallets (user_id INTEGER PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0)")
    con.execute("INSERT INTO wallets VALUES (?, 0)", (V,))
    con.commit(); con.close()
    db, pm, clubs = edb.EconomyDB(), pmdb.PlayerMarketDB(), cdb.ClubDB()
    for u in (A, B):
        await clubs.create_club(u, f"FC{u}", now)
        await db.claim_daily(u, 0, now)

    # 명문 구단: 선수는 언제 만들어도 같다 · 10개
    t1, t2 = cdb.elite_team("royal"), cdb.elite_team("royal")
    assert t1["lineup"] == t2["lineup"] and len(t1["lineup"]) == 11 and t1["rating"] > 70
    assert len(await clubs.elite_list()) == 10
    price = cdb.ELITE_SIZES["미드"][0]
    assert (await clubs.buy_elite(A, "ideal", now))["reason"] == "balance"
    await db.add_balance(A, price * 3)
    r = await clubs.buy_elite(A, "ideal", now)
    assert r["ok"] and r["cost"] == price and r["prev_owner"] is None
    assert (await clubs.buy_elite(A, "ruhr", now))["reason"] == "one"      # 한 사람 한 구단
    assert (await clubs.buy_elite(A, "ideal", now))["reason"] == "mine"
    # B 가 1.5배로 빼앗는다 → 돈은 A 에게
    await db.add_balance(B, price * 2)
    a_bal = await db.get_balance(A)
    r = await clubs.buy_elite(B, "ideal", now)
    assert r["ok"] and r["cost"] == price * 3 // 2 and r["prev_owner"] == A
    assert await db.get_balance(A) == a_bal + price * 3 // 2
    c = next(x for x in await clubs.elite_list() if x["key"] == "ideal")
    assert c["owner_id"] == B and c["cost"] == round(price * 1.5 * 1.5)

    # 하루 수입: 산 날은 없고 다음 날부터 지난 날만큼
    assert await clubs.settle_elite_income(now) == []
    b_bal = await db.get_balance(B)
    paid = await clubs.settle_elite_income(now + 2 * D)
    assert paid == [{"owner_id": B, "key": "ideal", "days": 2, "amount": 2 * cdb.ELITE_SIZES["미드"][1]}]
    assert await db.get_balance(B) == b_bal + paid[0]["amount"]
    assert await clubs.settle_elite_income(now + 2 * D) == []

    # 스쿼드 B: 고르면 경기 팀이 명문 구단 · 빼앗기면 자동으로 A
    assert (await clubs.set_squad(A, True))["reason"] == "no_elite"
    assert (await clubs.set_squad(B, True))["ok"]
    m = await clubs.match_team(B)
    assert m["squad"] == "B" and m["name"] == "암스테르담 아이디얼" and m["filled"] == 11
    assert (await clubs.match_team(A))["name"] == "FC1"
    await db.add_balance(A, price * 10)
    await clubs.buy_elite(A, "ideal", now)
    assert (await clubs.match_team(B))["name"] == "FC2"

    # 시설: 레벨마다 비용 · 최대 5 · 효과
    await db.set_balance(A, 0)
    assert (await clubs.upgrade_facility(A, "youth"))["reason"] == "balance"
    await db.add_balance(A, sum(cdb.FACILITY_COSTS))
    for lv in range(1, 6):
        assert (await clubs.upgrade_facility(A, "youth"))["level"] == lv
    assert (await clubs.upgrade_facility(A, "youth"))["reason"] == "max"
    assert await db.get_balance(A) == 0
    assert (await clubs.facilities(A))["youth"] == 5
    await db.add_balance(A, 10_000_000)
    info, _ = cdb.prospect_input("유스", "대한민국", "ST", 9, "1-1")

    class R(random.Random):
        def randint(self, a, b):
            return b if (a, b) == (75, 94) else a
    p = await clubs.create_prospect(A, info, now, rng=R(), rookie=False)
    assert p["ok"] and p["pot"] == 99 and p["price"] == cdb.PROSPECT_PRICE     # 94 + 5 · rookie=False 면 반값 없음
    # 경기장: 적중 상금 +4% / Lv
    await db.add_balance(B, cdb.FACILITY_COSTS[0])
    await clubs.upgrade_facility(B, "stadium")
    r = await clubs.record_official(B, A, 2, 0, now, 10_000, "W", 2.0)
    assert r["delta"] == 10_400

    # 꾸미기
    await db.set_balance(B, cdb.EMBLEM_PRICE)
    assert (await clubs.decorate(B, "emblem", "🦁"))["ok"]
    assert (await clubs.decorate(B, "emblem", "🦁"))["reason"] == "same"
    assert (await clubs.decorate(B, "stadium", "드림 아레나"))["reason"] == "balance"
    t = await clubs.get_team(B)
    assert t["emblem"] == "🦁" and t["stadium"] is None

    # 송금 조건: 새 유저만 · 기존 유저는 없음
    assert await db.transfer_locks(V) == []
    locks = {x["name"]: x for x in await db.transfer_locks(A)}
    assert locks["출석 7일"]["value"] == 1 and len(locks) == 4
    sql("UPDATE daily_claims SET total_days=7 WHERE user_id=?", A)
    sql("UPDATE club_official SET w=10 WHERE user_id=?", A)
    await db.mark_tutorial(A, now)
    for k, *_ in edb.ROOKIE_MISSIONS:
        sql("INSERT OR IGNORE INTO rookie_claims(user_id, mission, ts) VALUES(?, ?, 0)", A, k)
    assert await db.transfer_locks(A) == []

    # 쿠폰 → 상자 → 카드 하나
    assert (await db.redeem_coupon(A, "patch25", now))["items"] == {"box": 1}
    assert (await db.redeem_coupon(A, "PATCH25", now))["reason"] == "used"
    r = await db.open_box(A, 1, rng=random.Random(3))
    item, qty = r["cards"][1]
    assert r["ok"] and len(r["cards"]) == 3 and item in edb.BOX_REWARDS and 1 <= qty <= 3
    inv = (await db.inventory(A))[0]
    assert inv.get("box", 0) == 0 and inv[item] >= qty
    assert (await db.open_box(A, 0))["reason"] == "none"

    # 신인 부스트는 rookie=False 서버에서 꺼진다
    win = lambda lv, con: (1000, 1, {"ok": True})                                 # noqa: E731
    assert (await db.play_scout(B, now, win, rookie=False))["delta"] == 1000
    assert (await db.play_scout(B, now + 120, win))["delta"] == 2000


def test_flow():
    asyncio.run(_flow())


def test_release():
    T, other = release.TEST_GUILD, 757761125403066419
    before, after = release.RELEASE_TS - 1000, release.RELEASE_TS + 1000
    assert release.preview(T, before) and not release.preview(other, before) and release.preview(other, after)
    assert release.hidden_commands(other, before) == release.PREVIEW_ONLY
    assert release.hidden_commands(T, before) == release.hidden_commands(other, after) == release.LEGACY_ONLY
    assert release.maintenance(release.RELEASE_TS - 300) and release.maintenance(release.RELEASE_TS + 299)
    assert not release.maintenance(release.RELEASE_TS - 301) and not release.maintenance(release.RELEASE_TS + 300)


if __name__ == "__main__":
    test_flow()
    test_release()
    print("OK: elite")
