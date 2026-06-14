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
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id  TEXT    NOT NULL,
            user_id   INTEGER NOT NULL,
            fighter   TEXT    NOT NULL,
            amount    INTEGER NOT NULL,
            odds      REAL    NOT NULL,
            settled   INTEGER NOT NULL DEFAULT 0,
            won       INTEGER,
            UNIQUE(match_id, user_id)
        )
    """)
    con.commit()
    return con


class UfcDB:
    def __init__(self):
        _connect().close()

    async def _run(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    # 베팅 등록 (중복 불가)
    async def place_bet(self, match_id: str, user_id: int, fighter: str, amount: int, odds: float) -> bool:
        def _fn(match_id, user_id, fighter, amount, odds):
            con = _connect()
            try:
                con.execute(
                    "INSERT INTO ufc_bets (match_id, user_id, fighter, amount, odds) VALUES (?,?,?,?,?)",
                    (match_id, user_id, fighter, amount, odds)
                )
                con.commit()
                return True
            except sqlite3.IntegrityError:
                return False
            finally:
                con.close()
        return await self._run(_fn, match_id, user_id, fighter, amount, odds)

    # 내 베팅 조회
    async def get_bet(self, match_id: str, user_id: int) -> Optional[sqlite3.Row]:
        def _fn(match_id, user_id):
            con = _connect()
            row = con.execute(
                "SELECT * FROM ufc_bets WHERE match_id=? AND user_id=?",
                (match_id, user_id)
            ).fetchone()
            con.close()
            return row
        return await self._run(_fn, match_id, user_id)

    # 경기별 베팅 목록
    async def list_bets(self, match_id: str) -> list:
        def _fn(match_id):
            con = _connect()
            rows = con.execute(
                "SELECT * FROM ufc_bets WHERE match_id=?", (match_id,)
            ).fetchall()
            con.close()
            return rows
        return await self._run(_fn, match_id)

    # 정산 — winner 파이터명과 일치하면 당첨
    async def settle(self, match_id: str, winner: str) -> list[dict]:
        def _fn(match_id, winner):
            con = _connect()
            rows = con.execute(
                "SELECT * FROM ufc_bets WHERE match_id=? AND settled=0", (match_id,)
            ).fetchall()
            results = []
            for row in rows:
                won = row["fighter"].lower() == winner.lower()
                payout = int(row["amount"] * row["odds"]) if won else 0
                con.execute(
                    "UPDATE ufc_bets SET settled=1, won=? WHERE id=?",
                    (1 if won else 0, row["id"])
                )
                results.append({
                    "user_id": row["user_id"],
                    "fighter": row["fighter"],
                    "amount": row["amount"],
                    "odds": row["odds"],
                    "won": won,
                    "payout": payout,
                })
            con.commit()
            con.close()
            return results
        return await self._run(_fn, match_id, winner)
