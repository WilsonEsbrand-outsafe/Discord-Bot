"""3.7.2: 경기장 = 공식경기 상금 + 레벨 × 4% (테스트 서버 먼저)."""
import asyncio
import sqlite3
import tempfile
from pathlib import Path

import services.club_db as cdb
import services.economy_db as edb
import services.player_market_db as pmdb

TMP = Path(tempfile.mkdtemp()) / "t.sqlite3"
edb.DB_PATH = cdb.DB_PATH = pmdb.DB_PATH = TMP

import release   # noqa: E402


def test_stadium_prize():
    odds = {"W": 2.4, "D": 3.6, "L": 2.6}

    saved, release.RELEASE_372_TS = release.RELEASE_372_TS, None   # 공개 전 동작 (다른 서버는 그대로)

    async def flow():
        edb.EconomyDB()
        clubs = cdb.ClubDB()
        now = 1_810_000_000
        for u in (921, 922):
            await clubs.create_club(u, f"경기장{u}", now)
        con = sqlite3.connect(TMP)
        con.execute("INSERT OR IGNORE INTO club_facilities(user_id) VALUES(921)")
        con.execute("UPDATE club_facilities SET stadium=3 WHERE user_id=921")
        con.commit()
        con.close()
        base = cdb.official_prize("W", odds, 50)["prize"]
        r = await clubs.record_official(921, 922, 2, 0, now, 0, "W", 1.0, weekly=True, prize_odds=odds)
        assert r["prize"]["prize"] == base and "stadium" not in r["prize"]          # 다른 서버: 그대로
        tok = release.GUILD.set(release.TEST_GUILD)
        try:
            r = await clubs.record_official(921, 922, 2, 0, now + 60, 0, "W", 1.0, weekly=True, prize_odds=odds)
        finally:
            release.GUILD.reset(tok)
        assert r["prize"]["stadium"] == 12 and r["prize"]["prize"] == int(round(base * 1.12, -2))
        assert r["week"]["purse"] == base + r["prize"]["prize"]
    try:
        asyncio.run(flow())
    finally:
        release.RELEASE_372_TS = saved
