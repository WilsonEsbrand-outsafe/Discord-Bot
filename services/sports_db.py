# services/sports_db.py — 3.2.1: MLB · KBO · NBA 토토 경기 · 배당 · 베팅 (승패 두 갈래, 무승부면 환불)
# 배당은 표에 저장해 둔다 — 재시작(배포)마다 Odds API 를 다시 부르면 무료 크레딧이 녹는다.
import asyncio
import sqlite3
from typing import Optional


DB_PATH = "data/sports.sqlite3"


def _connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""
        CREATE TABLE IF NOT EXISTS sports_bets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sport       TEXT    NOT NULL,
            event_id    TEXT    NOT NULL,
            home        TEXT    NOT NULL,
            away        TEXT    NOT NULL,
            commence_ts INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            pick        TEXT    NOT NULL,
            amount      INTEGER NOT NULL,
            odds        REAL    NOT NULL,
            settled     INTEGER NOT NULL DEFAULT 0,
            won         INTEGER,
            payout      INTEGER NOT NULL DEFAULT 0,
            UNIQUE(event_id, user_id)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS sports_games (
            event_id    TEXT    PRIMARY KEY,
            sport       TEXT    NOT NULL,
            home        TEXT    NOT NULL,
            away        TEXT    NOT NULL,
            commence_ts INTEGER NOT NULL,
            home_odds   REAL,
            away_odds   REAL,
            odds_ts     INTEGER
        )
    """)
    con.commit()
    return con


class SportsDB:
    def __init__(self):
        _connect().close()

    async def _run(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    async def upsert_games(self, sport: str, games: list[dict]):
        """무료 경기 목록 → 표에 넣거나 시각 · 팀만 고친다 (받아 둔 배당은 그대로)."""
        def _fn():
            con = _connect()
            con.executemany("INSERT INTO sports_games (event_id, sport, home, away, commence_ts) VALUES (?,?,?,?,?)"
                            " ON CONFLICT(event_id) DO UPDATE SET home=excluded.home, away=excluded.away,"
                            " commence_ts=excluded.commence_ts",
                            [(g["event_id"], sport, g["home"], g["away"], g["ts"]) for g in games])
            con.commit()
            con.close()
        return await self._run(_fn)

    async def set_odds(self, rows: list[tuple[str, float, float]], now_ts: int):
        def _fn():
            con = _connect()
            con.executemany("UPDATE sports_games SET home_odds=?, away_odds=?, odds_ts=? WHERE event_id=?",
                            [(h, a, now_ts, eid) for eid, h, a in rows])
            con.commit()
            con.close()
        return await self._run(_fn)

    async def list_games(self, sport: str, start_ts: int, end_ts: int) -> list[sqlite3.Row]:
        def _fn():
            con = _connect()
            rows = con.execute("SELECT * FROM sports_games WHERE sport=? AND commence_ts>? AND commence_ts<=? ORDER BY commence_ts",
                               (sport, start_ts, end_ts)).fetchall()
            con.close()
            return rows
        return await self._run(_fn)

    async def get_game(self, event_id: str) -> Optional[sqlite3.Row]:
        def _fn():
            con = _connect()
            row = con.execute("SELECT * FROM sports_games WHERE event_id=?", (event_id,)).fetchone()
            con.close()
            return row
        return await self._run(_fn)

    async def place_bet(self, sport: str, game: dict, user_id: int, pick: str, amount: int, odds: float) -> bool:
        def _fn():
            con = _connect()
            try:
                con.execute("INSERT INTO sports_bets (sport, event_id, home, away, commence_ts, user_id, pick, amount, odds)"
                            " VALUES (?,?,?,?,?,?,?,?,?)",
                            (sport, game["event_id"], game["home"], game["away"], game["ts"], user_id, pick, amount, odds))
                con.commit()
                return True
            except sqlite3.IntegrityError:
                return False
            finally:
                con.close()
        return await self._run(_fn)

    async def get_bet(self, event_id: str, user_id: int) -> Optional[sqlite3.Row]:
        def _fn():
            con = _connect()
            row = con.execute("SELECT * FROM sports_bets WHERE event_id=? AND user_id=?", (event_id, user_id)).fetchone()
            con.close()
            return row
        return await self._run(_fn)

    async def list_unsettled(self, before_ts: int) -> list[sqlite3.Row]:
        """시작 시각이 before_ts 이전인 정산 안 된 베팅."""
        def _fn():
            con = _connect()
            rows = con.execute("SELECT * FROM sports_bets WHERE settled=0 AND commence_ts<=? ORDER BY id", (before_ts,)).fetchall()
            con.close()
            return rows
        return await self._run(_fn)

    async def settle_bet(self, bet_id: int, won: Optional[bool], payout: int) -> bool:
        """won=None 은 무효(환불). 이미 정산됐으면 False (두 번 지급 막기)."""
        def _fn():
            con = _connect()
            cur = con.execute("UPDATE sports_bets SET settled=1, won=?, payout=? WHERE id=? AND settled=0",
                              (None if won is None else int(won), payout, bet_id))
            con.commit()
            con.close()
            return cur.rowcount == 1
        return await self._run(_fn)

    async def list_recent_for_user(self, user_id: int, limit: int = 10) -> list[sqlite3.Row]:
        def _fn():
            con = _connect()
            rows = con.execute("SELECT * FROM sports_bets WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
            con.close()
            return rows
        return await self._run(_fn)
