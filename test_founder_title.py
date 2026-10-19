# test_founder_title.py — 2.8 창단 멤버 영구 칭호: 한 번만 지급 · 도박 계열 결과에만 이름 옆 표시 · 공개 전엔 테스트 서버만
# 실행: venv/Scripts/python.exe test_founder_title.py
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

import release
import services.economy_db as edb
from services import ui

edb.DB_PATH = Path(tempfile.mkdtemp()) / "t.sqlite3"


def _member(uid, gid):
    return SimpleNamespace(id=uid, display_name="규완", display_avatar=SimpleNamespace(url="https://x/a.png"),
                           guild=SimpleNamespace(id=gid))


def test_grant_once():
    edb.EconomyDB()
    con = sqlite3.connect(edb.DB_PATH)
    rows = con.execute("SELECT user_id, title, source FROM user_titles ORDER BY user_id").fetchall()
    assert sorted(r[0] for r in rows) == sorted(edb.FOUNDER_IDS) and {r[1] for r in rows} == {"🏅 창단 멤버"}
    assert {r[2] for r in rows} == {"founder"}
    assert con.execute("SELECT 1 FROM eco_migrations WHERE name='founder-2.8'").fetchone()   # 지급 표시
    # 누가 칭호를 지워도 다시 켤 때 또 주지 않는다 (한 번만)
    con.execute("DELETE FROM user_titles WHERE user_id=?", (edb.FOUNDER_IDS[0],))
    con.commit()
    ui.TITLES.clear()
    edb.EconomyDB()
    assert con.execute("SELECT COUNT(*) FROM user_titles").fetchone()[0] == 3
    assert edb.FOUNDER_IDS[0] not in ui.TITLES and ui.TITLES[edb.FOUNDER_IDS[1]] == "🏅 창단 멤버"
    con.close()


def test_display():
    founder, other = edb.FOUNDER_IDS[1], 42
    ui.TITLES[founder] = "🏅 창단 멤버"
    m = _member(founder, release.TEST_GUILD)
    for sec in ("🎙️ 경마 중계", "🎙️ 달팽이 레이스 중계", "🎙️ 조정 중계", "🎙️ F1 중계", "🎙️ 양궁 중계",
                "🎙️ 페널티킥 중계", "🎙️ 야구 중계", "🎙️ 농구 중계", "🎙️ UFC 중계", "🎙️ 리그 중계", "🎰 토토"):
        assert ui.card("t", "", ui.INFO, m, sec).author.name == f"🏅 창단 멤버 규완 · {sec}", sec
    assert ui.card("t", "", ui.INFO, m, "🎒 아이템").author.name == "규완 · 🎒 아이템"          # 도박 계열만
    assert ui.card("t", "", ui.INFO, _member(other, release.TEST_GUILD), "🎙️ 경마 중계").author.name == "규완 · 🎙️ 경마 중계"
    old = release.RELEASE_TS
    try:
        release.RELEASE_TS = None   # 공개 전: 다른 서버엔 아직 안 보인다
        assert ui.card("t", "", ui.INFO, _member(founder, 1), "🎙️ 경마 중계").author.name == "규완 · 🎙️ 경마 중계"
        release.RELEASE_TS = 1      # 공개 뒤: 모든 서버
        assert ui.card("t", "", ui.INFO, _member(founder, 1), "🎙️ 경마 중계").author.name.startswith("🏅 창단 멤버 규완")
    finally:
        release.RELEASE_TS = old


if __name__ == "__main__":
    test_grant_once()
    test_display()
    print("OK: founder title")
