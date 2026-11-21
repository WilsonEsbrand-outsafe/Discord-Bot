"""3.9.1 (코드 이름 384): 출석 · 스카우트 · 훈련 · 직관 100배, 낚시 10배, 수능 버닝 낚시 효과 + 버닝 미션, 대선 11/22 21:00 개표 · 공개 때 새로 시작."""
import asyncio
import calendar
import random
import sqlite3
import tempfile
from pathlib import Path

import services.economy_db as edb

edb.DB_PATH = Path(tempfile.mkdtemp()) / "t391.sqlite3"

import release  # noqa: E402
release.RELEASE_384_TS = release.RELEASE_LOBBY_TS = None   # 공개 전(테스트 서버만) 기준으로 본다 — 공개 뒤 동작은 테스트 안에서 시각을 넣어 본다
from services import burning as B  # noqa: E402
from services import fishing as FI  # noqa: E402
from services import lobby as L  # noqa: E402

MID = calendar.timegm((2026, 11, 24, 3, 0, 0))   # 버닝 중


def run(coro):
    return asyncio.run(coro)


def _con():
    return sqlite3.connect(edb.DB_PATH)


def _bal(uid):
    con = _con()
    r = con.execute("SELECT balance FROM wallets WHERE user_id=?", (uid,)).fetchone()
    con.close()
    return r[0] if r else 0


def _in(guild):
    return release.GUILD.set(guild)


def test_pay_switch_and_hidden_command():
    tok = _in(release.TEST_GUILD)
    try:
        assert edb.pay_384("attend") == 100 and edb.pay_384("fish") == 10
    finally:
        release.GUILD.reset(tok)
    tok = _in(1)
    try:
        assert edb.pay_384("train") == 1
    finally:
        release.GUILD.reset(tok)
    assert "버닝" in release.hidden_commands(1) and "버닝" not in release.hidden_commands(release.TEST_GUILD)


def test_attend_100x_with_bonus():
    db = edb.EconomyDB()
    U = 39101
    tok = _in(release.TEST_GUILD)
    try:
        con = _con()
        con.execute("INSERT OR IGNORE INTO daily_claims(user_id, last_claim_ts, total_days) VALUES(?, 0, 6)", (U,))
        con.commit(); con.close()
        reward = 30_000 * edb.pay_384("attend")
        t = calendar.timegm((2026, 11, 20, 0, 0, 0))   # 버닝 전
        ok, bal, _, total, bonus = run(db.claim_daily(U, reward, t))
        assert ok and total == 7 and bonus == edb.ATTEND_BONUS[7] * 100 and bal == 3_000_000 + 5_000_000
    finally:
        release.GUILD.reset(tok)


def test_grind_rolls_scaled():
    from cogs.economy import Economy
    eco = Economy.__new__(Economy)
    random.seed(3)
    edb.EconomyDB()
    con = _con()
    tok = _in(release.TEST_GUILD)
    try:
        for _ in range(30):
            d, _, info = eco._train_roll(1, None, True)
            assert abs(d) >= 1000 * 100 and info["base"] % 100 == 0
            d, _, info = eco._watch_roll(1, con, 1)
            assert info.get("item") or abs(d) >= 200 * 100
    finally:
        release.GUILD.reset(tok)
        con.close()
    tok = _in(1)
    try:
        d, _, _ = eco._train_roll(1, None, True)
        assert abs(d) <= 30_000                                                    # 다른 서버는 그대로
    finally:
        release.GUILD.reset(tok)


def test_fish_price_10x():
    tok = _in(1)
    try:
        base = FI.fish_price("crucian", 25, 1.0, 1.0, 1.0)
    finally:
        release.GUILD.reset(tok)
    tok = _in(release.TEST_GUILD)
    try:
        assert FI.fish_price("crucian", 25, 1.0, 1.0, 1.0) == base * 10
    finally:
        release.GUILD.reset(tok)
    assert B.BUFFS["fish_sell"] == 1.5 and B.BUFFS["fish_xp"] == 2.0


def test_burning_missions():
    FI.FishingDB()   # fish_log 표
    db = edb.EconomyDB()
    U, V = 39111, 39112
    con = _con()
    B.bump(con, U, "attend", 1, B.START - 1)                                     # 기간 밖은 안 센다
    for _ in range(3):
        B.bump(con, U, "attend", 1, MID)
    B.bump(con, U, "harvest", 60, MID)
    for i in range(55):
        con.execute("INSERT INTO fish_log VALUES(?,?,?,?,?)", (U, "crucian", 20.0, MID + i, 0))
    con.execute("INSERT INTO fish_log VALUES(?,?,?,?,?)", (U, "crucian", 20.0, B.START - 5, 0))   # 기간 전
    con.commit(); con.close()
    B._SEEN.clear(); B.take_pending()
    for m in range(35):
        B.touch(U, MID + m * 60)
        B.touch(U, MID + m * 60 + 10)                                             # 같은 분은 한 번만
    pend = B.take_pending()
    assert pend == {U: 35}
    run(db._tx(lambda c: B.add_play(c, pend, MID)))
    rows = {m["key"]: m for m in run(db._tx(lambda c: B.status(c, U)))}
    assert rows["attend3"]["done"] and not rows["attend6"]["done"] and rows["attend3"]["value"] == 3
    assert rows["fish50"]["done"] and rows["fish200"]["value"] == 55
    assert rows["harvest50"]["done"] and rows["play30"]["done"] and not rows["play60"]["done"]

    L.TESTERS.clear()
    r = run(db._tx(lambda c: B.claim(c, U, MID)))
    assert r["claimed"] == []                                                     # 공개 전엔 테스트 서버 사람만
    L.TESTERS.add(U)
    try:
        r = run(db._tx(lambda c: B.claim(c, U, MID)))
        assert len(r["claimed"]) == 4 and r["items"] == {"scout_skip": 1, "auto_fish": 2, "watch_skip": 1}
        con = _con()
        inv = dict(con.execute("SELECT item, qty FROM inventory WHERE user_id=?", (U,)).fetchall())
        con.close()
        assert inv == {"scout_skip": 1, "auto_fish": 2, "watch_skip": 1}
        assert all(k in edb.ITEMS and k not in edb.ITEM_PRICES for m in B.MISSIONS for k in m[4])   # 상점에서 못 사는 것만
        assert run(db._tx(lambda c: B.claim(c, U, MID)))["claimed"] == []          # 한 번씩만
        assert run(db._tx(lambda c: B.claim(c, U, B.END + B.CLAIM_GRACE)))["claimed"] == []
    finally:
        L.TESTERS.clear()
    old = release.RELEASE_384_TS
    release.RELEASE_384_TS = MID - 10
    try:
        assert run(db._tx(lambda c: B.claim(c, V, MID)))["claimed"] == []          # 채운 게 없으면 없다
    finally:
        release.RELEASE_384_TS = old


def test_burning_ui_smoke():
    from cogs.burning import burning_embed

    class U:
        id, display_name, mention, guild = 1, "테스터", "<@1>", None
        class display_avatar:
            url = "https://example.com/a.png"
    rows = [{"key": k, "name": n, "what": w, "goal": g, "items": m, "value": 0, "done": False, "claimed": False}
            for k, n, w, g, m in B.MISSIONS]
    e = burning_embed(U(), rows, MID, "")
    assert e.title == B.NAME and len(e.fields) == 2 and "변호사 선임권" in e.fields[1].value and "얼티밋팩 교환권" in e.fields[1].value
    assert len(e.fields[1].value) <= 1024


def test_election_restart_at_release():
    db = L.LobbyDB()
    pre = calendar.timegm((2026, 11, 21, 0, 0, 0))   # 공개 전 (테스트 서버)
    A, Bb = 39121, 39122
    con = _con()
    for u in (A, Bb):
        con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 50000000)", (u,))
    con.commit(); con.close()

    async def flow():
        s = await db.state(A, pre)
        assert s["no"] == L.FIRST_NO and s["closes_ts"] == L.FIRST_CLOSE
        assert (await db.vote(A, 0, 5, pre))["ok"]                                # 4표 = 400만원
        assert (await db.bet(Bb, 1, 2_000_000, pre))["ok"]
        assert _bal(A) == 46_000_000 and _bal(Bb) == 48_000_000
        rel = pre + 3600
        st = await db.restart(rel, random.Random(2))
        assert st["no"] == L.FIRST_NO and st["closes_ts"] == L.FIRST_CLOSE and st["refunds"] == {A: 4_000_000, Bb: 2_000_000}
        assert _bal(A) == 50_000_000 and _bal(Bb) == 50_000_000
        s = await db.state(A, rel)
        assert s["no"] == L.FIRST_NO and s["my_vote"] is None and sum(s["votes"]) == 0
        assert await db.restart(rel + 60) is None                                  # 한 번만
    run(flow())


def test_farm_cap_and_tool_price():
    import services.farm as F
    db = F.FarmDB()
    U, O = 39131, 39132
    t = MID
    L.TESTERS.clear()
    L.TESTERS.add(U)
    try:
        assert F.farm_cap(U, t) == 7 and F.farm_cap(O, t) is None                  # 공개 전엔 테스트 서버 사람만
        con = _con()
        for i in range(7):
            con.execute("INSERT INTO farms(owner, land, energy, energy_ts, plots, created_ts) VALUES(?,?,?,?,?,?)",
                        (U, "plain", 100, int(t), 6, int(t) + i))
        cur = con.execute("INSERT INTO farms(owner, land, energy, energy_ts, plots, created_ts) VALUES(?,?,?,?,?,?)",
                          (O, "plain", 100, int(t), 6, int(t)))
        other = cur.lastrowid
        mine = con.execute("SELECT id FROM farms WHERE owner=? ORDER BY id LIMIT 1", (U,)).fetchone()[0]
        con.commit(); con.close()
        r = run(db.create_more(U, "plain", mine, int(t)))
        assert not r["ok"] and r["reason"] == "max" and r["max"] == 7
        r = run(db.offer_farm(U, other, 10, int(t), mine, auto=False))
        assert not r["ok"] and r["reason"] == "max"
    finally:
        L.TESTERS.clear()
    tok = _in(release.TEST_GUILD)
    try:
        assert F.shop_price("drink") == F.TOOLS["drink"][1] * 5 and F.tool_price("fert", False) == F.TOOLS["fert"][1]
    finally:
        release.GUILD.reset(tok)


def test_gifts_once_and_enchant_slow():
    db = edb.EconomyDB()
    gifts = (("gift-t-a", 39141, 250_000_000), ("gift-t-b", 39142, 100_000_000))
    assert run(db.give_gifts_once(gifts, int(MID))) == [(39141, 250_000_000), (39142, 100_000_000)]
    assert run(db.give_gifts_once(gifts, int(MID) + 5)) == []                     # 한 번만
    con = _con()
    q = con.execute("SELECT qty FROM inventory WHERE user_id=39141 AND item='reward_cash'").fetchone()[0]
    con.close()
    assert q == 250_000_000 and "reward_cash" in edb.CASH_ITEMS
    r = run(db.use_gem_cash(39141, "reward_cash"))
    assert r["ok"] and r["amount"] == 250_000_000 and _bal(39141) == 250_000_000
    assert [g[1:] for g in edb.GIFTS_391] == [(1146659618790051960, 250_000_000), (725662872327159838, 100_000_000)]


def test_enchant_skips_burning():
    import services.farm as F
    db = F.FarmDB()
    A = 39151
    L.TESTERS.clear()
    L.TESTERS.add(A)                                                              # 버닝을 받는 사람
    try:
        async def flow():
            fid = (await db.create(A, "plain", int(MID)))["id"]
            con = _con()
            con.execute("UPDATE farms SET whet=9, energy=1000, plots=12 WHERE id=?", (fid,))
            con.execute("INSERT INTO farm_seeds(farm_id, key, qty) VALUES(?, 'potato', 56)", (fid,))
            con.commit(); con.close()
            assert (await db.make_ingot(A, fid, int(MID)))["ok"] and (await db.enchant(A, fid, "potato", int(MID)))["ok"]
            con = _con()
            con.execute("UPDATE farm_seeds SET qty=3 WHERE farm_id=? AND key='potato_e'", (fid,))
            con.execute("UPDATE farms SET plant_n=6 WHERE id=?", (fid,))
            con.commit(); con.close()
            p = await db.plant(A, fid, "potato", int(MID))
            assert p["ok"] and p["ench"] == 3 and p["count"] == 6
            con = _con()
            rows = dict(con.execute("SELECT ench, MAX(ready_ts) FROM farm_fields WHERE farm_id=? GROUP BY ench", (fid,)).fetchall())
            con.close()
            normal, ench = rows[0] - int(MID), rows[1] - int(MID)
            assert abs(ench - normal * B.BUFFS["farm_grow"] * F.ENCH_SLOW) <= 2      # 인챈트 칸은 버닝 '두 배 빨리'가 없고 10% 늦다
            con = _con()
            con.execute("DELETE FROM farm_items WHERE farm_id=?", (fid,))
            con.execute("INSERT INTO farm_items(farm_id, key, tier, qty) VALUES(?, 'potato', 1, 10)", (fid,))
            con.execute("INSERT INTO farm_items(farm_id, key, tier, qty) VALUES(?, 'potato_e', 1, 10)", (fid,))
            con.commit(); con.close()
            r = await db.sell(A, fid, int(MID))
            want = int(F.value("potato", 1) * 10 * B.BUFFS["farm_sell"] + F.value("potato_e", 1) * 10)
            assert r["ok"] and abs(r["coins"] - want) <= 1, (r, want)
        run(flow())
    finally:
        L.TESTERS.clear()


def test_shop_moves_to_home_channel():
    """11/21 테스트 서버 농사 상점은 '축구-봇' 채널로 — #패치노트에 있던 스레드는 옮기고 지운다."""
    from unittest.mock import AsyncMock, MagicMock
    import discord
    import cogs.farm as C

    def text_channel(cid, name):
        ch = MagicMock(spec=discord.TextChannel, id=cid)
        ch.name = name
        ch.is_news = MagicMock(return_value=False)
        ch.permissions_for = MagicMock(return_value=MagicMock(view_channel=True, create_public_threads=True,
                                                              send_messages_in_threads=True))
        return ch

    def thread(tid, parent):
        th = MagicMock(spec=discord.Thread, id=tid, parent_id=parent.id, archived=False, locked=False)
        th.parent = parent
        th.send = AsyncMock(return_value=MagicMock(id=tid + 1))
        th.delete, th.edit = AsyncMock(), AsyncMock()
        return th

    async def flow():
        bot = MagicMock()
        cog = C.Farm(bot)
        notes, home = text_channel(80, "패치노트"), text_channel(81, "축구-봇")
        old, new = thread(8100, notes), thread(8200, home)
        home.create_thread = AsyncMock(return_value=new)
        guild = MagicMock(id=release.TEST_GUILD, text_channels=[notes, home])
        guild.get_thread = MagicMock(return_value=old)
        await cog.db.set_shop_thread(guild.id, old.id, 1, C.shop_ver(guild.id))
        C.SHOPS[guild.id], C.SHOP_MSGS[guild.id] = old.id, (1, C.shop_ver(guild.id))
        assert cog._shop_home(guild) is home and cog._shop_home(MagicMock(id=1, text_channels=[home])) is None
        assert await cog.ensure_shop(guild) is new
        assert home.create_thread.await_args.kwargs["name"] == "농사 상점" and old.delete.await_count == 1
        assert C.SHOPS[guild.id] == new.id and (await cog.db.shop_threads())[guild.id][0] == new.id
        guild.get_thread = MagicMock(return_value=new)                                  # 다시 부르면 그대로
        assert await cog.ensure_shop(guild) is new and home.create_thread.await_count == 1
        loose = text_channel(82, "💬축구-봇-채팅")                                       # 이름에 들어 있으면
        assert cog._shop_home(MagicMock(id=release.TEST_GUILD, text_channels=[notes, loose])) is loose
    run(flow())


def test_live_old_message_reposts():
    """11/21 로그: 1시간 넘은 밭 화면을 10초마다 고치다가 디스코드 30046 오류 → 새 글로 올리고 다시 고친다."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock
    import discord
    import cogs.farm as C

    def old_limit():
        return discord.HTTPException(MagicMock(status=429, reason="Too Many Requests"),
                                     {"code": 30046, "message": "Maximum number of edits to messages older than 1 hour reached."})

    async def flow():
        cog = C.Farm.__new__(C.Farm)
        cog.bot, cog._live = MagicMock(), {}
        th = SimpleNamespace(id=7, guild=SimpleNamespace(id=release.TEST_GUILD))
        msg = SimpleNamespace(id=1, channel=th)
        cog.track(msg, 99, "")
        cog._live_one = AsyncMock(side_effect=old_limit())
        cog.repost = AsyncMock(return_value=True)
        await cog.live_loop.coro(cog)
        cog.repost.assert_awaited_once_with(th, 99, force=True)
        assert old_limit().code == C.OLD_EDIT_LIMIT
        cog._live_one = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500, reason="x"), "boom"))
        await cog.live_loop.coro(cog)                                                       # 다른 오류는 새로 올리지 않는다
        assert cog.repost.await_count == 1
    run(flow())
