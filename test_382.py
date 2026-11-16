"""3.8.2: 농장 코인 지갑 하나 · 에너지 2배 · 새 작물 · 상자 상향 · 바로 인수 없앰 (농장 · 명문 구단) · 리그 중간 순위 · 농장 스레드 다시 열기."""
import asyncio
import random
import sqlite3
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import services.economy_db as edb

edb.DB_PATH = Path(tempfile.mkdtemp()) / "t382.sqlite3"

import release  # noqa: E402
import services.club_db as cdb  # noqa: E402
import services.farm as F  # noqa: E402
import services.player_market_db as pmdb  # noqa: E402

T = 1_800_000_000


def run(coro):
    return asyncio.run(coro)


def _set_coins(fid, c):
    con = sqlite3.connect(edb.DB_PATH)
    con.execute("UPDATE farms SET coins=? WHERE id=?", (c, fid))
    con.commit(); con.close()


def test_switch():
    assert release.preview_382(release.TEST_GUILD) and release.RELEASE_382_TS is None and not release.preview_382(1)


def test_purse_and_takeover():
    """농장 코인 + 맡아 둔 코인이 지갑 하나로 · 인수해도 코인이 갈라지지 않는다 · 금액과 상관없이 주인이 수락해야."""
    db = F.FarmDB()

    async def flow():
        A, B = 38201, 38202
        G = (await db.create(A, "river", T))["id"]
        H = (await db.create(B, "plain", T))["id"]
        _set_coins(G, 5_000)
        _set_coins(H, 700)
        assert (await db.get(G, T))["coins"] == 5_000 and not (await db.get(G, T))["v382"]
        assert await db.upgrade(A) == 5_000 and await db.upgrade(A) is None                 # 한 번만
        g = await db.get(G, T)
        assert g["v382"] and g["coins"] == 5_000 and g["farm_coins"] == 0 and g["cost"] == F.ENERGY_COST_382
        # 바로 인수 없음 (auto=False): 아무리 많이 내도 제안으로만
        r = await db.offer_farm(A, H, 4_000, T, G, auto=False)
        assert r["ok"] and not r["auto"]
        x = await db.answer_farm_offer(B, r["offer_id"], True, T + 10)
        assert x["ok"] and x["accepted"]
        mine = await db.farms_of(A, T)
        assert {f["id"] for f in mine} == {G, H}
        assert all(f["coins"] == 5_000 - 4_000 + 700 for f in mine)                          # 인수한 농장 코인도 지갑 하나로
        assert all(f["farm_coins"] == 0 for f in mine)
        # 판 사람(B, 지갑 없음)은 남은 농장이 없어서 맡아 둠 → 지갑으로 옮기면 같이 들어간다
        assert await db.upgrade(B) == 4_000
        K = (await db.create(B, "meadow", T))["id"]
        assert (await db.get(K, T))["coins"] == 4_000
        # 지갑끼리: 파는 사람도 지갑으로 받는다
        r = await db.offer_farm(B, H, 900, T, K, auto=False)
        x = await db.answer_farm_offer(A, r["offer_id"], True, T + 20)
        assert x["ok"] and (await db.get(G, T))["coins"] == 1_700 + 900 and (await db.get(K, T))["coins"] == 4_000 - 900
        # 코인을 쓰는 곳도 지갑 (상점 · 환전)
        assert (await db.buy(A, G, "potato", 10, T))["ok"] and (await db.get(G, T))["coins"] == 2_600 - 50
        assert (await db.exchange(A, G, 50, T))["ok"] and (await db.get(G, T))["coins"] == 2_500
        # 예전 방식(auto=True)은 그대로
        assert (await db.offer_farm(A, K, 10 ** 6, T, G))["reason"] == "coins"

    run(flow())


def test_energy_cost():
    db = F.FarmDB()

    async def flow():
        A = 38211
        G = (await db.create(A, "plain", T))["id"]
        await db.upgrade(A)
        r = await db.plant(A, G, "carrot", T)
        assert r["ok"] and r["count"] == F.PLOTS_BASE
        assert (await db.get(G, T))["energy"] == F.ENERGY_MAX - F.PLOTS_BASE * F.ENERGY_COST_382
        h = await db.harvest(A, G, T + 3600, random.Random(1))
        assert h["ok"] and h["count"] == F.PLOTS_BASE

    run(flow())


def test_crops_and_chests():
    for k in F.NEW_CROPS_382:
        assert k in F.CROPS and k in F.ITEMS and F.shop_price(k)
    for tier in F.CHESTS:
        old = sum(r["worth"] for _ in range(300) for r in F.roll_chest(tier, False, random.Random(3)))
        new = sum(r["worth"] for _ in range(300) for r in F.roll_chest(tier, False, random.Random(3), True))
        assert new > old * 3 and len(F.roll_chest(tier, False, random.Random(1), True)) == F.CHEST_REWARDS_382[tier]
        assert all(k in F.ITEMS for k in F.CHEST_ITEMS_382[tier][1])
    assert F.chest_p(1, True) > F.chest_p(1)


def test_shop_crops_by_guild():
    import cogs.farm as C
    test = [f.name for f in C.shop_embeds(release.TEST_GUILD)[1].fields]
    other = [f.name for f in C.shop_embeds(1)[1].fields]
    assert "망고" in test and "망고" not in other
    assert C.shop_ver(release.TEST_GUILD) != C.shop_ver(1) == C.SHOP_VER


def test_elite_no_auto():
    cdb.DB_PATH = pmdb.DB_PATH = edb.DB_PATH
    db, clubs = edb.EconomyDB(), cdb.ClubDB()
    pmdb.PlayerMarketDB()

    async def flow():
        now = int(time.time())
        A, B = 38221, 38222
        for u in (A, B):
            await clubs.create_club(u, f"FC{u}", now)
            await db.add_balance(u, 10 ** 12)
        assert (await clubs.buy_elite(A, "ruhr", now, v381=True))["ok"]
        r = await clubs.offer_elite(B, "ruhr", 10 ** 11, now, auto=False)
        assert r["ok"] and not r["auto"]

    run(flow())


def test_league_mid():
    from cogs.economy import Economy
    rng = random.Random(5)
    early = [Economy.league_mid(1, 0, True, rng) for _ in range(300)]
    assert max(early) >= 15                                                   # 우승 팀도 개막엔 아래쪽에 있을 수 있다
    assert all(abs(Economy.league_mid(10, 2, True, rng) - 10) <= 8 for _ in range(100))
    assert all(abs(Economy.league_mid(10, 0, False, rng) - 10) <= 5 for _ in range(100))


def test_thread_reopen():
    import discord
    import cogs.farm as C
    db = F.FarmDB()

    async def flow():
        A = 38231
        G = (await db.create(A, "plain", T))["id"]
        await db.set_thread(G, release.TEST_GUILD, 38255)
        parent = MagicMock(spec=discord.TextChannel)
        th = MagicMock(id=38256, send=AsyncMock())
        parent.create_thread = AsyncMock(return_value=th)
        guild = MagicMock(id=release.TEST_GUILD, get_channel=MagicMock(return_value=parent),
                          get_member=MagicMock(return_value=MagicMock(display_name="가", mention="<@1>")))
        bot = MagicMock(get_guild=MagicMock(return_value=guild))
        cog = C.Farm.__new__(C.Farm)
        cog.bot, cog.db = bot, db
        await cog.on_raw_thread_delete(SimpleNamespace(guild_id=release.TEST_GUILD, parent_id=10, thread_id=38255))
        assert await db.thread_farm(38256) == G and await db.thread_farm(38255) is None
        assert th.send.await_count == 1
        await cog.on_raw_thread_delete(SimpleNamespace(guild_id=1, parent_id=10, thread_id=38256))   # 공개 전 다른 서버는 그대로
        assert parent.create_thread.await_count == 1

    run(flow())


def test_undot():
    from discord.http import Route
    from services import undot as U
    assert U.undot("A · B · C") == "A, B, C" and U.undot("· 첫 줄\n끝 ·") == "첫 줄\n끝" and U.undot("없음") == "없음"
    p = {"content": "a · b", "embeds": [{"title": "x · y", "fields": [{"name": "n · m", "value": "v · w", "inline": True}],
                                         "footer": {"text": "f · g"}}],
         "components": [{"type": 1, "components": [{"type": 3, "custom_id": "farm:a·b", "placeholder": "p · q",
                                                     "options": [{"label": "l · k", "value": "key·1", "description": "d · e"}]}]}]}
    U.clean(p)
    s = str(p)
    assert "·" in s and s.count("·") == 2                                    # custom_id · 메뉴 value 만 남는다
    assert p["embeds"][0]["fields"][0]["value"] == "v, w"
    test = Route("POST", "/channels/{channel_id}/messages", channel_id=1)
    other = Route("POST", "/channels/{channel_id}/messages", channel_id=2)
    chans = {1: SimpleNamespace(guild=SimpleNamespace(id=release.TEST_GUILD)), 2: SimpleNamespace(guild=SimpleNamespace(id=5))}
    assert U.on_for(test, chans.get) and not U.on_for(other, chans.get)
    U.remember("tok", release.TEST_GUILD)
    assert U.on_for(Route("POST", "/webhooks/{webhook_id}/{webhook_token}", webhook_id=1, webhook_token="tok"), chans.get)
    assert not U.on_for(Route("POST", "/users/@me/channels"), chans.get)    # 서버를 모르면 공개 뒤부터
    sync = Route("PUT", "/applications/{application_id}/guilds/{guild_id}/commands", application_id=1, guild_id=release.TEST_GUILD)
    assert U.on_for(sync, chans.get)
    U.install(chans.get)
    U.install(chans.get)                                                     # 두 번 불러도 한 번만
    from discord import http as dhttp
    assert getattr(dhttp.HTTPClient.request, "_undot", False)


def test_lobby_rules():
    from services import lobby as L
    import calendar
    c = L.close_after(calendar.timegm((2026, 11, 16, 5, 0, 0)))
    assert time.gmtime(c + 9 * 3600)[:5] == (2026, 11, 22, 21, 0) and time.gmtime(c + 9 * 3600).tm_wday == 6
    cands = L.make_candidates(random.Random(1))
    assert len(cands) == L.CANDIDATES_N and all(len({k for k, _ in x["pledges"]}) == L.PLEDGES_N for x in cands)
    assert abs(sum(L.support(cands, [0, 0, 0, 0])) - 100) < 1e-6
    sup = L.support(cands, [10 ** 9, 0, 0, 0])
    assert sup[0] > L.support(cands, [0, 0, 0, 0])[0]                       # 로비가 몰리면 지지율이 오른다
    bets = [(1, 0, 100), (2, 0, 300), (3, 1, 400)]
    assert L.payouts(bets, 0, set()) == {1: 200, 2: 600}                    # 낙선 400원을 100:300 으로
    assert L.payouts(bets, 0, {2}) == {1: 800}                              # 적발된 사람 몫도 나머지에게
    assert L.payouts(bets, 2, set()) == {}
    # 돈만으로 정해지지 않는다: 로비가 크게 몰려도 다른 후보가 당선될 때가 있다
    wins = [L.pick_winner(cands, [10 ** 6, 0, 0, 0], random.Random(s)) for s in range(300)]
    assert 0 < wins.count(0) < 300


def test_lobby_db_and_effects():
    from services import lobby as L
    db = L.LobbyDB()
    edb_db = edb.EconomyDB()

    async def flow():
        now = calendar_ts = 1_800_000_000
        A, B = 38241, 38242
        await edb_db.add_balance(A, 1_000_000)
        await edb_db.add_balance(B, 1_000_000)
        e = await db.state(A, now)
        assert e["cands"] and e["news"]
        assert (await db.lobby(A, 0, 100, now))["reason"] == "amount"
        assert (await db.lobby(A, 0, 10 ** 9, now))["reason"] == "balance"
        r = await db.lobby(A, 0, 200_000, now)
        assert r["ok"] and r["balance"] == 800_000
        await db.lobby(B, 1, 300_000, now)
        assert await db.news_tick(now) is None                               # 아직 뉴스 시각 전
        n = await db.news_tick(now + L.NEWS_GAP[1] + 1, random.Random(2))
        assert n and n["text"] and n["delta"]
        assert await db.settle(now) is None                                  # 마감 전
        r = await db.settle(e["closes_ts"] + 1, random.Random(4))
        assert r and r["pool"] == 500_000 and r["next"] == e["id"] + 1
        w = r["winner"]
        assert await db.settle(e["closes_ts"] + 2) is None                  # 두 번 정산되지 않는다
        assert len(r["history"]) >= 3 and abs(sum(r["shares"]) - 100) < 1e-6      # 시작 · 로비 · 뉴스 · 마감 지지율 기록
        assert max(range(4), key=lambda i: r["shares"][i]) == w
        assert len((await db.state(A, e["closes_ts"] + 3))["history"]) == 1    # 새 선거는 첫 점 하나부터
        bal_a, bal_b = await edb_db.get_balance(A), await edb_db.get_balance(B)
        if w == 0 and A not in r["caught"]:
            assert bal_a == 800_000 + 500_000
        if w == 1 and B not in r["caught"]:
            assert bal_b == 700_000 + 500_000
        assert (await db.last_winner())["name"] == r["cand"]["name"]
        _ = calendar_ts
        # 공약 효과: 공개 전엔 테스트 서버 사람만
        L.ACTIVE.clear()
        L.ACTIVE.update({"attend": 2.0})
        L.TESTERS.clear()
        L.TESTERS.add(A)
        assert L.mult("attend", A) == 2.0 and L.mult("attend", B) == 1.0 and L.mult("fish_sell", A) == 1.0
        ok, *_rest, bonus = await edb_db.claim_daily(A, 30_000, now)
        assert ok and bonus >= 30_000
        L.ACTIVE.clear()

    run(flow())


def test_lobby_count_and_chart():
    from services import lobby as L
    from cogs import lobby as C
    rng = random.Random(5)
    cands = L.make_candidates(rng)
    final = L.final_shares(cands, [0, 10 ** 6, 0, 0], rng)
    frames = L.count_frames(final, rng)
    assert [p for p, _ in frames][-1] == 100 and frames[-1][1] == final
    assert all(abs(sum(s) - 100) < 1e-6 for _, s in frames)
    em = C.count_embed(1, cands, 55, frames[3][1], 1_000_000)
    assert "개표율" in em.description and "·" not in em.description
    o = 1_800_000_000
    png = C.support_chart(cands, [(o, [25.0] * 4), (o + 7200, [30.0, 20.0, 25.0, 25.0])], o, L.close_after(o), o + 9000)
    assert png[:4] == b"\x89PNG"
    assert C.support_chart(cands, [], o, L.close_after(o), o)[:4] == b"\x89PNG"   # 기록이 없어도 그려진다


def test_lobby_screen():
    import cogs.lobby as CL
    from services import lobby as L
    cands = L.make_candidates(random.Random(3))
    e = {"id": 1, "closes_ts": 1_800_000_000, "cands": cands, "support": L.support(cands, [0] * 4), "pots": [0] * 4,
         "mine": [10_000, 0, 0, 0], "news": [{"ts": 1_800_000_000, "text": "뉴스", "delta": 3}],
         "last": {"id": 0, "winner": 1, "cands": cands}}
    em = CL.lobby_embed(MagicMock(display_name="가", display_avatar=MagicMock(url="")), e)
    text = em.description + "".join(f.name + f.value for f in em.fields)
    assert "·" not in text and "내 로비" in text and "지금 적용 중인 공약" in text
    assert CL.kst(1_800_000_000) == "1월 15일 (금) 17:00"
    assert "로비" in release.hidden_commands(1) and "로비" not in release.hidden_commands(release.TEST_GUILD)
