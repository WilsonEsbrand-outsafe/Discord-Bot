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


def test_agent_auto():
    from cogs.economy import Economy as E
    con = sqlite3.connect(TMP)
    con.execute("CREATE TABLE IF NOT EXISTS inventory (user_id INTEGER, item TEXT, qty INTEGER, PRIMARY KEY(user_id, item))")
    eco = E.__new__(E)
    d, xp, info = eco._agent_auto(2, con, 9)
    rep = info["report"]
    assert info["agent"] and xp in (3, -1) and info["offer"] == rep["offers"][__import__("services.agent").agent.best_auto(rep)]
    assert d == (int(info["offer"]["fee"] * 2) if info["ok"] else 0)
    d, xp, info = eco._scout_roll(1, con, 9, True, True)   # 스킵권도 같은 방식
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


def test_convert():
    pm, now = _pm()
    con = sqlite3.connect(TMP)
    edb.EconomyDB()
    free = [r[0] for r in con.execute(f"SELECT player_id FROM pm_players WHERE retired=0 AND player_id NOT LIKE 'AMT_%' "
                                      f"AND player_id NOT IN ({pmdb._OWNED_SQL}) LIMIT 3")]
    price = {p: con.execute("SELECT price FROM pm_market WHERE player_id=?", (p,)).fetchone()[0] for p in free}
    con.execute("INSERT INTO pm_holdings(user_id, player_id, qty) VALUES(41, ?, 2)", (free[0],))
    con.execute("INSERT INTO pm_holdings(user_id, player_id, qty) VALUES(42, ?, 1)", (free[0],))
    con.execute("INSERT INTO pm_holdings(user_id, player_id, qty) VALUES(42, 'AMT_GK1', 1)")
    con.execute("INSERT INTO pm_listings(seller_id, player_id, qty, price_per, listed_at, expires_at, instant_sell_at, status) "
                "VALUES(42, ?, 1, 1, ?, ?, ?, 'active')", (free[1], now, now + 999, now))
    con.execute("CREATE TABLE IF NOT EXISTS club_lineup (user_id INTEGER, slot INTEGER, player_id TEXT)")
    con.execute("INSERT INTO club_lineup(user_id, slot, player_id) VALUES(42, 1, ?)", (free[0],))
    for u in (41, 42):
        con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (u,))
    con.commit()
    b = {u: con.execute("SELECT balance FROM wallets WHERE user_id=?", (u,)).fetchone()[0] for u in (41, 42)}
    out = run(pm.convert_owners_370(now, force=True))
    assert out[41]["cards"] == 2 and out[41]["money"] == 2 * price[free[0]]
    assert out[42]["cards"] == 2 and out[42]["money"] == price[free[0]] + price[free[1]]
    assert con.execute("SELECT balance FROM wallets WHERE user_id=41").fetchone()[0] == b[41] + 2 * price[free[0]]
    assert con.execute("SELECT qty FROM inventory WHERE user_id=41 AND item='pack_ticket'").fetchone()[0] == pmdb.PACK_TICKETS_370
    left = con.execute("SELECT player_id FROM pm_holdings WHERE user_id IN (41, 42) AND qty>0").fetchall()
    assert left == [("AMT_GK1",)]                             # 아마추어만 남는다
    assert not con.execute("SELECT 1 FROM club_lineup WHERE player_id=?", (free[0],)).fetchone()
    assert run(pm.convert_owners_370(now)) == {}              # 한 번만
    # 🎫 교환권으로 얼티밋팩
    eco = edb.EconomyDB()
    tok = release.GUILD.set(T)
    try:
        bal = run(eco.get_balance(41))
        ok, msg, res = run(pm.buy_pack(user_id=41, pack_type="얼티밋", pulls=2, now_ts=now,
                                       get_balance=eco.get_balance, add_balance=eco.add_balance))
        assert ok and "교환권 2장" in msg and run(eco.get_balance(41)) == bal
        assert con.execute("SELECT qty FROM inventory WHERE user_id=41 AND item='pack_ticket'").fetchone()[0] == pmdb.PACK_TICKETS_370 - 2
    finally:
        release.GUILD.reset(tok)
    con.close()

def test_bot_clubs():
    from services import bot_clubs as bc
    from cogs.bot_clubs import BotClubs
    pm, now = _pm()
    eco = edb.EconomyDB()
    cdb.ClubDB()
    cog = BotClubs.__new__(BotClubs)
    cog.pm, cog.money, cog.rng, cog.bot, cog.clubs = pm, eco, random.Random(7), None, cdb.ClubDB()
    pm._is_market_open = lambda ts: True
    con = sqlite3.connect(TMP)
    con.execute("DELETE FROM bot_club_state") if con.execute(
        "SELECT name FROM sqlite_master WHERE name='bot_club_state'").fetchone() else None
    con.commit()
    run(cog.run_once(now, allowed=lambda u: u == 51, rng=random.Random(7)))
    assert con.execute("SELECT COUNT(*) FROM bot_club_state").fetchone()[0] == len(bc.BOT_IDS)
    assert con.execute("SELECT COUNT(*) FROM clubs WHERE user_id BETWEEN 101 AND 110").fetchone()[0] == 0    # 경기엔 안 나온다
    assert con.execute("SELECT COUNT(*) FROM club_lineup WHERE user_id=101").fetchone()[0] == 11            # 선발 11명
    # 접속 시간은 감독마다 다르고, 접속 시간대 안에서만
    nxt = [con.execute("SELECT next_ts FROM bot_club_state WHERE bot_id=?", (b,)).fetchone()[0] for b in bc.BOT_IDS]
    assert len(set(nxt)) > 1 and all(bc.online(b, t) for b, t in zip(bc.BOT_IDS, nxt))
    # 한 번 접속: 약한 자리를 채우거나 남는 선수를 내놓는다 · 받은 트레이드는 따져 본다
    pid = con.execute("SELECT player_id FROM club_lineup WHERE user_id=101 AND player_id NOT LIKE 'AMT_%' LIMIT 1").fetchone()[0]
    run(eco.add_balance(51, 10**10))
    ok, low, _ = run(pm.create_trade(proposer_id=51, receiver_id=101, proposer_pids=[], receiver_pids=[pid],
                                     proposer_cash=1_000, receiver_cash=0, now_ts=now, get_balance=eco.get_balance))
    price = con.execute("SELECT price FROM pm_market WHERE player_id=?", (pid,)).fetchone()[0]
    ok, high, _ = run(pm.create_trade(proposer_id=51, receiver_id=101, proposer_pids=[], receiver_pids=[pid],
                                      proposer_cash=price * 3, receiver_cash=0, now_ts=now, get_balance=eco.get_balance))
    assert bc.judge_trade(con, 101, low) is False and bc.judge_trade(con, 101, high) is True
    pid2 = con.execute("SELECT player_id FROM club_lineup WHERE user_id=101 AND player_id NOT LIKE 'AMT_%' AND player_id<>? "
                       "LIMIT 1", (pid,)).fetchone()[0]
    p2 = con.execute("SELECT price FROM pm_market WHERE player_id=?", (pid2,)).fetchone()[0]
    ok, close, _ = run(pm.create_trade(proposer_id=51, receiver_id=101, proposer_pids=[], receiver_pids=[pid2],
                                       proposer_cash=int(p2 * 1.4), receiver_cash=0, now_ts=now, get_balance=eco.get_balance))
    did = run(cog.visit(101, now, allowed=lambda u: u == 51, rng=random.Random(1)))
    assert f"trade:{low}:reject" in did and f"trade:{high}:accept" in did
    assert any(d.startswith(f"trade:{close}:counter:") for d in did)            # 조금 모자라면 역제안
    ctid = int(next(d for d in did if d.startswith(f"trade:{close}:counter:")).split(":")[-1])
    ct = con.execute("SELECT proposer_id, receiver_id, receiver_cash FROM pm_trades WHERE trade_id=?", (ctid,)).fetchone()
    assert ct[0] == 101 and ct[1] == 51 and ct[2] > int(p2 * 1.4)
    assert any(d.startswith("routine:+") for d in did)                           # 하루 일과로 돈을 번다
    assert run(pm.get_holding(51, pid)) == 1
    logs = [d for b in bc.BOT_IDS for d in run(cog.visit(b, now, allowed=lambda u: u == 51, rng=random.Random(b)))]
    assert not any(d.startswith(("match:", "pack:")) for d in logs)              # 이적시장 일만 한다
    assert any(d.startswith(("free:", "listing:", "offer:", "list:", "bargain:")) for d in logs)
    assert not any(d.startswith("routine:") for d in run(cog.visit(101, now, allowed=lambda u: u == 51)))   # 일과는 하루 한 번
    # 쉬는 날 · 접속 시간대
    assert any(bc.rest_day(101, d) for d in range(100)) and not all(bc.rest_day(101, d) for d in range(100))
    # 봇 매물은 3.7.0 서버에서만 · 봇은 랭킹에서 빠진다 · 공식경기 상대도 3.7.0 서버에서만
    assert not any(bc.is_bot(x[1]) for x in run(pm.get_listings("", limit=25)))
    assert not any(bc.is_bot(r[0]) for r in run(pm.get_ranking(50)))
    tok = release.GUILD.set(T)
    try:
        assert not any(bc.is_bot(u) for u in run(cdb.ClubDB().official_opponents(51)))
    finally:
        release.GUILD.reset(tok)
    con.close()


def test_official_daily_370():
    db = cdb.ClubDB()
    now = int(time.time())
    assert cdb.weekly_daily(True) == 15 and cdb.weekly_daily(False) == 30
    con = sqlite3.connect(TMP)
    con.execute("DELETE FROM club_official_week WHERE user_id=61")
    con.commit()
    con.close()
    tok = release.GUILD.set(T)
    try:
        for i in range(17):
            run(db.record_official(61, 62, 1, 0, now + i, 0, "W", 1.0, weekly=True))
    finally:
        release.GUILD.reset(tok)
    t = run(db.official_today(61, now + 20))
    assert t["n"] == 15 and t["last"] > 0     # 3.7.0 서버: 하루 15판까지만 센다
