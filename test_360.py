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


def test_reserve():
    """2군: 강화 전 유망주 + 유스 + 2군 아마추어 · 1군에선 빠짐 · 1군 아마추어 최소 OVR · 유스 하루 성장 한도."""
    import services.youth as Y
    Y.DB_PATH = TMP

    async def flow():
        now = int(time.time())
        db, clubs, pm, yd = edb.EconomyDB(), cdb.ClubDB(), pmdb.PlayerMarketDB(), Y.YouthDB()
        U = 3601
        await clubs.create_club(U, "이군FC", now)
        await pm.give_amateur_squad(U)
        await db.add_balance(U, 10_000_000)
        info, _ = cdb.prospect_input("이군이", "대한민국", "ST", 10, "1-1")
        pid = (await clubs.create_prospect(U, info, now))["pid"]
        con = sqlite3.connect(TMP)
        con.execute("UPDATE prospects SET ovr=60, pot=90, pro=10, proneness=1 WHERE user_id=?", (U,))
        con.execute("INSERT INTO youth_players(user_id, name, nick, pos, personality, ovr, pot, grade, join_age, joined_ts) "
                    "VALUES(?, '유스킴', '번개', 'MF', 'diligent', 50.0, 80, 'B', 15, ?)", (U, now))
        con.commit(); con.close()
        # 예전 서버: 유망주가 1군 명단에, 아마추어 OVR 50
        tok = release.GUILD.set(OTHER)
        sq = {p["player_id"]: p for p in await clubs.squad(U)}
        assert pid in sq and sq["AMT_GK_01"]["ovr"] == 50
        release.GUILD.reset(tok)
        # 3.6.0 서버: 유망주는 2군, 1군 아마추어 최소 65
        tok = release.GUILD.set(T)
        sq = {p["player_id"]: p for p in await clubs.squad(U)}
        assert pid not in sq and sq["AMT_GK_01"]["ovr"] == cdb.AMATEUR_MIN_360
        t = await clubs.reserve_team(U, now)
        ids = [s["player_id"] for s in t["xi"]]
        assert len(ids) == 11 and pid in ids and any(i.startswith("YY") for i in ids)
        assert all(s["ovr"] == cdb.RESERVE_OVR for s in t["xi"] if s.get("amateur"))
        assert t["prospect"]["pid"] == pid and len(t["youth_ids"]) == 1
        # 2군 경기 경험치 ×0.7
        x = (await clubs.record_prospects([(t["xi"], 1, 0, None, 0)], [], now, rng=NoHurt, v360=True,
                                          xp_mult=cdb.RESERVE_XP, xp_label="2군"))[0]
        assert x["xp"] == round(15 * cdb.RESERVE_XP) and "2군 ×0.7" in x["mods"]
        # OVR 70 이 되면 1군으로
        con = sqlite3.connect(TMP)
        con.execute("UPDATE prospects SET ovr=70 WHERE user_id=?", (U,))
        con.commit(); con.close()
        assert pid in {p["player_id"] for p in await clubs.squad(U)}
        assert (await clubs.reserve_team(U, now))["prospect"] is None
        release.GUILD.reset(tok)
        # 유스 성장: 하루 RESERVE_DAILY 판까지
        yid = t["youth_ids"][0]
        for i in range(Y.RESERVE_DAILY):
            g = await yd.reserve_growth(U, [yid], True, now)
            assert g["grown"] and g["left"] == Y.RESERVE_DAILY - i - 1
        assert (await yd.reserve_growth(U, [yid], True, now))["grown"] == []
        st = await yd.status(U, now)
        assert st["youth"][0]["ovr"] > 50 and not st["tried"]
    asyncio.run(flow())


def test_reserve_match_screen():
    import services.youth as Y
    import cogs.hub as H
    Y.DB_PATH = TMP

    async def flow():
        now = int(time.time())
        hub = H.Hubs.__new__(H.Hubs)
        hub.clubs, hub.youth, hub._reserve_at = cdb.ClubDB(), Y.YouthDB(), {}
        U = 3602
        await hub.clubs.create_club(U, "화면FC", now)

        class Me:
            class display_avatar:
                url = "https://x"
            id, display_name, guild = U, "나", None
        await hub.clubs.create_club(U + 1, "상대FC", now)
        opp = await hub.clubs.reserve_opponent(U, 60, now)
        assert opp["uid"] != U and opp["name"].endswith(" 2군")          # 다른 유저 구단의 2군
        e = await hub._reserve_match(Me, now)
        assert e.title.count("2군") == 2 and ":" in e.title and "화면FC 2군" in e.title
        assert "쉬는 중" in (await hub._reserve_match(Me, now + 5)).title      # 쿨타임
        assert "2군" in await hub._reserve_text(Me, now)
    asyncio.run(flow())


def test_prospect_transfer():
    async def flow():
        now = int(time.time())
        db, clubs = edb.EconomyDB(), cdb.ClubDB()
        A, B, C = 3611, 3612, 3613
        for u in (A, B, C):
            await clubs.create_club(u, f"거래{u}", now)
            await db.add_balance(u, 50_000_000)
        info, _ = cdb.prospect_input("이적생", "대한민국", "CM", 7, "1-1")
        p = await clubs.create_prospect(A, info, now)
        await clubs.set_slot(A, 0, p["pid"])
        await clubs.set_captain(A, p["pid"])
        bal_a = await db.get_balance(A)
        # C 는 이미 현역 유망주 · 영구결번 7 인 구단은 못 받는다
        info2, _ = cdb.prospect_input("원래", "대한민국", "ST", 9, "1-1")
        await clubs.create_prospect(C, info2, now)
        assert (await clubs.transfer_prospect(A, C, p["id"], 0, now))["reason"] == "exists"
        con = sqlite3.connect(TMP)
        con.execute("INSERT INTO prospects(user_id, name, nation, position, number, birthday, foot, height, ovr, pot, aged, "
                    "peak_ovr, peak_age, created_ts, retired_ts, retired_number) VALUES(?, '전설', '대한민국', 'ST', 7, '1-1', "
                    "'오른발', 180, 60, 80, 20, 60, 20, ?, ?, 1)", (B, now - 99, now - 9))
        con.commit()
        assert (await clubs.transfer_prospect(A, B, p["id"], 0, now))["reason"] == "retired_number"
        con.execute("UPDATE prospects SET retired_number=0 WHERE user_id=? AND retired_ts>0", (B,))
        con.commit(); con.close()
        assert (await clubs.transfer_prospect(A, B, p["id"], 10 ** 12, now))["reason"] == "balance"
        assert (await clubs.transfer_prospect(A, A, p["id"], 0, now))["reason"] == "self"
        r = await clubs.transfer_prospect(A, B, p["id"], 20_000_000, now)
        assert r["ok"] and r["user_id"] == B and r["name"] == "이적생"
        assert await db.get_balance(A) == bal_a + 20_000_000
        assert (await clubs.prospects(A, now))["active"] is None
        assert (await clubs.prospects(B, now))["active"]["id"] == p["id"]
        team = await clubs.get_team(A)
        assert team["captain"] is None and all(s.get("player_id") != p["pid"] for s in team["lineup"])
        assert (await clubs.transfer_prospect(A, B, p["id"], 0, now))["reason"] == "none"   # 두 번은 안 된다
    asyncio.run(flow())


def test_prospect_items():
    async def flow():
        now = int(time.time())
        db, clubs = edb.EconomyDB(), cdb.ClubDB()
        U = 3621
        await db.add_balance(U, 50_000_000)
        assert (await clubs.use_prospect_item(U, "xp_book", now))["reason"] == "none"
        for k in ("xp_boost", "xp_book", "rehab", "mental", "elixir"):
            await db.give_item(U, k, 2)
            assert edb.shop_open(k, guild_id=T) and not edb.shop_open(k, guild_id=OTHER, now_ts=0)
        assert (await clubs.use_prospect_item(U, "xp_book", now))["reason"] == "no_prospect"
        info, _ = cdb.prospect_input("아이템", "대한민국", "ST", 11, "1-1")
        p = await clubs.create_prospect(U, info, now - 3 * cdb.PROSPECT_YEAR)   # 20세
        con = sqlite3.connect(TMP)
        con.execute("UPDATE prospects SET ovr=50, xp=0, pot=90, pro=10, confidence=10, proneness=1 WHERE id=?", (p["id"],))
        con.commit()
        r = await clubs.use_prospect_item(U, "xp_book", now)
        assert (r["ovr"], r["xp"]) == (53, 80 - 20 - 23 - 26)   # 50→51 20 · 51→52 23 · 52→53 26
        assert (await clubs.use_prospect_item(U, "rehab", now))["reason"] == "useless"
        con.execute("UPDATE prospects SET injury_games=5, injury='발목 염좌 (경미)' WHERE id=?", (p["id"],))
        con.commit()
        assert (await clubs.use_prospect_item(U, "rehab", now))["ok"]
        assert not (await clubs.prospects(U, now))["active"]["injured"]
        assert (await clubs.use_prospect_item(U, "mental", now))["conf"] == 15
        r = await clubs.use_prospect_item(U, "elixir", now)
        assert (r["age0"], r["age"]) == (20, 19) and (await clubs.prospects(U, now))["active"]["age"] == 19
        assert (await clubs.use_prospect_item(U, "xp_boost", now))["uses"] == cdb.XP_BOOST_USES
        x = (await clubs.record_prospects([([{"player_id": p["pid"]}], 1, 0, None, 0)], [], now, rng=NoHurt, v360=True))[0]
        assert x["xp"] == 30 and any("부스터" in m for m in x["mods"])
        x = (await clubs.record_prospects([([{"player_id": p["pid"]}], 1, 0, None, 0)], [], now, rng=NoHurt, v360=False))[0]
        assert x["xp"] == 15                                                   # 예전 서버엔 안 붙는다
        assert con.execute("SELECT uses FROM buffs WHERE user_id=? AND item='xp_boost'", (U,)).fetchone()[0] == 9
        con.close()
    asyncio.run(flow())


def test_fishing_dex_rewards():
    import services.fishing as F
    F.DB_PATH = TMP

    async def flow():
        db = F.FishingDB()
        U = 3631
        pool = F.by_grade(True, True)
        con = db._connect()
        con.executemany("INSERT INTO fish_dex(user_id, species, cnt, best) VALUES(?,?,1,10)", [(U, k) for k in pool[0]])
        con.commit(); con.close()
        bal = await db.get_balance(U)
        assert await db.claim_dex(U) == [(0, *F.DEX_REWARDS[0])]
        assert await db.claim_dex(U) == [] and await db.get_balance(U) == bal + F.DEX_REWARDS[0][0]
        con = db._connect()
        con.executemany("INSERT INTO fish_dex(user_id, species, cnt, best) VALUES(?,?,1,10)",
                        [(U, k) for b in pool[1:] for k in b])
        con.commit(); con.close()
        got = await db.claim_dex(U)
        assert [g for g, *_ in got] == [1, 2, 3, 4, 5] and await db.dex_rewards(U) == set(range(6))
        assert (await db.inventory(U))[0].get("auto_fish") == sum(r for _, r in F.DEX_REWARDS)
    asyncio.run(flow())


def test_pool_min_ovr():
    """공개(RELEASE_360_TS) 뒤: 새 선수 OVR 65부터 · 아무도 안 가진 65 밑 은퇴 · 풀 1,200명 · 팩 표 바뀜."""
    P = pmdb
    pm = P.PlayerMarketDB()
    assert all(pm._spawn_player(0, 0, min_ovr=65)["ovr"] >= 65 for _ in range(300))
    assert P.PACKS["브론즈"]["price"] == 50_000 and P.pool_size() == P.POOL_SIZE
    old = release.RELEASE_360_TS
    release.RELEASE_360_TS = 1
    try:
        assert P.PACKS["브론즈"]["price"] == 150_000 and P.pool_size() == P.POOL_SIZE_360
        assert set(P.PACKS) == set(P.PACKS_OLD)
        assert all(pm._spawn_player(0, 10)["ovr"] >= 65 for _ in range(300))

        async def flow():
            con = sqlite3.connect(TMP)
            for pid, ovr in (("W1", 50), ("W2", 60), ("W3", 70), ("W4", 55)):
                con.execute("INSERT INTO pm_players(player_id, name, nation, position, age, ovr, pot, pot_grade, base_value, "
                            "retired, created_month, updated_ts) VALUES(?, ?, '대한민국', 'MF', 25, ?, ?, 'C', 1000, 0, 0, 0)",
                            (pid, pid, ovr, ovr))
            con.execute("INSERT INTO pm_holdings(user_id, player_id, qty) VALUES(1, 'W4', 1)")
            con.commit()
            await pm.ensure_active_pool(1000, target=0)
            got = dict(con.execute("SELECT player_id, retired FROM pm_players WHERE player_id LIKE 'W%'").fetchall())
            con.close()
            assert got == {"W1": 1, "W2": 1, "W3": 0, "W4": 0}   # 가진 선수는 남는다
        asyncio.run(flow())
    finally:
        release.RELEASE_360_TS = old


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("✅ test_360 통과")
