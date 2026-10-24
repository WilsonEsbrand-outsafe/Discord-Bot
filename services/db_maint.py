# services/db_maint.py — 점검 시간에 DB 정리 (3.2.0)
# 서버 DB 가 1.3GB 까지 커졌는데, 같은 줄 수를 새로 만들면 200MB 남짓 → 나머지는 지운 기록이 남긴 빈 페이지다.
# 빈 공간이 FREE_RATIO 를 넘고 release.MAINTENANCE 안일 때만, 봇이 디스코드에 붙기 전에 VACUUM 한다 (그동안 봇은 멈춰 있다).
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Optional

import release

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "economy.sqlite3"
FREE_RATIO = 0.3      # 빈 공간이 30% 넘을 때만
DISK_MARGIN = 2.5     # 남은 디스크가 실제 데이터의 2.5배는 있어야 한다 (VACUUM 은 새로 한 벌 쓴다)


def _stat(con) -> tuple[int, int, int]:
    return tuple(con.execute(f"PRAGMA {k}").fetchone()[0] for k in ("page_count", "freelist_count", "page_size"))


def vacuum_if_due(path: Path = DB_PATH, now: Optional[float] = None) -> str:
    """점검 중이고 빈 공간이 많으면 VACUUM. → skip / disk / done / fail (실패해도 봇은 그대로 켜진다)."""
    if not path.exists() or not release.maintenance(now):
        return "skip"
    con = sqlite3.connect(path, timeout=60)
    try:
        pc, fl, ps = _stat(con)
        if not pc or fl / pc < FREE_RATIO:
            return "skip"
        live = (pc - fl) * ps
        free_disk = shutil.disk_usage(path.parent).free
        if free_disk < live * DISK_MARGIN:
            print(f"[DB] VACUUM 건너뜀: 디스크 여유 {free_disk / 1e6:,.0f}MB < 필요 {live * DISK_MARGIN / 1e6:,.0f}MB")
            return "disk"
        print(f"[DB] VACUUM 시작: {pc * ps / 1e6:,.0f}MB (빈 공간 {fl * ps / 1e6:,.0f}MB)", flush=True)
        t = time.monotonic()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("DROP INDEX IF EXISTS idx_pm_price_hist_pid")   # 지울 색인(player_market_db)을 먼저 — VACUUM 이 다시 짓지 않게
        con.execute("VACUUM")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        pc2, _, ps2 = _stat(con)
        print(f"[DB] VACUUM 완료: {pc * ps / 1e6:,.0f}MB → {pc2 * ps2 / 1e6:,.0f}MB ({time.monotonic() - t:.0f}초)", flush=True)
        return "done"
    except Exception as e:
        print(f"[DB] VACUUM 실패 (봇은 그대로 켜짐): {e!r}", flush=True)
        return "fail"
    finally:
        con.close()
