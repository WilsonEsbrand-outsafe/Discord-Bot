# test_management.py — 2.8 구단 경영: 관중 · 매출 · 유지비(마이너스 없음) · 매진 · 팬 만족도(목표 · 20%씩) · 티켓 · 하루 한 번 정산
# · 최근 10경기 기록 · 공개 전엔 경영을 연 구단만 / 공개 뒤엔 모든 구단 · 스폰서 · 명문 구단 보너스 · 허브 💼 경영 화면 · 계정 삭제
# 실행: venv/Scripts/python.exe test_management.py
import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import services.club_db as cdb
import services.economy_db as edb
import services.player_market_db as pmdb

TMP = Path(tempfile.mkdtemp()) / "t.sqlite3"
edb.DB_PATH = cdb.DB_PATH = pmdb.DB_PATH = TMP

import cogs.hub as CH   # noqa: E402
import cogs.sponsor as CS   # noqa: E402
import release   # noqa: E402
import services.management as M   # noqa: E402
release.RELEASE_311_TS = None   # 3.2.0 공개 전: 경영 수입은 바로 입금 (묵혀두기는 test_club311)

D = 86400
T0 = 1_800_000_000


def test_numbers():
    # 설계안 예시: 새 구단 (전력 50 · 경기장 Lv.0 · 만족도 50 · 보통) → 2,000명 매진 · 약 56만원
    lv0 = {k: 0 for k in cdb.FACILITIES}
    r = M.day_report(50, [50] * 11, lv0, 50, "normal")
    assert r["fans"] == 2000 and r["sellout"] and 540_000 < r["net"] < 570_000, r
    assert 40_000 < M.wages([50] * 11) < 46_000 and 200_000 < M.wages([75] * 11) < 240_000
    # 중간: 전력 70 · 경기장 Lv.2 · 시설 합계 약 1.6억
    mid = {"stadium": 2, "training": 2, "youth": 1, "medical": 1}
    r = M.day_report(70, [70] * 11, mid, 50, "normal")
    assert r["fans"] == 7000 and 1_500_000 < r["net"] < 2_000_000, r
    # 모든 시설 Lv.5 유지비 약 288만 · 비용은 수입까지만
    lv5 = {k: 5 for k in cdb.FACILITIES}
    assert int(M.facility_spent(lv5) * M.FAC_UPKEEP) == 2_880_000
    r = M.day_report(45, [45] * 11, lv5, 10, "high")
    assert r["net"] == 0 and r["cost"] == r["gross"]
    # 티켓: 저가 관중 ↑ · 고가 관중 ↓
    assert M.demand(70, 50, "low") > M.demand(70, 50, "normal") > M.demand(70, 50, "high")
    # 만족도 목표 · 보너스
    t, parts = M.sat_target("WWWWWWWWWW", lv5, "low", 999)
    assert parts == {"form": 20, "facility": 20, "ticket": 10, "aquarium": 10} and t == 100
    assert M.sat_target("LLLLLLLLLL", lv0, "high", 0)[0] == 15
    assert M.fan_bonus(69.9) == (0, 0) and M.fan_bonus(70) == (0.03, 0.05) and M.fan_bonus(85) == (0.05, 0.10)


def test_settle():
    pm = pmdb.PlayerMarketDB()
    db = M.MgmtDB()
    clubs = db.clubs

    async def flow():
        for u in (1, 2, 3):
            await clubs.create_club(u, f"경영{u} FC", 1)
            await pm.give_amateur_squad(u)
            await clubs.auto_lineup(u)
        # 최근 10경기 기록
        for g in ((3, 0), (2, 2), (0, 1)):
            await clubs.record_match(1, 2, *g)
        inp = await db.inputs(1)
        assert inp["recent"] == "WDL"
        assert (await db.inputs(2))["recent"] == "LDW"
        # 경영 화면을 연 구단(1)만 정산 · 같은 날은 정산 없음
        await db.join(1, T0)
        assert await db.settle(T0) == []
        bal = await db.get_balance(1)
        out = await db.settle(T0 + D)
        assert [r["user_id"] for r in out] == [1]
        r = out[0]
        assert await db.get_balance(1) == bal + r["net"] and r["net"] >= 0
        exp = M.SAT_START + M.SAT_MOVE * (r["target"] - M.SAT_START) + (M.SELLOUT_SAT if r["sellout"] else 0)
        assert abs(r["sat"] - round(exp, 1)) < 1e-9
        assert await db.settle(T0 + D + 3600) == []   # 하루 한 번
        # 며칠 밀려도 한 번만
        assert len(await db.settle(T0 + 4 * D)) == 1
        # 티켓은 다음 정산부터
        assert await db.set_ticket(1, "high") and not await db.set_ticket(1, "vip")
        st = await db.state(1)
        r2 = (await db.settle(T0 + 5 * D))[0]
        assert r2["ticket"] == "high" and st["ticket"] == "high"
        assert r2["parts"]["ticket"] == -15
        # 공개 뒤: 모든 구단이 들어오고 다음 날부터 정산
        release.RELEASE_TS, keep = T0, release.RELEASE_TS
        assert {r["user_id"] for r in await db.settle(T0 + 6 * D)} == {1}
        assert {r["user_id"] for r in await db.settle(T0 + 7 * D)} == {1, 2, 3}
        release.RELEASE_TS = keep
        # 명문 구단 보너스: 만족도 85 이상 +10%
        con = db._connect()
        con.execute("UPDATE club_mgmt SET sat=90 WHERE user_id=1")
        con.execute("UPDATE club_mgmt SET sat=72 WHERE user_id=2")
        con.commit()
        con.close()
        b = await db.get_balance(1)
        assert await db.elite_bonus(1, 40_000_000) == 4_000_000 and await db.get_balance(1) == b + 4_000_000
        assert await db.elite_bonus(2, 40_000_000) == 2_000_000
        assert await db.elite_bonus(99, 40_000_000) == 0
        # 스폰서: 2.8 서버에서만 팬 만족도 보너스
        sp = CS.Sponsor.__new__(CS.Sponsor)
        sp.mgmt = db
        assert await sp._fan_bonus(SimpleNamespace(user=SimpleNamespace(id=1), guild_id=release.TEST_GUILD)) == 0.05
        assert await sp._fan_bonus(SimpleNamespace(user=SimpleNamespace(id=1), guild_id=999)) == 0.0
        # 구단을 없애면 정산 없이 넘어간다
        await db.delete_user(3)
        assert 3 not in {r["user_id"] for r in await db.settle(T0 + 8 * D)}
        con = db._connect()
        assert not con.execute("SELECT 1 FROM club_mgmt WHERE user_id=3").fetchone()
        assert not con.execute("SELECT 1 FROM club_form WHERE user_id=3").fetchone()
        con.close()
    real = release.RELEASE_TS
    release.RELEASE_TS = None   # 공개 전 상황부터 (테스트 시각 T0 는 실제 공개 시각 뒤라서)
    try:
        asyncio.run(flow())
    finally:
        release.RELEASE_TS = real


class Resp:
    def __init__(self, log):
        self.log, self.done = log, False

    def is_done(self):
        return self.done

    async def edit_message(self, **k):
        self.done = True
        self.log.append(k)


def test_hub_page():
    uid = 40
    user = SimpleNamespace(id=uid, display_name="경영", display_avatar=SimpleNamespace(url="https://x/a.png"))
    hubs = CH.Hubs.__new__(CH.Hubs)
    hubs.bot, hubs.db, hubs.clubs, hubs.mgmt = None, edb.EconomyDB(), cdb.ClubDB(), M.MgmtDB()

    async def flow():
        v = hubs.club_view(user)
        v.page = next(i for i, p in enumerate(v.pages) if p.title == "경영")
        e = await v.build()
        assert "구단생성" in e.description   # 구단 없음
        await hubs.clubs.create_club(uid, "경영 허브", 1)
        await pmdb.PlayerMarketDB().give_amateur_squad(uid)
        await hubs.clubs.auto_lineup(uid)
        e = await v.build()
        assert "팬 만족도" in e.description and "오늘 밤 예상" in e.description and "매진" in e.description
        log = []
        low = next(c for c in v.children if (getattr(c, "label", "") or "").startswith("저가"))
        await low.callback(SimpleNamespace(user=user, response=Resp(log)))
        assert (await hubs.mgmt.state(uid))["ticket"] == "low"
        assert "저가" in log[-1]["embeds"][-1].description
        assert next(c for c in v.children if (getattr(c, "label", "") or "").startswith("저가")).disabled
        # 정산 카드 (DM)
        r = (await hubs.mgmt.settle(int(CH.time.time()) + D, uid))[0]
        card = CH.mgmt_report_card(r, None, "🏟️ 어제 구단 경영 정산")
        assert "순이익" in card.description and "팬 만족도" in card.description
    asyncio.run(flow())


if __name__ == "__main__":
    test_numbers()
    test_settle()
    test_hub_page()
    lv0 = {k: 0 for k in cdb.FACILITIES}
    for name, rating, lv, sat, t in (("새 구단", 50, lv0, 50, "normal"),
                                     ("중간", 70, {"stadium": 2, "training": 2, "youth": 1, "medical": 1}, 50, "normal"),
                                     ("최상위", 85, {k: 5 for k in cdb.FACILITIES}, 70, "high")):
        r = M.day_report(rating, [int(rating)] * 11, lv, sat, t)
        print(f"{name}: 관중 {r['fans']:,}/{r['capacity']:,} · 매출 {r['gross']:,} · 유지비 {r['wage']:,}+{r['upkeep']:,} · 순이익 {r['net']:,}")
    print("OK: management")
