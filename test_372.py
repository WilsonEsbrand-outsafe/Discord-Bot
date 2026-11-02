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


def test_switch():
    assert release.RELEASE_372_TS is None
    assert release.preview_372(release.TEST_GUILD) and not release.preview_372(1)
    assert "원석" in release.hidden_commands(1) and "원석" not in release.hidden_commands(release.TEST_GUILD)


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
