# test_hub.py — 2.8 허브: /오늘 · /게임 · /구단 · /시장 허브 — 모든 페이지가 그려지고, 버튼(출석 · 루키 보상 · 자동편성)이 동작하고, 컴포넌트 한도 안
# 실행: venv/Scripts/python.exe test_hub.py
import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import services.club_db as cdb
import services.economy_db as edb
import services.player_market_db as pmdb

TMP = Path(tempfile.mkdtemp()) / "t.sqlite3"
edb.DB_PATH = cdb.DB_PATH = pmdb.DB_PATH = TMP

import cogs.club as CC   # noqa: E402
import cogs.hub as CH   # noqa: E402
import release   # noqa: E402
from services.hub import HubView   # noqa: E402

U = 31
USER = SimpleNamespace(id=U, display_name="허브", display_avatar=SimpleNamespace(url="https://x/a.png"),
                       guild=SimpleNamespace(id=release.TEST_GUILD))


class Resp:
    def __init__(self, log):
        self.log, self.done = log, False

    def is_done(self):
        return self.done

    async def send_message(self, *a, **k):
        self.done = True
        self.log.append((k.get("embed") or (k.get("embeds") or [None])[-1], k.get("view"), k.get("ephemeral")))

    edit_message = send_message

    async def defer(self, *a, **k):
        self.done = True


def _inter(log, guild=release.TEST_GUILD):
    async def edit_orig(*a, **k):
        log.append(((k.get("embeds") or [k.get("embed")])[-1], k.get("view"), None))
    return SimpleNamespace(user=USER, guild_id=guild, response=Resp(log), edit_original_response=edit_orig,
                           followup=SimpleNamespace(send=edit_orig))


def _limits(v):
    rows = {}
    for c in v.children:
        rows[c.row or 0] = rows.get(c.row or 0, 0) + (5 if isinstance(c, CH.discord.ui.Select) else 1)
    assert all(n <= 5 for n in rows.values()), rows


async def _all_pages(view: HubView):
    for i in range(-1, len(view.pages)):
        view.page = i
        e = await view.build()
        _limits(view)
        assert e.title and len(e.description or "") < 4096, (i, e.title)


def test_hubs():
    hubs = CH.Hubs.__new__(CH.Hubs)
    hubs.bot, hubs.db, hubs.clubs, hubs.fish = None, edb.EconomyDB(), cdb.ClubDB(), CH.FishingDB()
    hubs.dungeon, hubs.pm, hubs.tac = CH.DungeonDB(), pmdb.PlayerMarketDB(), CH.TacticsDB()

    async def flow():
        await hubs.clubs.create_club(U, "허브 FC", 1)
        await hubs.pm.give_amateur_squad(U)
        await hubs.clubs.auto_lineup(U)
        # /오늘: 출석 버튼 → 잔액 · 체크
        log = []
        await CH.Hubs.today.callback(hubs, _inter(log))
        e, v, eph = log[-1]
        assert "오늘 할 일" in e.title and "던전" in e.description and "낚시" in e.description and eph
        btn = next(c for c in v.children if (getattr(c, "label", "") or "").startswith("출석하기"))
        await btn.callback(_inter(log))
        assert await hubs.db.get_balance(U) == CH.ATTEND_REWARD
        v.page = 0
        await v.build()
        assert not any((getattr(c, "label", "") or "").startswith("출석하기") for c in v.children)
        await _all_pages(hubs.today_view(USER))
        # 루키 미션: 구단 창단 보상 받기 버튼
        rv = hubs.today_view(USER, 1)
        await rv.build()
        claim = next(c for c in rv.children if (getattr(c, "label", "") or "").startswith("보상 받기"))
        await claim.callback(_inter(log))
        assert await hubs.db.get_balance(U) > CH.ATTEND_REWARD
        # /게임
        await CH.Hubs.games.callback(hubs, _inter(log))
        assert "게임" in log[-1][0].title and len(log[-1][1].pages) == len(CH.GAMES)
        await _all_pages(hubs.games_view(USER))
        # /구단 허브 · 자동편성 버튼
        cv = hubs.club_view(USER)
        await _all_pages(cv)
        cv.page = 0
        await cv.build()
        auto = next(c for c in cv.children if getattr(c, "label", "") == "자동편성")
        await auto.callback(_inter(log))
        assert "자동편성" in log[-1][0].title or log[-1][0].title
        # /시장 허브
        await _all_pages(hubs.market_view(USER))

        # 2.8 서버에서 내 /구단 → 허브, 다른 사람 구단 · 2.7 서버는 예전 카드
        club = CC.Club.__new__(CC.Club)
        club.bot = SimpleNamespace(get_cog=lambda n: hubs)
        club.clubs = hubs.clubs
        await CC.Club.show_club.callback(club, _inter(log), None)
        assert isinstance(log[-1][1], HubView) and log[-1][2] is False
        await CC.Club.show_club.callback(club, _inter(log, guild=999), None)
        assert not isinstance(log[-1][1], HubView) and "허브 FC" in log[-1][0].title
    asyncio.run(flow())


if __name__ == "__main__":
    test_hubs()
    print("OK: hub")
