# test_onboarding.py — 2.8 🎓 입문 튜토리얼: 관문(새 유저만) · 실습 단계 · 자동 설정 · 연습 경기 · 보상 한 번 · 루키 미션 합치기 · 다시 보기
# 실행: venv/Scripts/python.exe test_onboarding.py
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

import cogs.tutorial as CT   # noqa: E402
import release   # noqa: E402
release.RELEASE_TS = release.MAINTENANCE = None   # 2.8 공개 전 상태로 고정: 다른 서버(999 등)는 2.7, 테스트 서버만 2.8
import services.onboarding as OB   # noqa: E402


class Resp:
    def __init__(self, log):
        self.log, self.done = log, False

    def is_done(self):
        return self.done

    async def send_message(self, *a, **k):
        self.done = True
        self.log.append(("send", k.get("embed"), k.get("view")))

    async def edit_message(self, *a, **k):
        self.done = True
        self.log.append(("edit", k.get("embed"), k.get("view")))

    async def defer(self, *a, **k):
        self.done = True

    async def send_modal(self, modal):
        self.done = True
        self.log.append(("modal", None, modal))


def _inter(user, log, guild=release.TEST_GUILD):
    async def edit_orig(*a, **k):
        log.append(("edit", k.get("embed"), k.get("view")))
    return SimpleNamespace(user=user, guild_id=guild, response=Resp(log), edit_original_response=edit_orig)


def _gate(uid):
    con = sqlite3.connect(TMP)
    try:
        return OB.gate_state(con, uid)
    finally:
        con.close()


def _user(uid, dm_ok=True):
    async def send(*a, **k):
        if not dm_ok:
            raise RuntimeError("DM 막힘")
    return SimpleNamespace(id=uid, display_name=f"뉴비{uid}", display_avatar=SimpleNamespace(url="https://x/a.png"),
                           guild=SimpleNamespace(id=release.TEST_GUILD), send=send)


def test_flow():
    cog = CT.Tutorial.__new__(CT.Tutorial)
    cog.bot, cog.db, cog.ob = None, edb.EconomyDB(), OB.OnboardingDB()
    cog.clubs, cog.pm = cdb.ClubDB(), pmdb.PlayerMarketDB()

    async def flow():
        # 관문: 2.5 전부터 있던 유저 · 구단이 있는 유저는 그대로, 새 유저만 튜토리얼
        con = sqlite3.connect(TMP)
        con.execute("INSERT INTO rookie(user_id, start_ts) VALUES(1, 0)")
        con.commit()
        con.close()
        await cog.clubs.create_club(2, "기존 FC", 1)
        assert _gate(1) == "ok" and _gate(2) == "ok" and _gate(3) == "tutorial"

        U = 3
        user = _user(U, dm_ok=False)
        log = []
        inter = _inter(user, log)
        await CT.Tutorial.tutorial.callback(cog, inter)                 # 새 유저 → 입문 튜토리얼
        kind, e, v = log[-1]
        assert isinstance(v, CT.OnboardingView) and "구단 만들기" in e.title and "1/8" in e.description
        nxt = next(c for c in v.children if c.label == "다음")
        assert nxt.disabled                                             # 실습 전에는 다음으로 못 감
        # ① 구단: 이름 모달 → 창단 + 자동 설정
        await v.children[0].callback(_inter(user, log))
        modal = log[-1][2]
        assert isinstance(modal, CT.ClubNameModal)
        modal.name._value = ""
        await modal.on_submit(_inter(user, log))
        kind, e, v = log[-1]
        assert "뉴비3 FC" in e.description and "창단 보너스" in e.description and "주장" in e.description
        club = await cog.clubs.get_team(U)
        assert club["manager"] == OB.DEFAULT_MANAGER and club["medic"] == OB.DEFAULT_MEDIC and club["captain"]
        assert (await cog.clubs.get_team(U))["filled"] == 11 and _gate(U) == "tutorial"   # 끝나기 전엔 관문 유지
        # 중간에 나갔다가 다시 /튜토리얼 → 같은 단계부터
        nxt = next(c for c in v.children if c.label == "다음")
        assert not nxt.disabled
        await nxt.callback(_inter(user, log))
        await CT.Tutorial.tutorial.callback(cog, _inter(user, log))
        assert "선수단" in log[-1][1].title
        v = log[-1][2]
        # ② 자동편성 → ③ 유망주(설명만) → ④ 출석 → ⑤ 연습 경기 → ⑥ 둘러보기 → ⑦ 알림 설정 → ⑧ 상자
        bal0 = await cog.db.get_balance(U)
        for want in ("자동편성", None, "출석", "경기", None):
            if want:
                await v.children[0].callback(_inter(user, log))
                kind, e, v = log[-1]
                assert "✅" in e.description or "⚽" in e.description, e.description
            await next(c for c in v.children if c.label == "다음").callback(_inter(user, log))
            v = log[-1][2]
        assert await cog.db.get_balance(U) == bal0 + CT.ATTEND_REWARD
        team = await cog.clubs.get_team(U)
        assert team["wins"] + team["draws"] + team["losses"] == 1                 # 연습 경기는 내 전적에만
        # ⑦ 알림 설정 (2.9.1): 버튼 → /알림설정과 같은 토글 화면 (처음엔 모두 꺼짐)
        assert "알림 설정" in log[-1][1].title and "모두 꺼져" in log[-1][1].description
        from cogs.notify import NotifyToggleView
        await next(c for c in v.children if c.label == "알림 설정하기").callback(_inter(user, log))
        assert isinstance(log[-1][2], NotifyToggleView) and not any(log[-1][2].settings.values())
        await next(c for c in v.children if c.label == "다음").callback(_inter(user, log))
        v = log[-1][2]
        assert "마무리" in log[-1][1].title
        await v.children[0].callback(_inter(user, log))                           # 상자 열기
        kind, e, v = log[-1]
        assert v is None and "환영" in e.title and "루키 미션 **구단 창단**" in e.description and "첫 경기" in e.description
        assert "DM을 보낼 수 없었어요" in e.description
        inv, _ = await cog.db.inventory(U)
        assert all(inv.get(k, 0) >= n for k, n in OB.FINAL_ITEMS.items())
        assert _gate(U) == "ok"
        bal = await cog.db.get_balance(U)
        # 보상은 한 번만 · 다시 보기는 실습 · 보상 없이
        assert not (await cog.ob.finish(U, 5))["first"] and await cog.db.get_balance(U) == bal
        await CT.Tutorial.tutorial.callback(cog, _inter(user, log))
        tv = log[-1][2]
        assert isinstance(tv, CT.TutorialView)
        replay = next(c for c in tv.children if "다시 보기" in (getattr(c, "label", None) or ""))
        await replay.callback(_inter(user, log))
        kind, e, v = log[-1]
        assert "다시 보기" in e.title and not any("실습" in c.label for c in v.children)
        assert not next(c for c in v.children if c.label == "다음").disabled
        # 2.8 이 아직 안 된 서버: 예전 체크리스트 그대로 (관문도 2.5 방식)
        other = _user(4)
        await CT.Tutorial.tutorial.callback(cog, _inter(other, log, guild=999))
        assert isinstance(log[-1][2], CT.TutorialView) and not any("다시 보기" in (getattr(c, "label", None) or "") for c in log[-1][2].children)
        # 2.8 아이템 장: 한 줄에 효과 하나 + 얻는 곳 한 줄 · 토토 용지 · 판매는 다음 장
        titles = [t for _, t, _ in tv.steps]
        assert titles[titles.index("아이템") + 1] == "토토 용지 · 판매" and "토토 용지 · 판매" not in [t for _, t, _ in CT.TUTORIAL_STEPS]
        body = tv.steps[titles.index("아이템")][2]
        assert body.count("얻는 곳") == 1 and "토토 용지" not in body and "50%" not in body
        # NPC 상대는 아마추어보다 약하다
        assert OB.npc_team()["rating"] < team["rating"]
    asyncio.run(flow())


if __name__ == "__main__":
    test_flow()
    print("OK: onboarding")
