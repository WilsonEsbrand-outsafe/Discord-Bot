# test_360.py — 3.6.0 (테스트 서버 먼저)
# 실행: venv/Scripts/python.exe test_360.py
import asyncio
import random
import sqlite3
import tempfile
import time
from pathlib import Path

import services.club_db as cdb
import services.economy_db as edb
import services.player_market_db as pmdb

TMP = Path(tempfile.mkdtemp()) / "t.sqlite3"
edb.DB_PATH = cdb.DB_PATH = pmdb.DB_PATH = TMP

import release   # noqa: E402

T, OTHER = release.TEST_GUILD, 757761125403066419


class NoHurt:
    @staticmethod
    def random():
        return 1.0


def test_release_switch():
    assert release.preview_360(T) and not release.preview_360(OTHER, 0)


def _stats(table):
    ev = sum(p * float(m) for p, m, *_ in table)
    one = sum(p for p, m, *_ in table if m in ("1", "-1"))
    win = sum(p for p, m, *_ in table if float(m) > 0)
    return ev, one, win


def test_tables_less_one():
    """1배는 더 줄고, 합 · 수익 확률 · 기대값은 3.4.1 그대로."""
    from cogs.economy import Economy as E
    for key in ("PK", "BAT", "HOOP", "UFC"):
        old, new = getattr(E, f"{key}_TABLE_341"), getattr(E, f"{key}_TABLE_360")
        assert abs(sum(p for p, *_ in new) - 1) < 1e-9
        (ev0, one0, win0), (ev1, one1, win1) = _stats(old), _stats(new)
        assert abs(ev0 - ev1) < 1e-9 and abs(win0 - win1) < 1e-9
        assert one1 < one0 - 0.09 and one1 > 0.3, key   # 드물게는 나온다
        assert [r[1:] for r in new] == [r[1:] for r in old]   # 배수 · 멘트(장면 그림) 그대로


def test_race_prizes():
    import cogs.race as R
    rng = random.Random(360)
    ones = lambda old: sum(1 in [abs(x) for x in R.prizes_341(4, rng, old)] for _ in range(3000)) / 3000
    assert ones(R.PRIZE_OLD_360) < ones(R.PRIZE_OLD_341) - 0.1
    assert all(sum(R.race_prizes(4, T)) == 0 and sum(R.race_prizes(6, T)) <= 0 for _ in range(50))


def test_prospect_no_daily_cap():
    """하루 판수 제한 · 같은 상대 반복 감소가 없다 (전력 차 · 친선은 그대로)."""
    async def flow():
        now = int(time.time())
        db, clubs = edb.EconomyDB(), cdb.ClubDB()
        await db.add_balance(361, 10_000_000)
        info, _ = cdb.prospect_input("무제한", "대한민국", "ST", 9, "1-1")
        pid = (await clubs.create_prospect(361, info, now))["pid"]
        con = sqlite3.connect(TMP)
        con.execute("UPDATE prospects SET pro=10, pot=99, ovr=50 WHERE user_id=361")
        con.commit(); con.close()

        async def play(v360, gap=0, friendly=False):
            return (await clubs.record_prospects([([{"player_id": pid}], 1, 0, 777, gap)], [], now, rng=NoHurt,
                                                 friendly=friendly, v360=v360))[0]
        for _ in range(cdb.PROSPECT_DAILY_GROWTH + 5):
            x = await play(True)
        assert x["xp"] == 15 and not x["mods"]          # 25번째 · 같은 상대여도 그대로
        assert (await play(False))["xp"] is None         # 예전 서버: 오늘 판수 끝
        assert (await play(True, gap=25))["xp"] == round(15 * 0.4)   # 전력 차 보정은 남는다
        assert (await play(True, friendly=True))["xp"] == round(15 * 0.5)
    asyncio.run(flow())


def test_scout_buff():
    from cogs.economy import Economy as E
    random.seed(1)

    class Me:
        SCOUT_EVENTS = [dict(E.SCOUT_EVENTS[0], success_rate=1.0)]
        TRAIN_RATE_CAP, TRAIN_RATE_PER_LV, SCOUT_WIN_360 = 1.0, 0, E.SCOUT_WIN_360
        scout_money_mult = staticmethod(E.scout_money_mult)
    import services.player_market_db as P
    old = P.SCOUT_FIND_PROB
    P.SCOUT_FIND_PROB = (0,) * 5
    try:
        import cogs.economy as ce
        ce.SCOUT_FIND_PROB = P.SCOUT_FIND_PROB
        lo, hi = E.SCOUT_EVENTS[0]["win"]
        for _ in range(50):
            d, _, info = E._scout_roll(Me, 1, None, 1, v360=True)
            assert lo * 1.5 - 100 <= d <= hi * 1.5 + 100
    finally:
        P.SCOUT_FIND_PROB = old


def test_fishing_weekly_all():
    import services.fishing as F
    F.DB_PATH = TMP

    async def flow():
        db = F.FishingDB()
        now = int(time.time())
        con = db._connect()
        wk = F.week_key(now)
        con.executemany("INSERT INTO fish_log VALUES(?,?,?,?,?)", [
            (1, "tuna", 300, now, wk), (1, "tuna", 280, now, wk), (2, "goldcarp", 60, now, wk), (3, "anchovy", 17, now, wk)])
        con.commit(); con.close()
        assert [t["user_id"] for t in await db.weekly_top(now)] == [2, 1, 3]   # 금붕어(높은 등급) 먼저
        allf = await db.weekly_top(now, 10, per_user=False)
        assert [t["user_id"] for t in allf] == [2, 1, 1, 3] and [t["size"] for t in allf[1:3]] == [300, 280]
    asyncio.run(flow())


def test_weekly_ranking():
    """공식경기 주간 랭킹: 내가 건 경기만 · 하루 WEEKLY_DAILY 판까지 · 다음 주에 한 번만 보상."""
    async def flow():
        db, clubs = edb.EconomyDB(), cdb.ClubDB()
        now = 1_800_000_000
        for u in (901, 902, 903):
            await clubs.create_club(u, f"주간{u}", now)
        for _ in range(cdb.WEEKLY_DAILY + 3):
            r = await clubs.record_official(901, 902, 2, 0, now, 1000, "W", 2.0, weekly=True)
        assert not r["week"]["counted"] and r["week"]["points"] == 3 * cdb.WEEKLY_DAILY
        r = await clubs.record_official(901, 902, 1, 0, now + 86400, 1000, "W", 2.0, weekly=True)   # 다음 날은 다시
        if cdb.week_key(now + 86400) == cdb.week_key(now):
            assert r["week"]["counted"]
        await clubs.record_official(903, 901, 1, 1, now, 1000, "D", 2.0, weekly=True)
        await clubs.record_official(902, 903, 0, 1, now, 1000, "W", 2.0)   # 예전 서버: 주간 기록 없음
        top = await clubs.weekly_table(now)
        assert [t["user_id"] for t in top] == [901, 903]                     # 902 는 걸지 않았다 (상대만)
        assert (await clubs.weekly_me(903, now))["rank"] == 2 and await clubs.weekly_me(902, now) is None
        assert await clubs.settle_weekly(now) == []                          # 이번 주는 아직
        before = await db.get_balance(901)
        later = now + 8 * 86400
        paid = await clubs.settle_weekly(later)
        assert [(x["user_id"], x["rank"]) for x in paid] == [(901, 1), (903, 2)]
        assert await db.get_balance(901) - before == cdb.WEEKLY_REWARDS[0][0]
        con = sqlite3.connect(TMP)
        assert con.execute("SELECT qty FROM inventory WHERE user_id=901 AND item='enh_stone'").fetchone()[0] == 3
        con.close()
        assert await clubs.settle_weekly(later) == []                        # 한 번만
    asyncio.run(flow())


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, embed=None, view=None, wait=False, **kw):
        self.sent.append(embed)


class _Inter:
    guild_id = T

    def __init__(self):
        self.followup = _Followup()


def test_official_skip():
    """스킵: 중계 없이 결과 카드 한 장 (기록 · 정산은 그대로)."""
    import cogs.club as CC
    cog = CC.Club.__new__(CC.Club)
    cog.clubs = cdb.ClubDB()
    team = lambda n: {"name": n, "rating": 60, "uid": n == "B" and 2 or 1,
                      "xi": [{"player_id": f"{n}{i}", "name": f"{n}{i}", "pos": g, "ovr": 60}
                             for i, g in enumerate(("GK", "DF", "MF", "FW"))]}
    called = []

    async def after(result):
        called.append(result)
        return "\n정산됨"
    it = _Inter()
    asyncio.run(cog._play_match(it, None, team("A"), team("B"), "sec", "🏆", after=after, note="\n`배당` x", skip=True))
    assert len(it.followup.sent) == 1 and called and "정산됨" in it.followup.sent[0].description
    assert it.followup.sent[0].description.startswith("`배당`")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("✅ test_360 통과")
