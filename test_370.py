# test_370.py — 3.7.0 (테스트 서버 먼저)
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

import release   # noqa: E402

T, OTHER = release.TEST_GUILD, 757761125403066419
DAY = 86400


def run(c):
    return asyncio.run(c)


def test_release_switch():
    assert release.preview_370(T) and not release.preview_370(OTHER, 0)


def test_aquarium_freshness_frozen():
    from services.fishing import FishingDB, freshness
    db = FishingDB()
    now = int(time.time())
    con = sqlite3.connect(TMP)
    cur = con.execute("INSERT INTO fish_bag(user_id, species, size, caught_ts) VALUES(1, 'carp', 30, ?)", (now,))
    fid = cur.lastrowid
    cur = con.execute("INSERT INTO fish_bag(user_id, species, size, caught_ts) VALUES(1, 'carp', 30, ?)", (now,))
    old = cur.lastrowid
    con.commit()
    assert run(db.display(1, fid, True, now, v370=True))["ok"]
    assert run(db.display(1, old, True, now, v370=False))["ok"]
    later = now + 3 * DAY
    run(db.display(1, fid, False, later, v370=True))
    run(db.display(1, old, False, later, v370=False))
    rows = {r[0]: r[1] for r in con.execute("SELECT id, caught_ts FROM fish_bag WHERE user_id=1")}
    assert freshness(rows[fid], later) == 1.0          # 3.7.0: 전시한 3일은 안 친다
    assert freshness(rows[old], later) < 1.0           # 예전 서버: 그대로 떨어진다
    con.close()


def test_rod_resell_is_new():
    from services.fishing import FishingDB, ROD_DUR
    db = FishingDB()
    now = int(time.time())
    run(db.add_balance(2, 100_000_000))
    for gid, expect in ((T, ROD_DUR["rod_carbon"]), (OTHER, 50)):
        uid = 2 if gid == T else 3
        run(db.add_balance(uid, 100_000_000))
        assert run(db.buy_item(uid, "rod_carbon", now, 1, gid))["ok"]
        con = sqlite3.connect(TMP)
        con.execute("INSERT OR REPLACE INTO fish_rod VALUES(?, 'rod_carbon', 50)", (uid,))
        con.commit()
        assert run(db.sell_item(uid, "rod_carbon", 1, gid))["ok"]
        assert run(db.buy_item(uid, "rod_carbon", now, 1, gid))["ok"]
        r = con.execute("SELECT dur FROM fish_rod WHERE user_id=? AND rod='rod_carbon'", (uid,)).fetchone()
        assert (r[0] if r else ROD_DUR["rod_carbon"]) == expect
        con.close()


def test_official_prize_nerf():
    odds = {"W": 2.0, "D": 3.0, "L": 4.0}
    old = cdb.official_prize("W", odds, 50)["prize"]
    new = cdb.official_prize("W", odds, 50, cdb.PRIZE_STAKE_370)["prize"]
    assert new < old and abs(new / old - 2 / 3) < 0.01


def test_steroid_buff():
    t0, t1 = cdb.steroid_table(False), cdb.steroid_table(True)
    assert abs(sum(t1.values()) - 1) < 1e-9 and list(t0) == list(t1)
    good = lambda t: t["ovr"] + t["pot"] + t["awaken"]
    bad = lambda t: t["fragile"] + t["doping"] + t["retire"]
    assert good(t1) > good(t0) and bad(t1) < bad(t0)


def test_youth_reroll():
    from services.youth import YouthDB
    db = YouthDB()
    now = int(time.time())
    uid = 10
    run(db.add_balance(uid, 10_000_000))
    assert not edb.shop_open("youth_reroll", now, OTHER) and edb.shop_open("youth_reroll", now, T)
    assert run(db.reroll(uid, now))["reason"] == "none"
    a = run(db.open_tryout(uid, now, rng=random.Random(1), v311=True))["applicants"]
    assert run(db.reroll(uid, now))["reason"] == "item"
    assert run(db.buy_item(uid, "youth_reroll", now, 1, T))["ok"]
    r = run(db.reroll(uid, now, rng=random.Random(2)))
    assert r["ok"] and len(r["applicants"]) == len(a) and r["applicants"] != a
    assert run(db.status(uid, now))["items"]["youth_reroll"] == 0


def test_elite_claim():
    db = cdb.ClubDB()
    now = int(time.time())
    con = sqlite3.connect(TMP)
    key = next(iter(cdb.ELITE_CLUBS))
    con.execute("UPDATE elite_clubs SET owner_id=20, income_day=? WHERE key=?", (cdb.kst_day(now) - 2, key))
    con.commit()
    con.close()
    p = run(db.elite_pending(20, now))
    income = cdb.ELITE_SIZES[cdb.ELITE_CLUBS[key][2]][1]
    assert p["days"] == 2 and p["amount"] == 2 * income
    r = run(db.claim_elite(20, now))
    assert r["ok"] and r["amount"] == 2 * income
    assert run(db.claim_elite(20, now))["reason"] == "empty"
    assert run(db.claim_elite(21, now))["reason"] == "none"


def test_admin_item_autocomplete():
    from services.economy_db import ITEMS
    assert len(ITEMS) > 25 and "lawyer" in ITEMS   # 드롭다운(25개)이 나뉜다 — test_admin


def test_patch_thread():
    from cogs.patch_notes import PATCH_THREADS
    assert PATCH_THREADS[1408836309971243251] == 1566465295202123836


def test_scout_buff_370():
    from cogs.economy import Economy as E
    import cogs.economy as ce
    random.seed(370)
    con = sqlite3.connect(TMP)
    con.execute("CREATE TABLE IF NOT EXISTS inventory (user_id INTEGER, item TEXT, qty INTEGER, PRIMARY KEY(user_id, item))")

    class Me:
        SCOUT_EVENTS = [dict(E.SCOUT_EVENTS[0], success_rate=1.0)]
        TRAIN_RATE_CAP, TRAIN_RATE_PER_LV = 1.0, 0
        SCOUT_WIN_360, SCOUT_WIN_370, SCOUT_FIND_MULT_370 = E.SCOUT_WIN_360, E.SCOUT_WIN_370, E.SCOUT_FIND_MULT_370
        SCOUT_ITEM_PROB_370, SCOUT_ITEMS_370 = 1.0, E.SCOUT_ITEMS_370
        scout_money_mult = staticmethod(E.scout_money_mult)
    old = ce.SCOUT_FIND_PROB
    ce.SCOUT_FIND_PROB = (0,) * 5
    try:
        lo, hi = E.SCOUT_EVENTS[0]["win"]
        for _ in range(30):
            d, _, info = E._scout_roll(Me, 1, con, 77, v360=True, v370=True)
            assert lo * 3 - 100 <= d <= hi * 3 + 100 and info["item"] in E.SCOUT_ITEMS_370
        assert sum(q for (q,) in con.execute("SELECT qty FROM inventory WHERE user_id=77")) == 30
    finally:
        ce.SCOUT_FIND_PROB = old
        con.close()
