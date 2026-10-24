# test_club311.py — 3.1.1 /구단 통합: 📋 편성(포메이션 · 자리 · 선수 · 주장) · 선발 전부 빼기 · 빈자리 자동 채우기 ·
# 유망주 고정 자동편성 · 🏗️ 시설 · 👔 감독/의료진 버튼 · 아마추어 복구 · 명령어 숨김(테스트 서버 먼저)
# 실행: venv/Scripts/python.exe test_club311.py
import asyncio
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

import services.club_db as cdb
import services.economy_db as edb
import services.player_market_db as pmdb

TMP = Path(tempfile.mkdtemp()) / "t.sqlite3"
edb.DB_PATH = cdb.DB_PATH = pmdb.DB_PATH = TMP

import cogs.hub as CH   # noqa: E402
import release   # noqa: E402
from test_hub import USER, _inter, _all_pages   # noqa: E402

U = USER.id


def test_auto_assign_fixed():
    players = [{"player_id": str(i), "pos": "MF", "ovr": 80} for i in range(11)] + [
        {"player_id": "YP1", "pos": "FW", "ovr": 40}]
    assert "YP1" not in cdb.auto_assign(players, "4-4-2")
    out = cdb.auto_assign(players, "4-4-2", fixed=("YP1",))
    assert "YP1" in out and cdb.FORMATIONS["4-4-2"][out.index("YP1")] in ("ST", "CF", "LW", "RW") or \
        cdb.SLOT_GROUP[cdb.FORMATIONS["4-4-2"][out.index("YP1")]] == "FW"   # 유망주는 제 포지션에 고정
    # current: 앉은 자리는 그대로, 빈자리만
    cur = [None] * 11
    cur[0] = "5"
    out = cdb.auto_assign(players, "4-4-2", current=cur)
    assert out[0] == "5" and all(out)


def test_release_311():
    assert release.LEGACY_311 <= release.hidden_commands(release.TEST_GUILD)
    if release.RELEASE_311_TS is None:
        assert not release.LEGACY_311 & release.hidden_commands(757761125403066419)
        assert release.preview_311(release.TEST_GUILD) and not release.preview_311(1)


def test_club_hub_311():
    hubs = CH.Hubs.__new__(CH.Hubs)
    hubs.bot, hubs.db, hubs.clubs, hubs.fish = None, edb.EconomyDB(), cdb.ClubDB(), CH.FishingDB()
    hubs.dungeon, hubs.pm, hubs.tac, hubs.youth = CH.DungeonDB(), pmdb.PlayerMarketDB(), CH.TacticsDB(), CH.YouthDB()
    hubs.mgmt = CH.MgmtDB()

    def find(v, cond):
        return next(c for c in v.children if cond(c))

    async def flow():
        await hubs.clubs.create_club(U, "편성 FC", 1)
        await hubs.pm.give_amateur_squad(U)
        await hubs.clubs.auto_lineup(U)
        log = []
        # 다른 서버(공개 전)엔 편성 페이지가 없다
        assert "편성" not in [p.title for p in hubs.club_view(USER, guild_id=1).pages]
        cv = hubs.club_view(USER, guild_id=release.TEST_GUILD)
        assert [p.title for p in cv.pages][:2] == ["구단", "편성"]
        await _all_pages(cv)
        # 🧹 전부 빼기: 두 번 눌러야
        cv.page = 0
        await cv.build()
        await find(cv, lambda c: getattr(c, "label", "") == "선발 전부 빼기").callback(_inter(log))
        assert (await hubs.clubs.get_team(U))["filled"] == 11
        await find(cv, lambda c: getattr(c, "label", "") == "정말 다 빼기").callback(_inter(log))
        t = await hubs.clubs.get_team(U)
        assert t["filled"] == 0 and t["captain"] is None
        # 빈자리 자동 채우기
        assert await hubs.clubs.fill_empty(U) == 11 and (await hubs.clubs.get_team(U))["filled"] == 11
        assert await hubs.clubs.fill_empty(U) == 0
        await hubs.clubs.set_slot(U, 3, None)
        assert await hubs.clubs.fill_empty(U) == 1
        # 📋 편성: 포메이션 → 자리 → 선수 비우기 → 주장
        cv.page = 1
        await cv.build()
        sels = [c for c in cv.children if isinstance(c, CH.discord.ui.Select)]
        assert len(sels) == 4
        sels[0]._values = ["4-3-3"]
        await sels[0].callback(_inter(log))
        assert (await hubs.clubs.get_team(U))["formation"] == "4-3-3"
        sels = [c for c in cv.children if isinstance(c, CH.discord.ui.Select)]
        sels[1]._values = ["10"]
        await sels[1].callback(_inter(log))
        assert cv.ctx["slot"] == 10
        sels = [c for c in cv.children if isinstance(c, CH.discord.ui.Select)]
        sels[2]._values = ["-"]
        await sels[2].callback(_inter(log))
        assert (await hubs.clubs.get_team(U))["lineup"][10]["player_id"] is None
        sels = [c for c in cv.children if isinstance(c, CH.discord.ui.Select)]
        pid = next(o.value for o in sels[2].options if o.value != "-")
        sels[2]._values = [pid]
        await sels[2].callback(_inter(log))
        assert (await hubs.clubs.get_team(U))["lineup"][10]["player_id"] == pid
        sels = [c for c in cv.children if isinstance(c, CH.discord.ui.Select)]
        sels[3]._values = [pid]
        await sels[3].callback(_inter(log))
        assert (await hubs.clubs.get_team(U))["captain"] == pid
        # 🏗️ 시설: 돈 부족 → 충전 후 두 번 눌러 업그레이드
        cv.page = [p.title for p in cv.pages].index("시설")
        await cv.build()
        await find(cv, lambda c: getattr(c, "label", "") == "경기장 Lv.1").callback(_inter(log))
        await find(cv, lambda c: (getattr(c, "label", "") or "").startswith("정말 올리기")).callback(_inter(log))
        assert (await hubs.clubs.facilities(U))["stadium"] == 0
        await hubs.db.add_balance(U, cdb.FACILITY_COSTS[0])
        await find(cv, lambda c: getattr(c, "label", "") == "경기장 Lv.1").callback(_inter(log))
        await find(cv, lambda c: (getattr(c, "label", "") or "").startswith("정말 올리기")).callback(_inter(log))
        assert (await hubs.clubs.facilities(U))["stadium"] == 1
        # 👔 감독 · 의료진: 고르기 → 영입
        cv.page = [p.title for p in cv.pages].index("감독 · 의료진")
        await cv.build()
        await hubs.db.add_balance(U, cdb.MANAGERS["rookie"][5])
        sel = [c for c in cv.children if isinstance(c, CH.discord.ui.Select)][0]
        sel._values = ["rookie"]
        await sel.callback(_inter(log))
        await find(cv, lambda c: (getattr(c, "label", "") or "").endswith("영입 (30만원)")).callback(_inter(log))
        assert (await hubs.clubs.get_team(U))["manager"] == "rookie"
        await _all_pages(cv)
    asyncio.run(flow())


def test_amateur_restore():
    con = sqlite3.connect(pmdb.DB_PATH)
    con.execute("UPDATE pm_players SET age=40, ovr=45, pot_grade='C', retired=1 WHERE player_id='AMT_FW_01'")
    con.execute("UPDATE pm_holdings SET qty=0 WHERE player_id='AMT_GK_01'")
    con.commit()
    con.close()
    pmdb.PlayerMarketDB()   # 봇이 켜질 때 되살린다
    con = sqlite3.connect(pmdb.DB_PATH)
    assert con.execute("SELECT age, ovr, pot_grade, retired FROM pm_players WHERE player_id='AMT_FW_01'").fetchone() == (
        22, 50, "아마추어", 0)
    assert con.execute("SELECT qty FROM pm_holdings WHERE player_id='AMT_GK_01' AND user_id=?", (U,)).fetchone()[0] == 1
    con.close()


if __name__ == "__main__":
    test_auto_assign_fixed()
    test_release_311()
    test_club_hub_311()
    test_amateur_restore()
    print("✅ test_club311 통과")
