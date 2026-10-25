# test_ufc_migrate.py — 2.10.2: event_id 칸이 없는 예전 ufc.sqlite3 를 새 구조로 옮긴다.  실행: python test_ufc_migrate.py
import asyncio
import sqlite3
import tempfile
from pathlib import Path

import services.ufc_db as udb


def main():
    path = str(Path(tempfile.mkdtemp()) / "u.sqlite3")
    con = sqlite3.connect(path)   # 예전 구조 (event_id 없음 · match_id 가 경기 키)
    con.execute("CREATE TABLE ufc_bets (id INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT NOT NULL, user_id INTEGER NOT NULL,"
                " fighter TEXT NOT NULL, amount INTEGER NOT NULL, odds REAL NOT NULL, settled INTEGER NOT NULL DEFAULT 0,"
                " won INTEGER, UNIQUE(match_id, user_id))")
    con.execute("CREATE TABLE ufc_settled (match_id TEXT PRIMARY KEY)")
    con.execute("INSERT INTO ufc_bets (match_id, user_id, fighter, amount, odds) VALUES ('A|B', 1, 'A', 1000, 1.5)")
    con.execute("INSERT INTO ufc_settled VALUES ('C|D')")
    con.commit()
    con.close()

    udb.DB_PATH = path
    db = udb.UfcDB()

    async def flow():
        assert await db.place_bet("e1", "X|Y", 1, "X", 500, 2.0)            # 예전엔 no such column: event_id
        assert (await db.get_bet("e1", 1))["amount"] == 500
        old = await db.get_bet("A|B", 1)                                     # 예전 베팅도 옮겨졌다
        assert old and old["amount"] == 1000 and old["match_id"] == "A|B"
        assert await db.is_settled("C|D") and await db.has_unsettled_bets()
        # 3.2.1: 베팅 하나씩 정산 — 두 번째는 False (두 번 지급 막기)
        bet = (await db.list_unsettled())[0]
        assert await db.settle_bet(bet["id"], True) and not await db.settle_bet(bet["id"], True)
    asyncio.run(flow())

    con = sqlite3.connect(path)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"ufc_bets_legacy", "ufc_settled_legacy"} <= names                # 예전 표는 지우지 않고 보관
    con.close()
    udb.UfcDB()                                                              # 두 번째부터는 아무것도 안 한다
    print("OK: ufc migrate")


if __name__ == "__main__":
    main()
