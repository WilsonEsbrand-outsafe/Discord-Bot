"""3.8.4: /로비 첫 버전(주간 대선) + 지난 15번 대선 · 양당 구도 · 투표 · 베팅 · 탄핵, 농사 숫돌 주괴 · 씨앗 인챈트 · 낫 +15, 수능 버닝."""
import asyncio
import calendar
import random
import sqlite3
import tempfile
from pathlib import Path

import services.economy_db as edb

edb.DB_PATH = Path(tempfile.mkdtemp()) / "t384.sqlite3"

import release  # noqa: E402
release.RELEASE_384_TS = release.RELEASE_LOBBY_TS = None   # 공개 전(테스트 서버만) 기준으로 본다 — 공개 뒤 동작은 테스트 안에서 시각을 넣어 본다
import services.farm as F  # noqa: E402
from services import burning  # noqa: E402
from services import lobby as L  # noqa: E402


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_392(monkeypatch):
    """3.9.2 는 test_392.py — 여기서는 3.9.1 규칙만 본다."""
    import services.farm as _F
    monkeypatch.setattr(release, "preview_392", lambda *a, **k: False)
    monkeypatch.setattr(_F, "v392", lambda *a, **k: False)

KST = 9 * 3600


def run(coro):
    return asyncio.run(coro)


def _money(uid, n):
    con = sqlite3.connect(edb.DB_PATH)
    con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 0)", (uid,))
    con.execute("UPDATE wallets SET balance=? WHERE user_id=?", (n, uid))
    con.commit(); con.close()


def _bal(uid):
    con = sqlite3.connect(edb.DB_PATH)
    r = con.execute("SELECT balance FROM wallets WHERE user_id=?", (uid,)).fetchone()
    con.close()
    return r[0]


def test_switches():
    assert release.preview_384(release.TEST_GUILD) and not release.preview_384(1)
    assert release.preview_lobby(release.TEST_GUILD) and "로비" in release.hidden_commands(1)


def test_history_two_party():
    h = L.make_history()
    assert len(h) == L.HISTORY_N and h == L.make_history()                      # 늘 같은 기록
    wins = L.party_wins(h)
    assert sum(wins.values()) == L.HISTORY_N and wins[L.MAJORS[0]] + wins[L.MAJORS[1]] >= L.HISTORY_N - 1
    base = L.party_base(h)
    assert abs(sum(base[p] for p in L.MAJORS) - L.MAJOR_SHARE) < 1e-6 and abs(sum(base.values()) - 100) < 1e-6
    rng = random.Random(3)
    for _ in range(30):
        c = L.make_candidates(base, None, rng)
        assert 5 <= len(c) <= 6 and [x["party"] for x in c[:2]] == list(L.MAJORS)
        ch = L.win_chances(c, [0] * len(c), 300)
        assert abs(sum(ch) - 1) < 1e-6
        assert all(L.ODDS_RANGE[0] <= L.odds_of(p) <= L.ODDS_RANGE[1] for p in ch)


def test_votes_rules():
    assert L.votes_cost(0, 1) == 0 and L.votes_cost(0, 3) == 2 * L.VOTE_PRICE and L.votes_cost(1, 2) == 2 * L.VOTE_PRICE
    # 지지자만 혜택, 1등 공헌자는 두 배
    L.ACTIVE.clear(); L.VOTERS.clear()
    L.ACTIVE["attend"] = 1.5
    L.VOTERS.update({1: 1, 2: 2})
    assert L.mult("attend", 1) == 1.5 and L.mult("attend", 2) == 2.0 and L.mult("attend", 3) == 1.0
    L.ACTIVE.clear(); L.VOTERS.clear()
    assert L.top_voter([(5, 0, 10), (673864024030445569, 0, 99), (6, 0, 10), (7, 1, 50)], 0) == 5   # 관리자는 빼고, 같으면 먼저


def test_close_dates():
    first = L.FIRST_CLOSE
    import time as _t
    t = _t.gmtime(first + KST)
    assert (t.tm_mon, t.tm_mday, t.tm_hour, t.tm_wday) == (11, 22, 21, 6)        # 3.9.1: 내일(11/22 일) 21:00 KST 개표
    assert L.close_after(first) == first + 7 * 86400


def test_lobby_flow():
    db = L.LobbyDB()
    now = calendar.timegm((2026, 11, 21, 0, 0, 0))
    A, B, C = 38401, 38402, 38403

    async def flow():
        s = await db.state(A, now)
        assert s["no"] == L.FIRST_NO and s["closes_ts"] == L.FIRST_CLOSE and len(s["history"]) == L.HISTORY_N
        assert s["president"] is None and sum(s["wins"].values()) == L.HISTORY_N
        _money(A, 50_000_000); _money(B, 1_000_000); _money(C, 0)
        r = await db.vote(C, 0, 1, now)                                            # 기본 1표는 무료
        assert r["ok"] and r["cost"] == 0 and _bal(C) == 0
        assert (await db.vote(C, 0, 1, now))["reason"] == "balance"
        assert (await db.vote(C, 1, 1, now))["reason"] == "other"                 # 지지 후보는 한 명
        r = await db.vote(A, 1, 10, now)
        assert r["ok"] and r["cost"] == 9 * L.VOTE_PRICE and _bal(A) == 41_000_000
        assert (await db.vote(A, 1, 91, now))["reason"] == "max"
        assert (await db.bet(B, 1, 100, now))["reason"] == "amount"
        b = await db.bet(B, 1, 500_000, now)
        assert b["ok"] and b["odds"] >= L.ODDS_RANGE[0] and _bal(B) == 500_000
        s = await db.state(A, now)
        assert s["votes"][0] == 1 and s["votes"][1] == 10 and s["my_vote"] == (1, 10)
        for k in range(30):                                                         # 선거운동 뉴스
            await db.news_tick(now + (k + 1) * L.NEWS_GAP[1], random.Random(k))
        assert await db.settle(L.FIRST_CLOSE - 1) is None
        r = await db.settle(L.FIRST_CLOSE + 5, random.Random(1))
        assert r and r["no"] == L.FIRST_NO and r["next"] == L.FIRST_NO + 1 and r["next_closes"] == L.FIRST_CLOSE + 7 * 86400
        assert await db.settle(L.FIRST_CLOSE + 6) is None                          # 한 번만
        w = r["winner"]
        if w == 1:
            assert r["top"] == A and (B in r["paid"] or B in r["caught"])
            assert B not in r["paid"] or _bal(B) == 500_000 + r["paid"][B]
        else:
            assert B not in r["paid"] and _bal(B) == 500_000
        act, voters = L.active_from(r)
        assert act and all(u in voters for u in r["supporters"])
        p = await db.president()
        assert p["no"] == L.FIRST_NO and p["winner"] == w and p["approval"] == L.TERM_START
        # 임기: 나쁜 뉴스만 계속 → 탄핵
        rng = random.Random(0)
        rng.random = lambda: 0.99                                                   # 늘 나쁜 뉴스
        t, imp = L.FIRST_CLOSE + 10, None
        for _ in range(20):
            t += L.TERM_GAP[1] + 1
            x = await db.term_tick(t, rng)
            if x and x["impeached"]:
                imp = x
                break
        assert imp and "탄핵" in imp["impeach_text"]
        p = await db.president()
        assert p["impeached"] and L.active_from(p) == ({}, {})
        assert await db.term_tick(t + 10 ** 6) is None                             # 탄핵 뒤엔 임기 뉴스 없음
        s = await db.state(A, t)
        assert s["no"] == L.FIRST_NO + 1 and len(s["history"]) == L.HISTORY_N + 1 and s["president"]["impeached"]
    run(flow())


def test_lobby_ui_smoke():
    from cogs import lobby as CL
    db = L.LobbyDB()
    now = calendar.timegm((2026, 12, 1, 0, 0, 0))

    async def flow():
        s = await db.state(38410, now)
        e = CL.lobby_embed(None, s)
        text = e.description + "".join(f.name + f.value for f in e.fields) + (e.footer.text or "")
        assert "당선 확률" in text and "배당" in text and "집권 횟수" in text
        assert "·" not in text and len(e) < 6000 and len(e.fields) <= 25
    run(flow())


def test_farm_ingot_enchant_sickle():
    db = F.FarmDB()
    T = 1_800_000_000

    async def flow():
        A = 38420
        fid = (await db.create(A, "plain", T))["id"]
        con = sqlite3.connect(edb.DB_PATH)
        con.execute("UPDATE farms SET whet=20, coins=10000000, energy=1000 WHERE id=?", (fid,))
        con.execute("INSERT INTO farm_seeds(farm_id, key, qty) VALUES(?, 'potato', 60)", (fid,))
        con.commit(); con.close()
        assert (await db.enchant(A, fid, "potato", T))["reason"] == "ingot"
        r = await db.make_ingot(A, fid, T)
        assert r["ok"] and r["made"] == 2 and r["whet"] == 2
        assert (await db.make_ingot(A, fid, T))["reason"] == "whet"
        assert (await db.enchant(A, fid, "carrot", T))["reason"] == "key"         # 당근은 씨앗이 없다
        r = await db.enchant(A, fid, "potato", T)
        assert r["ok"] and r["left"] == 10 and r["eseeds"] == 50
        assert (await db.enchant(A, fid, "potato", T))["reason"] == "seeds"
        f = await db.get(fid, T)
        assert f["ingot"] == 1 and f["seeds"]["potato"] == 10 and f["eseeds"] == {"potato": 50}
        p = await db.plant(A, fid, "potato", T)                                     # 인챈트 씨앗부터
        assert p["ok"] and p["ench"] == p["count"] == 6 and p["ench_left"] == 44 and p["left"] == 10
        con = sqlite3.connect(edb.DB_PATH)                                          # 3.9.1 인챈트 칸은 10% 늦게
        readys = {r for (r,) in con.execute("SELECT ready_ts FROM farm_fields WHERE farm_id=? AND ench=1", (fid,))}
        con.close()
        assert readys == {int(T + (p["ready_ts"] - T) * F.ENCH_SLOW)} and p["ready_ts"] > T
        h = await db.harvest(A, fid, T + 10 ** 5, random.Random(1))
        assert h["ok"] and all(k == "potato" + F.ENCH for (k, _), _ in h["got"].items())
        assert F.value("potato" + F.ENCH, 1) == F.value("potato", 1) * F.ENCH_MULT
        # 낫: 기본 +10, 3.8.4 는 +15 까지
        con = sqlite3.connect(edb.DB_PATH)
        con.execute("UPDATE farms SET sickle=10 WHERE id=?", (fid,))
        con.commit(); con.close()
        assert (await db.enhance(A, fid, T))["reason"] == "max"
        r = await db.enhance(A, fid, T, cap=F.SICKLE_MAX_384, rng=random.Random(0))
        assert r["ok"] and len(F.SICKLE_CHANCE) == F.SICKLE_MAX_384
    run(flow())


def test_burning():
    mid = calendar.timegm((2026, 11, 25, 0, 0, 0))
    assert burning.START == calendar.timegm((2026, 11, 21, 0, 0, 0)) - KST
    assert burning.END == calendar.timegm((2026, 11, 30, 0, 0, 0)) - KST
    L.TESTERS.clear()
    assert burning.mult("farm_grow", 5, mid) == 1.0                              # 공개 전엔 테스트 서버 사람만
    L.TESTERS.add(5)
    assert burning.mult("farm_grow", 5, mid) == 2.0 and burning.mult("farm_grow", 5, burning.END) == 1.0
    old = release.RELEASE_384_TS
    release.RELEASE_384_TS = mid - 10
    try:
        assert burning.mult("attend", 6, mid) == 2.0 and burning.mult("attend", 6, burning.START - 1) == 1.0
    finally:
        release.RELEASE_384_TS = old
        L.TESTERS.clear()


def test_farm_bag_integration():
    """3.8.4 숫돌 · 상자 · 씨앗 · 동물 · 아이템을 사람마다 하나로 — 어느 농장에서 사도 · 써도 같은 보관함."""
    db = F.FarmDB()
    T = 1_800_000_000

    async def flow():
        A = 38430
        G = (await db.create(A, "plain", T))["id"]
        await db.upgrade(A)
        con = sqlite3.connect(edb.DB_PATH)
        con.execute("UPDATE farm_purse SET coins=10000000 WHERE owner=?", (A,))
        con.commit(); con.close()
        H = (await db.create_more(A, "meadow", G, T))["id"]
        assert (await db.buy(A, G, "potato", 30, T))["ok"] and (await db.buy(A, H, "chicken", 2, T))["ok"]
        assert (await db.buy(A, H, "drink", 3, T))["ok"]
        con = sqlite3.connect(edb.DB_PATH)
        con.execute("UPDATE farms SET whet=5, c_gold=1 WHERE id=?", (G,))
        con.execute("UPDATE farms SET whet=4, c_wood=2 WHERE id=?", (H,))
        con.commit(); con.close()
        g = await db.get(G, T)
        assert not g["bag"] and g["seeds"] == {"potato": 30} and g["whet"] == 5                 # 합치기 전엔 농장마다
        assert await db.upgrade_384(A) and not await db.upgrade_384(A)
        g, h = await db.get(G, T), await db.get(H, T)
        for x in (g, h):
            assert x["bag"] and x["seeds"] == {"potato": 30, "chicken": 2} and x["tools"] == {"drink": 3}
            assert x["whet"] == 9 and x["chests"] == {"wood": 3, "silver": 0, "gold": 1}   # G 시작 선물 나무 상자 1 + H 2
        assert (await db.make_ingot(A, H, T))["ok"]                                           # 두 농장 숫돌 합쳐 9개
        p = await db.plant(A, H, "potato", T)                                                 # 다른 농장에서 산 씨앗으로
        assert p["ok"] and p["left"] == 30 - p["count"]
        assert (await db.plant(A, G, "chicken", T))["ok"]
        assert (await db.open_chest(A, H, "gold", T))["ok"]
        g = await db.get(G, T)
        assert g["seeds"] == {"potato": 30 - p["count"], "chicken": 1} and g["ingot"] == 1 and g["chests"]["gold"] == 0
        assert (await db.use_tool(A, G, "drink", T, 3))["ok"] and (await db.get(H, T))["tools"] == {}
        con = sqlite3.connect(edb.DB_PATH)
        assert con.execute("SELECT COUNT(*) FROM farm_seeds WHERE qty != 0 AND farm_id IN (?, ?)", (G, H)).fetchone()[0] == 0   # 농장 쪽엔 남지 않는다
        con.close()
        assert await db.upgrade_all_384() >= 0
    run(flow())


def test_new_animals():
    from cogs import farm as CF
    assert all(k in F.ANIMALS and F.ANIMALS[k][3] in F.PRODUCTS for k in F.NEW_ANIMALS_384)
    assert set(F.NEW_ANIMALS_384) <= set(CF._animals_for(release.TEST_GUILD))
    assert not set(F.NEW_ANIMALS_384) & set(CF._animals_for(1))
    assert CF.shop_ver(release.TEST_GUILD) == 8                                  # 3.9.1 아이템 값
    emojis = [v[0] for v in F.PRODUCTS.values()] + [v[0] for v in F.ANIMALS.values()]
    assert len(set(emojis)) == len(emojis)                                                    # 밭 그림이 겹치지 않게


def test_replant_and_live_refresh():
    """3.8.4 [다시 심기] (마지막으로 심은 것) · 농장 화면 저절로 새로고침 (같은 글을 고친다, 바뀐 게 있을 때만)."""
    import time as _t
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock
    from cogs import farm as CF
    db = F.FarmDB()
    T = int(_t.time())

    async def flow():
        A = 38440
        G = (await db.create(A, "plain", T))["id"]
        await db.upgrade(A)
        await db.upgrade_384(A)
        f = await db.get(G, T)
        assert f["last_plant"] is None
        btn = {b.custom_id: b for b in CF.FarmPanel(None, f).children if hasattr(b, "custom_id")}
        assert "farm:refresh" not in btn and btn["farm:replant"].disabled
        assert (await db.plant(A, G, "carrot", T))["ok"]
        f = await db.get(G, T)
        btn = {b.custom_id: b for b in CF.FarmPanel(None, f).children if hasattr(b, "custom_id")}
        assert f["last_plant"] == "carrot" and btn["farm:replant"].label == "다시 심기: 당근" and not btn["farm:replant"].disabled
        assert "farm:refresh" in {b.custom_id for b in CF.FarmPanel(None, None).children}           # 재시작용 화면
        cog = CF.Farm.__new__(CF.Farm)
        cog.bot, cog.db, cog._live = MagicMock(), db, {}
        cog.big = lambda ch: False
        member = SimpleNamespace(id=A, display_name="t", display_avatar=SimpleNamespace(url="x"), mention="@t")
        th = SimpleNamespace(id=5, guild=SimpleNamespace(id=release.TEST_GUILD, get_member=lambda i: member))
        msg = SimpleNamespace(id=1, channel=th, edit=AsyncMock())
        cog.track(msg, G, "심었어요")
        await cog._live_one(cog._live[5], T + 1)
        await cog._live_one(cog._live[5], T + 1)
        assert msg.edit.await_count == 1                                                            # 그대로면 안 고친다
        cog.track(SimpleNamespace(id=2, channel=SimpleNamespace(id=6, guild=SimpleNamespace(id=1))), G)
        assert 6 not in cog._live                                                                   # 공개 전엔 테스트 서버만
    run(flow())
