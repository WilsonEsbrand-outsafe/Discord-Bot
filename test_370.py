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


def test_agent():
    from services import agent
    rng = random.Random(370)
    r = agent.make_report(1, rng)
    assert len(r["offers"]) == 3 and 1 <= r["stars"] <= 5
    fees = [o["fee"] for o in r["offers"]]
    probs = [o["prob"] for o in r["offers"]]
    assert fees == sorted(fees) and probs == sorted(probs, reverse=True)   # 큰 구단일수록 크고 어렵다
    assert agent.resolve(r, 2, 3, random.Random(1))["fee"] in (0, r["offers"][2]["fee"] * 3)
    ev1 = agent.expected_per_play(1, 3000)
    assert 30_000 < ev1 < 80_000   # 예전 스카우트(1.5배) 한 판 약 2.2만원보다 크게
    con = sqlite3.connect(TMP)
    assert agent.pending(con, 5) is None
    agent.save(con, 5, r, 0)
    assert agent.pending(con, 5) == r and agent.take(con, 5) == r and agent.take(con, 5) is None
    con.close()


def test_agent_settle():
    from cogs.economy import Economy as E
    from services import agent
    con = sqlite3.connect(TMP)
    con.execute("CREATE TABLE IF NOT EXISTS inventory (user_id INTEGER, item TEXT, qty INTEGER, PRIMARY KEY(user_id, item))")
    eco = E.__new__(E)
    rep = agent.make_report(2, random.Random(3))
    agent.save(con, 9, rep, 0)
    d, xp, info = eco._agent_roll(2, con, 9, 0)
    assert info["agent"] and info["report"] == rep and xp in (3, -1)
    assert d == (int(rep["offers"][0]["fee"] * 2) if info["ok"] else 0)
    assert agent.pending(con, 9) is None
    d, xp, info = eco._scout_roll(1, con, 9, True, True)   # 스킵권: 자동으로 보낸다
    assert info["agent"] and info["found"] is None
    con.close()


def _pm():
    pm = pmdb.PlayerMarketDB()
    now = int(time.time())
    run(pm.ensure_active_pool(now, target=300))
    return pm, now


def _holders(pid):
    con = sqlite3.connect(TMP)
    r = con.execute("SELECT COALESCE(SUM(qty),0) FROM pm_holdings WHERE player_id=? AND qty>0", (pid,)).fetchone()[0]
    con.close()
    return int(r)


def test_unique_pack_and_market():
    pm, now = _pm()
    eco = edb.EconomyDB()
    tok = release.GUILD.set(T)
    try:
        con = sqlite3.connect(TMP)
        pack = {"price": 10_000_000, "min_price": 0, "max_price": None}
        st, drawn = pmdb._draw_from_pool(con, pack, 10_000_000, 30, unique=True)
        ids = [r[0][0] for r in drawn]
        assert st == "OK" and len(set(ids)) == len(ids)          # 한 팩 안 중복 없음
        pid = ids[0]
        con.execute("INSERT INTO pm_holdings(user_id, player_id, qty) VALUES(31, ?, 1)", (pid,))
        con.commit()
        st, drawn = pmdb._draw_from_pool(con, pack, 10_000_000, 30, unique=True)
        assert pid not in [r[0][0] for r in drawn]               # 누가 가진 선수는 안 나온다
        con.close()
        run(eco.add_balance(32, 10**12))
        pm._is_market_open = lambda ts: True
        ok, msg = run(pm.buy_from_market(user_id=32, player_id=pid, qty=1, now_ts=now,
                                         get_balance=eco.get_balance, add_balance=eco.add_balance))
        assert not ok and "주인" in msg
        ok, msg = run(pm.buy_from_market(user_id=32, player_id=ids[1], qty=2, now_ts=now,
                                         get_balance=eco.get_balance, add_balance=eco.add_balance))
        assert not ok
        ok, msg = run(pm.buy_from_market(user_id=32, player_id=ids[1], qty=1, now_ts=now,
                                         get_balance=eco.get_balance, add_balance=eco.add_balance))
        assert ok and _holders(ids[1]) == 1
    finally:
        release.GUILD.reset(tok)
    # 다른 서버(공개 전): 예전처럼 중복 가능
    ok, _ = run(pm.buy_from_market(user_id=33, player_id=pid, qty=1, now_ts=now,
                                   get_balance=eco.get_balance, add_balance=eco.add_balance))
    run(eco.add_balance(33, 10**12))
    ok, _ = run(pm.buy_from_market(user_id=33, player_id=pid, qty=1, now_ts=now,
                                   get_balance=eco.get_balance, add_balance=eco.add_balance))
    assert ok and _holders(pid) == 2


def test_hold_cap():
    pm, now = _pm()
    eco = edb.EconomyDB()
    con = sqlite3.connect(TMP)
    free = [r[0] for r in con.execute(
        f"SELECT player_id FROM pm_players WHERE retired=0 AND player_id NOT LIKE 'AMT_%' "
        f"AND player_id NOT IN ({pmdb._OWNED_SQL}) LIMIT ?", (pmdb.HOLD_CAP_370 + 1,))]
    for p in free[:-1]:
        con.execute("INSERT INTO pm_holdings(user_id, player_id, qty) VALUES(34, ?, 1)", (p,))
    con.commit()
    con.close()
    run(eco.add_balance(34, 10**12))
    pm._is_market_open = lambda ts: True
    tok = release.GUILD.set(T)
    try:
        ok, msg = run(pm.buy_from_market(user_id=34, player_id=free[-1], qty=1, now_ts=now,
                                         get_balance=eco.get_balance, add_balance=eco.add_balance))
        assert not ok and "최대" in msg
        ok, msg, _ = run(pm.buy_pack(user_id=34, pack_type="브론즈", pulls=1, now_ts=now,
                                     get_balance=eco.get_balance, add_balance=eco.add_balance))
        assert not ok and "최대" in msg
    finally:
        release.GUILD.reset(tok)


def test_dedupe():
    pm, now = _pm()
    con = sqlite3.connect(TMP)
    pid = con.execute(f"SELECT player_id FROM pm_players WHERE retired=0 AND player_id NOT LIKE 'AMT_%' "
                      f"AND player_id NOT IN ({pmdb._OWNED_SQL}) LIMIT 1").fetchone()[0]
    price = con.execute("SELECT price FROM pm_market WHERE player_id=?", (pid,)).fetchone()[0]
    con.execute("INSERT INTO pm_holdings(user_id, player_id, qty) VALUES(41, ?, 2)", (pid,))   # 먼저 산 사람 2장
    con.execute("INSERT INTO pm_holdings(user_id, player_id, qty) VALUES(42, ?, 1)", (pid,))   # 선발 명단
    con.execute("CREATE TABLE IF NOT EXISTS club_lineup (user_id INTEGER, slot INTEGER, player_id TEXT)")
    con.execute("INSERT INTO club_lineup(user_id, slot, player_id) VALUES(42, 1, ?)", (pid,))
    for u in (41, 42):
        con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (u,))
    con.commit()
    b41 = con.execute("SELECT balance FROM wallets WHERE user_id=41").fetchone()[0]
    out = run(pm.dedupe_owners_370(now, force=True))
    q = dict(con.execute("SELECT user_id, qty FROM pm_holdings WHERE player_id=?", (pid,)).fetchall())
    assert q[42] == 1 and q[41] == 0                      # 명단에 올린 사람이 갖는다
    assert con.execute("SELECT balance FROM wallets WHERE user_id=41").fetchone()[0] == b41 + 2 * price
    assert any(u == 41 and n == 2 for u, _nm, n, _a in out)
    assert run(pm.dedupe_owners_370(now)) == []           # 한 번만
    con.close()


def test_bot_clubs():
    from services import bot_clubs as bc
    from cogs.bot_clubs import BotClubs
    pm, now = _pm()
    eco = edb.EconomyDB()
    cog = BotClubs.__new__(BotClubs)
    cog.pm, cog.money, cog.rng, cog.bot = pm, eco, random.Random(7), None
    con = sqlite3.connect(TMP)
    # 유저 매물 하나 (봇이 살 만한 값) + 유저 보유 선수 하나
    pid = con.execute(f"SELECT player_id FROM pm_players WHERE retired=0 AND player_id NOT LIKE 'AMT_%' "
                      f"AND player_id NOT IN ({pmdb._OWNED_SQL}) ORDER BY base_value LIMIT 1").fetchone()[0]
    con.execute("INSERT INTO pm_holdings(user_id, player_id, qty) VALUES(51, ?, 1)", (pid,))
    con.commit()
    bc.ACT_PROB, bc.OFFER_PROB = 1.0, 1.0
    try:
        r = run(cog.run_once(now, allowed=lambda u: u == 51, rng=random.Random(7), notify=False))
    finally:
        bc.ACT_PROB, bc.OFFER_PROB = 0.35, 0.12
    n_bots = con.execute("SELECT COUNT(*) FROM bot_club_state").fetchone()[0]
    assert n_bots == len(bc.BOT_IDS)
    assert con.execute("SELECT balance FROM wallets WHERE user_id=101").fetchone()[0] > 0
    assert r["list"] > 0
    offers = con.execute("SELECT proposer_id, receiver_id FROM pm_trades WHERE status='pending' AND proposer_id BETWEEN 101 AND 110").fetchall()
    assert r["offer"] == len(offers) and all(rv == 51 for _p, rv in offers) and len(offers) == 1   # 하루 한 번
    # 봇 매물은 3.7.0 서버에서만 보인다
    assert not any(bc.is_bot(x[1]) for x in run(pm.get_listings("", limit=25)))
    tok = release.GUILD.set(T)
    try:
        assert any(bc.is_bot(x[1]) for x in run(pm.get_listings("", limit=25)))
    finally:
        release.GUILD.reset(tok)
    assert not any(bc.is_bot(r[0]) for r in run(pm.get_ranking(50)))
    con.close()
