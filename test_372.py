"""3.7.2: /스카우트 = 돈 대신 💎 원석 5종 · 시세 · 환전 · 세공 · 보유 한도 (테스트 서버 먼저)."""
import random
import sqlite3

import release
from services import gems


def _con():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE wallets (user_id INTEGER PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0)")
    con.execute("CREATE TABLE inventory (user_id INTEGER, item TEXT, qty INTEGER, PRIMARY KEY(user_id, item))")
    return con


import calendar   # noqa: E402

BEFORE = calendar.timegm((2026, 11, 3, 0, 19, 0))
AFTER = calendar.timegm((2026, 11, 3, 0, 20, 0))


def setup_module():
    """공개 전 동작(다른 서버는 예전 그대로)을 시험하는 테스트들 — 공개 시각을 잠시 비운다."""
    global _SAVED
    _SAVED = release.RELEASE_372_TS
    release.RELEASE_372_TS = None


def teardown_module():
    release.RELEASE_372_TS = _SAVED


def test_switch():
    assert _SAVED == AFTER   # 11/03 09:20 KST (점검 09:20 ~ 09:25)
    release.RELEASE_372_TS = _SAVED
    try:
        assert release.preview_372(release.TEST_GUILD, BEFORE) and not release.preview_372(1, BEFORE)
        assert release.preview_372(1, AFTER)
    finally:
        release.RELEASE_372_TS = None
    assert "원석" in release.hidden_commands(1)   # 3.7.4 부터 테스트 서버에서도 /원석 은 빠진다 (test_374)


def test_prices_stay_in_range():
    con = _con()
    rng = random.Random(1)
    for t in range(0, 600 * 3000, 600):
        for g, (p, _) in gems.prices(con, t, rng).items():
            base = gems.GEMS[g][2]
            assert base * gems.PRICE_LO <= p <= base * gems.PRICE_HI


def test_give_cap_sell():
    con = _con()
    r = gems.give(con, 7, {"diamond": 65, "rough": 3})
    assert r["got"] == {"diamond": 60, "rough": 3} and r["lost"] == {"diamond": 5}
    p = gems.prices(con, 0)
    s = gems.sell(con, 7, "rough", 0)
    assert s["total"] == 3 * p["rough"][0] and gems.bag(con, 7) == {"diamond": 60}
    s = gems.sell(con, 7, None, 0)
    assert s["balance"] == 3 * p["rough"][0] + 60 * p["diamond"][0] and gems.bag(con, 7) == {}


def test_craft():
    con = _con()
    gems.give(con, 7, {"rough": 21})
    r = gems.craft(con, 7, "rough", 100, random.Random(2))
    assert r["ok"] and r["tries"] == 10 and r["used"] == 20 and sum(r["made"].values()) + r["fail"] == 10
    b = gems.bag(con, 7)
    assert b.get("rough") == 1 and b.get("emerald", 0) + b.get("sapphire", 0) == sum(r["made"].values())
    assert gems.craft(con, 7, "rough")["reason"] == "few"
    assert gems.craft(con, 7, "diamond")["reason"] == "top"
    gems.give(con, 7, {"diamond": 60, "ruby": 4})
    assert gems.craft(con, 7, "ruby")["reason"] == "full"


def test_scout_roll_gives_gems():
    from cogs.economy import Economy as E
    con = _con()
    eco = E.__new__(E)
    d, xp, info = eco._scout_roll(3, con, 9, True, True, True)
    n = sum(info["got"].values())
    assert d == 0 and xp == E.GEM_XP_372 and 4 <= n <= 6      # Lv.3: 2~4개 + 2
    assert sum(gems.bag(con, 9).values()) == n


def test_transfer_limit_372():
    from services.economy_db import transfer_limit
    tok = release.GUILD.set(release.TEST_GUILD)
    try:
        assert transfer_limit() == 1_000_000_000
    finally:
        release.GUILD.reset(tok)
    assert transfer_limit() == 100_000_000


def test_stadium_text_372():
    from services.club_db import facility_effect
    tok = release.GUILD.set(release.TEST_GUILD)
    try:
        assert facility_effect("stadium", 2) == "공식경기 상금 +8%"
    finally:
        release.GUILD.reset(tok)
    assert facility_effect("stadium", 2) == "공식경기 적중 상금 +8%"


def test_buy():
    con = _con()
    con.execute("INSERT INTO wallets VALUES(7, 1000000)")
    p = gems.prices(con, 0)
    r = gems.buy(con, 7, "emerald", 10, 0)
    assert r["ok"] and r["n"] == 10 and r["balance"] == 1_000_000 - 10 * p["emerald"][0]
    assert gems.bag(con, 7) == {"emerald": 10}
    assert gems.buy(con, 7, "diamond", 1, 0)["reason"] == "no"
    r = gems.buy(con, 7, "ruby", 0, 0)          # 살 수 있는 만큼 = 잔액 기준
    assert r["ok"] and r["n"] == (1_000_000 - 10 * p["emerald"][0]) // p["ruby"][0]
    assert gems.buy(con, 7, "ruby", 1, 0)["reason"] == "money"
    con.execute("UPDATE wallets SET balance=10000000000 WHERE user_id=7")
    gems.buy(con, 7, "rough", 0, 0)
    assert gems.bag(con, 7)["rough"] == gems.cap("rough") and gems.buy(con, 7, "rough", 1, 0)["reason"] == "full"


def test_dribble_no_cap_372():
    from services import dribble as D
    big = 10 ** 15
    assert D.payout(big, 7) == big + D.MAX_PAYOUT                  # 다른 서버: 50억 제한 그대로
    tok = release.GUILD.set(release.TEST_GUILD)
    try:
        assert D.payout(big, 7) == big * 140 and D.payout(big, 1) == big * 145 // 100
        assert D.payout(10 ** 18 + 7, 2) == (10 ** 18 + 7) * 240 // 100   # 큰 금액도 정확히
    finally:
        release.GUILD.reset(tok)
