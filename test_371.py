"""3.7.1: 선수풀(특히 비싼 선수) 늘리기 · 빚이 있어도 얼티밋팩 교환권으로 열기."""
import asyncio
import sqlite3
import tempfile
import time
from pathlib import Path

import services.club_db as cdb
import services.economy_db as edb
import services.player_market_db as pmdb

TMP = Path(tempfile.mkdtemp()) / "t.sqlite3"
edb.DB_PATH = cdb.DB_PATH = pmdb.DB_PATH = TMP


def run(c):
    return asyncio.run(c)


def test_pool_and_ticket_with_debt():
    pm = pmdb.PlayerMarketDB()
    now = int(time.time())
    run(pm.ensure_active_pool(now, target=0))
    con = sqlite3.connect(TMP)
    free = con.execute("SELECT COUNT(*) FROM pm_players WHERE retired=0 AND player_id NOT LIKE 'AMT_%'").fetchone()[0]
    assert free >= pmdb.FREE_MIN_371
    assert pmdb.PlayerMarketDB._tier_need_371(con) == {}      # 비싼 팩 등급마다 최소 인원이 찼다
    cheap = con.execute("SELECT COUNT(*) FROM pm_players p JOIN pm_market m ON m.player_id=p.player_id "
                        "WHERE p.retired=0 AND m.price < ?", (pmdb.CHEAP_PRICE_371,)).fetchone()[0]
    run(pm.ensure_active_pool(now, target=0))                  # 싼 선수는 CHEAP_CAP_371 명까지만 남는다
    assert con.execute("SELECT COUNT(*) FROM pm_players p JOIN pm_market m ON m.player_id=p.player_id "
                       "WHERE p.retired=0 AND m.price < ?", (pmdb.CHEAP_PRICE_371,)).fetchone()[0] == min(cheap, pmdb.CHEAP_CAP_371)
    ids = [r[0] for r in con.execute("SELECT player_id FROM pm_players WHERE player_id NOT LIKE 'AMT_%'")]
    assert len(ids) == len(set(ids))

    eco = edb.EconomyDB()
    run(eco.add_balance(7, -5_000_000))                        # 빚 500만
    con.execute("INSERT INTO inventory(user_id, item, qty) VALUES(7, 'pack_ticket', 10)")
    con.commit()
    ok, msg, res = run(pm.buy_pack(user_id=7, pack_type="얼티밋", pulls=10, now_ts=now,
                                   get_balance=eco.get_balance, add_balance=eco.add_balance))
    assert ok and "교환권 10장" in msg and len(res) == 10, msg
    assert run(eco.get_balance(7)) == -5_000_000
    ok, msg, _ = run(pm.buy_pack(user_id=7, pack_type="얼티밋", pulls=1, now_ts=now,
                                 get_balance=eco.get_balance, add_balance=eco.add_balance))
    assert not ok and "잔액" in msg                             # 교환권이 없으면 여전히 돈이 있어야
    con.close()


def test_bot_club_shop():
    import random
    from services import bot_clubs as bc
    from cogs.bot_clubs import BotClubs
    pm = pmdb.PlayerMarketDB()
    now = int(time.time())
    run(pm.ensure_active_pool(now, target=0))
    eco = edb.EconomyDB()
    cdb.ClubDB()
    cog = BotClubs.__new__(BotClubs)
    cog.pm, cog.money, cog.rng, cog.bot, cog.clubs = pm, eco, random.Random(3), None, cdb.ClubDB()
    pm._is_market_open = lambda ts: True
    run(cog._db(lambda con: bc.bootstrap(con, now, random.Random(3))))
    bid = bc.BOT_IDS[0]
    em, view = run(cog.club_page(61, bid))
    assert em.title and len(view.children) >= 2
    pid = run(cog._db(lambda con: [x for x in bc.squad(con, bid) if not x["amateur"]][0]["player_id"]))
    price, _w, _n = run(cog.quote(bid, pid, True))
    run(eco.add_balance(61, price))
    ok, msg = run(cog.deal(61, bid, pid, True))                 # 바로 산다
    assert ok, msg
    assert run(pm.get_holding(61, pid)) == 1 and run(pm.get_holding(bid, pid)) == 0 and run(eco.get_balance(61)) == 0
    sell, why, _n = run(cog.quote(bid, pid, False))
    assert sell is not None, why
    ok, msg = run(cog.deal(61, bid, pid, False))                # 바로 판다
    assert ok, msg
    assert run(pm.get_holding(61, pid)) == 0 and run(pm.get_holding(bid, pid)) == 1 and run(eco.get_balance(61)) == sell
    ok, msg = run(cog.deal(61, bid, pid, True))                 # 돈이 없으면 안 된다
    assert not ok and "잔액" in msg
