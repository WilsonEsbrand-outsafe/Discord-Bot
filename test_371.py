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
