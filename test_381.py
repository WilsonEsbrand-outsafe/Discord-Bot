"""3.8.1: 명문 구단 인수 제안 · 적정 인수 금액 · 2군 쿠폰 테스트는 잔액 마이너스여도 · 낚시 확률 대폭 상향 · 토토 당첨금 쌓아 두고 받기 (테스트 서버 먼저). /농사 는 test_farm.py."""
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
    assert release.TEST_GUILD == 757761125403066419 and not release.preview_381(1374213619793006704)   # 11/11 테스트 서버를 옮겼다
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


def test_toto_hold():
    """토토 당첨금: 테스트 서버 사람은 지갑 대신 쌓아 두고 /내베팅 [당첨금 받기] 로 받는다 · 파산해도 그대로 남는다."""
    from types import SimpleNamespace
    import cogs.toto as ct
    A, B = 38_100, 38_101
    guild = SimpleNamespace(get_member=lambda uid: object() if uid == A else None)
    bot = SimpleNamespace(get_guild=lambda gid: guild if gid == release.TEST_GUILD else None)
    assert release.toto_hold_381(bot, A) and not release.toto_hold_381(bot, B)
    assert not release.toto_hold_381(None, A) and not release.toto_hold_381(SimpleNamespace(), A)

    async def flow():
        db = edb.EconomyDB()
        now = int(time.time())
        await db.toto_upsert_match(match_id="h381", home="아스날", away="첼시", kickoff_ts=now + 3600,
                                   base_home=2.0, base_draw=3.0, base_away=3.0)
        for u in (A, B):
            await db.add_balance(u, 10_000)
            assert await db.toto_place_bet(user_id=u, match_id="h381", pick="1", amount=1_000, odds_locked=2.0, now_ts=now) is None
        ok, _ = await db.toto_set_result_and_settle("h381", "1", now + 9000, hold=lambda uid: release.toto_hold_381(bot, uid))
        assert ok
        assert await db.get_balance(A) == 9_000 and await db.toto_held(A) == 2_000     # 쌓아 둠
        assert await db.get_balance(B) == 11_000 and await db.toto_held(B) == 0        # 바로 지갑
        await db.toto_hold(A, 500)
        assert await db.toto_held(A) == 2_500

        # /내베팅: 받지 않은 당첨금 줄 · [당첨금 받기]
        cog = ct.Toto.__new__(ct.Toto)
        cog.db, cog.ufc, cog.api, cog.odds, cog.bot = db, ct.UfcDB(), None, None, bot
        cog.sports = ct.SportsDB()
        user = SimpleNamespace(id=A, display_name="토토러", display_avatar=SimpleNamespace(url="https://x/a.png"))
        sent = []
        async def rec(*a, **k):
            sent.append(k)
        async def noop(*a, **k):
            pass
        inter = SimpleNamespace(user=user, guild_id=release.TEST_GUILD, response=SimpleNamespace(defer=noop, send_message=rec),
                                followup=SimpleNamespace(send=rec))
        await ct.Toto.my_bets.callback(cog, inter)
        view = sent[-1]["view"]
        assert "받지 않은 당첨금 **2,500원**" in sent[-1]["embed"].description and view.claim and "2,500" in view.claim.label
        other = SimpleNamespace(user=SimpleNamespace(id=B), response=SimpleNamespace(send_message=rec))
        assert not await view.interaction_check(other)
        out = []
        async def edit(**k):
            out.append(k)
        async def say(*a, **k):
            out.append(a[0])
        await view._claim(SimpleNamespace(user=user, response=SimpleNamespace(edit_message=edit), followup=SimpleNamespace(send=say)))
        assert "2,500원" in out[-1] and view.claim.disabled
        assert await db.get_balance(A) == 11_500 and await db.toto_held(A) == 0
        assert (await db.toto_claim(A))["amount"] == 0                                  # 두 번 받지 않는다

        # 파산해도 쌓아 둔 당첨금은 건드리지 않는다
        await db.add_balance(A, -21_500)                                                 # 잔액 -10,000
        await db.toto_hold(A, 4_000)
        r = await db.declare_bankruptcy(A, now, rng=random.Random(1))
        assert r["ok"] and "toto" not in r and await db.toto_held(A) == 4_000
        assert r["balance"] == -10_000 + r["forgiven"]

    asyncio.run(flow())


def test_toto_hold_sports_ufc():
    """야구 · 농구 · UFC 정산도 테스트 서버 사람은 쌓아 둔다."""
    from types import SimpleNamespace
    import cogs.sports_toto as st
    import cogs.ufc_toto as ut
    A = 38_110
    guild = SimpleNamespace(get_member=lambda uid: object() if uid == A else None)
    bot = SimpleNamespace(get_guild=lambda gid: guild if gid == release.TEST_GUILD else None)
    sent = []

    async def fake_notify(bot, eco, uid, key, embed):
        sent.append(embed)
    keep = (st.send_notify, ut.send_notify, ut._fetch_espn, ut.espn_results, ut.match_result)
    st.send_notify = ut.send_notify = fake_notify

    async def flow():
        eco = edb.EconomyDB()
        cog = st.SportsToto.__new__(st.SportsToto)
        cog.bot, cog.eco = bot, eco
        class FakeDB:
            async def settle_bet(self, *a):
                return True
        cog.db = FakeDB()
        b = {"id": 1, "user_id": A, "sport": "mlb", "home": "H", "away": "W", "pick": "H", "amount": 1_000, "odds": 1.5}
        await cog._pay(b, "won")
        assert await eco.toto_held(A) == 1_500 and "/내베팅" in sent[-1].fields[1].value
        await cog._pay(dict(b, user_id=A + 1), "won")
        assert await eco.toto_held(A + 1) == 0 and await eco.get_balance(A + 1) == 1_500
        await cog._pay(b, "void")                                                        # 환불은 바로 지갑
        assert await eco.toto_held(A) == 1_500 and await eco.get_balance(A) == 1_000

        u = ut.UfcToto.__new__(ut.UfcToto)
        u.bot, u.eco = bot, eco
        class FakeUfc:
            async def list_unsettled(self):
                return [{"id": 7, "user_id": A, "match_id": "X|Y", "fighter": "X", "amount": 1_000, "odds": 2.0}]
            async def settle_bet(self, *a):
                return True
        u.db = FakeUfc()
        async def fake_espn(*a):
            return ["e"]
        ut._fetch_espn = fake_espn
        ut.espn_results = lambda events: {}
        ut.match_result = lambda mid, fighter, results: "won"
        await u._settle_from_espn()
        assert await eco.toto_held(A) == 3_500

    try:
        asyncio.run(flow())
    finally:   # 다른 테스트 파일이 같은 모듈을 쓴다
        st.send_notify, ut.send_notify, ut._fetch_espn, ut.espn_results, ut.match_result = keep


def test_rank_exclude():
    """관리자(release.RANK_EXCLUDE)는 자산 · 공식경기(주간 · 시즌 · 주간 보상) · 던전 · 낚시 순위에서 빠진다 (테스트 서버 먼저)."""
    import sqlite3
    import services.dungeon as DG
    ADMIN, A = 673864024030445569, 38_200
    assert ADMIN in release.RANK_EXCLUDE and release.rank_sql("x", False) == ""
    assert release.rank_sql("x", True) == f" AND x NOT IN ({ADMIN})"

    async def flow():
        now = int(time.time())
        edb.EconomyDB()
        clubs, pm = cdb.ClubDB(), pmdb.PlayerMarketDB()
        dg, fi = DG.DungeonDB(), FI.FishingDB()
        con = sqlite3.connect(TMP)
        for uid, bal in ((ADMIN, 10 ** 15), (A, 10 ** 14)):
            con.execute("INSERT OR REPLACE INTO wallets(user_id, balance) VALUES(?, ?)", (uid, bal))
            con.execute("INSERT OR REPLACE INTO club_official_week(user_id, week, points, w, gf, ga, last_ts) "
                        "VALUES(?, ?, ?, 1, 9, 0, ?)", (uid, cdb.week_key(now), 300 if uid == ADMIN else 200, now))
            con.execute("INSERT OR REPLACE INTO club_official(user_id, season, points, w, gf, ga) VALUES(?, ?, ?, 1, 9, 0)",
                        (uid, cdb.season_key(now), 300 if uid == ADMIN else 200))
            con.execute("INSERT OR REPLACE INTO clubs(user_id, club_name) VALUES(?, ?)", (uid, f"FC{uid % 1000}"))
            con.execute("INSERT OR REPLACE INTO dungeon_best(user_id, week, floor, ts) VALUES(?, ?, ?, ?)",
                        (uid, DG.week_key(now), 10 if uid == ADMIN else 5, now))
            con.execute("INSERT INTO fish_log(user_id, species, size, ts, week) VALUES(?, ?, ?, ?, ?)",
                        (uid, next(iter(FI.FISH)), 999.0, now, FI.week_key(now)))
        con.commit()
        con.close()

        ids = lambda rows, k=0: [r[k] if isinstance(r, tuple) else r["user_id"] for r in rows]
        assert ids(await pm.get_ranking(10))[:2] == [ADMIN, A] and ADMIN not in ids(await pm.get_ranking(10, hide=True))
        assert ids(await clubs.weekly_table(now))[:2] == [ADMIN, A] and ADMIN not in ids(await clubs.weekly_table(now, hide=True))
        assert ids(await clubs.official_table(now))[:2] == [ADMIN, A]
        assert ADMIN not in ids(await clubs.official_table(now, admin=True))
        assert (await clubs.weekly_me(A, now))["rank"] == 2 and (await clubs.weekly_me(A, now, hide=True))["rank"] == 1
        assert (await clubs.weekly_me(ADMIN, now, hide=True))["rank"] is None
        assert ids(await dg.weekly_top(now, 10))[:2] == [ADMIN, A] and ADMIN not in ids(await dg.weekly_top(now, 10, hide=True))
        assert ADMIN in ids(await fi.weekly_top(now, 10)) and ADMIN not in ids(await fi.weekly_top(now, 10, hide=True))

        # 주간 보상 (공개 뒤): 관리자는 랭킹 보상 없이, 다른 사람이 1위
        keep = release.RELEASE_381_TS
        release.RELEASE_381_TS = 0
        try:
            paid = {x["user_id"]: x for x in await clubs.settle_weekly(now + 8 * 86400)}
        finally:
            release.RELEASE_381_TS = keep
        assert paid[A]["rank"] == 1 and (ADMIN not in paid or paid[ADMIN]["rank"] is None)

    asyncio.run(flow())
