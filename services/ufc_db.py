# services/ufc_db.py
import asyncio
import sqlite3
from typing import Optional


DB_PATH = "data/ufc.sqlite3"


# 2.10.2: 서버의 ufc.sqlite3 에 event_id 칸이 없는 예전 표가 남아 있어 UFC 베팅이 전부
# 'no such column: event_id' 로 실패했다. 예전 표는 *_legacy 로 이름만 바꿔 보관하고, 옮길 수 있는 칸은 새 표로 옮긴다.
_TABLE_COLS = {"ufc_bets": ("event_id", "match_id", "user_id", "fighter", "amount", "odds", "settled", "won"),
               "ufc_settled": ("event_id",)}


def _migrate(con):
    for table, cols in _TABLE_COLS.items():
        have = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
        if not have or "event_id" in have:
            continue
        legacy = f"{table}_legacy"
        while con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (legacy,)).fetchone():
            legacy += "_"
        con.execute(f"ALTER TABLE {table} RENAME TO {legacy}")
        con.commit()
        _create(con)
        # event_id 가 없던 시절엔 match_id 가 경기 키였다
        src = {c: c for c in cols if c in have}
        if "match_id" in have:
            src["event_id"] = "match_id"
        try:
            con.execute(f"INSERT OR IGNORE INTO {table} ({', '.join(src)}) SELECT {', '.join(src.values())} FROM {legacy}")
            con.commit()
        except sqlite3.Error:
            con.rollback()   # 칸이 너무 달라 못 옮기면 예전 표에 그대로 둔다


def _create(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS ufc_bets (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id   TEXT    NOT NULL,
            match_id   TEXT    NOT NULL,
            user_id    INTEGER NOT NULL,
            fighter    TEXT    NOT NULL,
            amount     INTEGER NOT NULL,
            odds       REAL    NOT NULL,
            settled    INTEGER NOT NULL DEFAULT 0,
            won        INTEGER,
            UNIQUE(event_id, user_id)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS ufc_settled (
            event_id TEXT PRIMARY KEY
        )
    """)
    con.commit()


def _connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    _create(con)
    _migrate(con)
    return con


class UfcDB:
    def __init__(self):
        _connect().close()

    async def _run(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    async def place_bet(self, event_id: str, match_id: str, user_id: int, fighter: str, amount: int, odds: float) -> bool:
        def _fn(event_id, match_id, user_id, fighter, amount, odds):
            con = _connect()
            try:
                con.execute(
                    "INSERT INTO ufc_bets (event_id, match_id, user_id, fighter, amount, odds) VALUES (?,?,?,?,?,?)",
                    (event_id, match_id, user_id, fighter, amount, odds)
                )
                con.commit()
                return True
            except sqlite3.IntegrityError:
                return False
            finally:
                con.close()
        return await self._run(_fn, event_id, match_id, user_id, fighter, amount, odds)

    async def get_bet(self, event_id: str, user_id: int) -> Optional[sqlite3.Row]:
        def _fn(event_id, user_id):
            con = _connect()
            row = con.execute("SELECT * FROM ufc_bets WHERE event_id=? AND user_id=?", (event_id, user_id)).fetchone()
            con.close()
            return row
        return await self._run(_fn, event_id, user_id)

    async def is_settled(self, event_id: str) -> bool:
        def _fn(event_id):
            con = _connect()
            row = con.execute("SELECT 1 FROM ufc_settled WHERE event_id=?", (event_id,)).fetchone()
            con.close()
            return row is not None
        return await self._run(_fn, event_id)

    async def has_unsettled_bets(self) -> bool:
        def _fn():
            con = _connect()
            row = con.execute("SELECT 1 FROM ufc_bets WHERE settled=0 LIMIT 1").fetchone()
            con.close()
            return row is not None
        return await self._run(_fn)

    async def list_unsettled(self) -> list[sqlite3.Row]:
        def _fn():
            con = _connect()
            rows = con.execute("SELECT * FROM ufc_bets WHERE settled=0 ORDER BY id").fetchall()
            con.close()
            return rows
        return await self._run(_fn)

    async def settle_bet(self, bet_id: int, won: bool) -> bool:
        """3.2.1: 베팅 하나만 정산 표시. 이미 정산됐으면 False (두 번 지급 막기)."""
        def _fn(bet_id, won):
            con = _connect()
            cur = con.execute("UPDATE ufc_bets SET settled=1, won=? WHERE id=? AND settled=0", (1 if won else 0, bet_id))
            con.commit()
            con.close()
            return cur.rowcount == 1
        return await self._run(_fn, bet_id, won)

    async def list_bets_for_user(self, user_id: int) -> list[sqlite3.Row]:
        def _fn(user_id):
            con = _connect()
            rows = con.execute("SELECT * FROM ufc_bets WHERE user_id=? AND settled=0", (user_id,)).fetchall()
            con.close()
            return rows
        return await self._run(_fn, user_id)

    async def list_recent_for_user(self, user_id: int, limit: int = 10) -> list[sqlite3.Row]:
        """정산 완료 포함 최근 베팅 (최신순)."""
        def _fn(user_id, limit):
            con = _connect()
            rows = con.execute("SELECT * FROM ufc_bets WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
            con.close()
            return rows
        return await self._run(_fn, user_id, limit)

    async def settle(self, event_id: str, winner: str) -> list[dict]:
        def _fn(event_id, winner):
            con = _connect()
            rows = con.execute("SELECT * FROM ufc_bets WHERE event_id=? AND settled=0", (event_id,)).fetchall()
            results = []
            for row in rows:
                won = row["fighter"].lower() == winner.lower()
                payout = int(row["amount"] * row["odds"]) if won else 0
                con.execute("UPDATE ufc_bets SET settled=1, won=? WHERE id=?", (1 if won else 0, row["id"]))
                results.append({
                    "user_id": row["user_id"],
                    "fighter": row["fighter"],
                    "amount": row["amount"],
                    "odds": row["odds"],
                    "won": won,
                    "payout": payout,
                })
            con.execute("INSERT OR IGNORE INTO ufc_settled VALUES (?)", (event_id,))
            con.commit()
            con.close()
            return results
        return await self._run(_fn, event_id, winner)
