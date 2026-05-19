# services/club_db.py
import sqlite3
import asyncio
from pathlib import Path
from typing import Optional, Tuple

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "economy.sqlite3"


class ClubDB:
    def __init__(self):
        self._lock = asyncio.Lock()
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        con = sqlite3.connect(DB_PATH)
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA foreign_keys=ON;")
        return con

    def _init_db(self):
        con = self._connect()
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS clubs (
                    user_id INTEGER PRIMARY KEY,
                    club_name TEXT NOT NULL,
                    created_ts INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            con.commit()
        finally:
            con.close()

    async def _run(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    async def get_club(self, user_id: int) -> Optional[Tuple[int, str, int]]:
        async with self._lock:
            def work():
                con = self._connect()
                try:
                    row = con.execute(
                        "SELECT user_id, club_name, created_ts FROM clubs WHERE user_id=?",
                        (int(user_id),),
                    ).fetchone()
                    return row
                finally:
                    con.close()
            return await self._run(work)

    async def create_club(self, user_id: int, club_name: str, now_ts: int) -> Tuple[bool, str]:
        club_name = (club_name or "").strip()
        if not club_name:
            return (False, "구단 이름이 비어 있습니다.")

        async with self._lock:
            def work():
                con = self._connect()
                try:
                    exists = con.execute(
                        "SELECT 1 FROM clubs WHERE user_id=?",
                        (int(user_id),),
                    ).fetchone()
                    if exists:
                        return (False, "이미 구단이 있습니다.")

                    con.execute(
                        "INSERT INTO clubs(user_id, club_name, created_ts) VALUES(?, ?, ?)",
                        (int(user_id), club_name, int(now_ts)),
                    )
                    con.commit()
                    return (True, "구단 생성 완료")
                finally:
                    con.close()

            return await self._run(work)
