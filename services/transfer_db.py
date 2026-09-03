# services/transfer_db.py
# 이적 트래커 — 이미 전송한 기사 URL + 주제 추적

import re
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
            # 같은 소식을 여러 매체가 보도하면 URL 은 다르지만 주제는 같다.
            # Google News 를 소스로 쓰면 한 트윗이 4~5건으로 들어온다.
            con.execute("""
                CREATE TABLE IF NOT EXISTS transfer_topics (
                    topic     TEXT    PRIMARY KEY,
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

    def claim_topic(self, signature: str, ttl_seconds: int = 6 * 3600) -> bool:
        """이 주제를 지금 전송해도 되는지. 처음이면 기록하고 True.

        같은 소식을 여러 매체가 보도하면 URL 은 달라도 서명은 같다.
        TTL 안에 이미 보낸 주제면 False 를 돌려 중복 전송을 막는다.
        """
        signature = (signature or "").strip()
        if not signature:
            return True          # 서명을 못 뽑으면 URL 중복 제거에만 맡긴다
        now = int(time.time())
        con = self._connect()
        try:
            row = con.execute(
                "SELECT posted_at FROM transfer_topics WHERE topic = ?", (signature,)
            ).fetchone()
            if row and (now - int(row[0])) < ttl_seconds:
                return False
            con.execute(
                "INSERT INTO transfer_topics(topic, posted_at) VALUES(?, ?) "
                "ON CONFLICT(topic) DO UPDATE SET posted_at = excluded.posted_at",
                (signature, now),
            )
            con.commit()
            return True
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
            con.execute("DELETE FROM transfer_topics WHERE posted_at < ?", (cutoff,))
            con.commit()
            return cur.rowcount
        finally:
            con.close()


# ── 주제 중복 제거 ────────────────────────────────────────────
# 매체 이름·상투어를 걷어내고 남는 고유명사(구단·선수명)로 서명을 만든다.
_SIG_NOISE = {
    "fabrizio", "romano", "here", "transfer", "transfers", "deal", "deals",
    "confirms", "confirmed", "reveals", "revealed", "claims", "report",
    "reports", "latest", "news", "update", "updates", "agreed", "agreement",
    "signing", "signs", "star", "midfielder", "defender", "striker", "winger",
    "forward", "goalkeeper", "move", "bid", "medical", "done", "official",
    "after", "with", "from", "that", "this", "into", "over", "amid", "ahead",
    "gives", "drops", "makes", "player", "club", "clubs", "season", "summer",
}


def topic_signature(title: str) -> str:
    """기사 제목에서 주제 서명을 만든다. 같은 소식이면 같은 값이 나와야 한다.

    Google News 제목은 "... - 매체명" 이라 매체명을 먼저 떼고, 따옴표를 지운 뒤
    대문자로 시작하는 낱말(구단·선수명)만 남긴다. 소문자 낱말까지 넣으면
    같은 소식인데도 매체마다 표현이 달라 서명이 갈린다.

    ponytail: 고유명사 집합 기반 휴리스틱이다. 같은 구단의 서로 다른 영입이
    같은 날 뜨면 뒤엣것이 눌릴 수 있다(TTL 로 완화). 실제로 놓치는 양을 보고
    부족하면 그때 선수명 사전이나 유사도 비교로 올린다.
    """
    title = re.split(r"\s+[-–|]\s+[^-–|]{2,40}$", title.strip())[0]
    title = re.sub(r"[‘’“”'\"]", " ", title)
    words = re.findall(r"[A-Z][A-Za-z'\-]{2,}", title)
    keep = sorted({w.lower() for w in words if w.lower() not in _SIG_NOISE})
    # 고유명사가 둘 이상이면 그것으로 묶는다. 매체마다 표현이 달라도
    # 구단+선수 조합이 같으면 같은 소식이다.
    if len(keep) >= 2:
        return " ".join(keep[:4])
    # 하나뿐이면 구단명만 남은 경우가 대부분이라("chelsea"), 그걸로 묶으면
    # 같은 날 서로 다른 소식까지 눌린다. 대신 정규화한 제목으로 떨어뜨려
    # 제목이 사실상 같은 재탕만 걸러낸다.
    norm = re.sub(r"[^a-z0-9 ]", " ", title.lower())
    return "t:" + " ".join(norm.split())
