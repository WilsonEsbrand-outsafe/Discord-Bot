# services/ufc_db.py
import asyncio
import sqlite3
from typing import Optional


DB_PATH = "data/ufc.sqlite3"


def _connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
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
