# services/transfer_db.py
# 이적 트래커 — 이미 전송한 기사 URL 추적

import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "transfer_news.sqlite3"


class TransferDB:
    def __init__(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(DB_PATH)

    def _init_db(self) -> None:
        con = self._connect()
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS transfer_seen (
                    url      TEXT    PRIMARY KEY,
                    posted_at INTEGER NOT NULL
                )
            """)
            con.commit()
        finally:
            con.close()

    def is_seen(self, url: str) -> bool:
        con = self._connect()
        try:
            return con.execute(
                "SELECT 1 FROM transfer_seen WHERE url = ?", (url,)
            ).fetchone() is not None
        finally:
            con.close()

    def mark_seen(self, url: str) -> None:
        con = self._connect()
        try:
            con.execute(
                "INSERT OR IGNORE INTO transfer_seen(url, posted_at) VALUES(?, ?)",
                (url, int(time.time())),
            )
            con.commit()
        finally:
            con.close()

    def mark_seen_bulk(self, urls: list[str]) -> None:
        """여러 URL을 한 번에 seen 처리 (초기 기동 시 flood 방지용)."""
        if not urls:
            return
        now = int(time.time())
        con = self._connect()
        try:
            con.executemany(
                "INSERT OR IGNORE INTO transfer_seen(url, posted_at) VALUES(?, ?)",
                [(u, now) for u in urls],
            )
            con.commit()
        finally:
            con.close()

    def cleanup_old(self, days: int = 30) -> int:
        """N일 이상 된 기록 삭제."""
        cutoff = int(time.time()) - days * 86400
        con = self._connect()
        try:
            cur = con.execute(
                "DELETE FROM transfer_seen WHERE posted_at < ?", (cutoff,)
            )
            con.commit()
            return cur.rowcount
        finally:
            con.close()
