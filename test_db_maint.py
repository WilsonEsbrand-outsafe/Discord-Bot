# test_db_maint.py — 3.2.0 점검 때 DB 정리(VACUUM): 점검 중 · 빈 공간 30% 넘을 때만, 데이터는 그대로
# 실행: venv/Scripts/python.exe test_db_maint.py
import sqlite3
import tempfile
from pathlib import Path

import release
from services import db_maint as M


def test_vacuum():
    p = Path(tempfile.mkdtemp()) / "t.sqlite3"
    con = sqlite3.connect(p)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE h(k INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO h(v) VALUES(?)", (("x" * 200,) for _ in range(20000)))
    con.commit()
    con.execute("DELETE FROM h WHERE k > 2000")   # 90% 지움 → 빈 페이지
    con.commit()
    con.close()
    start, end = release.MAINTENANCE
    assert M.vacuum_if_due(p, start - 1) == "skip"          # 점검 전엔 안 한다
    before = p.stat().st_size
    assert M.vacuum_if_due(p, start + 1) == "done"
    con = sqlite3.connect(p)
    assert con.execute("SELECT COUNT(*) FROM h").fetchone()[0] == 2000   # 데이터는 그대로
    assert con.execute("PRAGMA freelist_count").fetchone()[0] == 0
    con.close()
    assert p.stat().st_size < before / 3
    assert M.vacuum_if_due(p, start + 2) == "skip"          # 이미 정리됐으면 다시 안 한다
    assert M.vacuum_if_due(Path(tempfile.mkdtemp()) / "none.sqlite3", start + 1) == "skip"


if __name__ == "__main__":
    test_vacuum()
    print("OK: db_maint")
