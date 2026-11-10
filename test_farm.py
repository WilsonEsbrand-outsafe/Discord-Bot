"""3.8.1 🌾 /농사: 땅 고르기 · 심기 · 수확 · 동물 · 합치기 · 확장 · 낫 강화 · 상자 · 환전 (테스트 서버 먼저)."""
import asyncio
import random
import sqlite3
import tempfile
from pathlib import Path

import services.economy_db as edb

edb.DB_PATH = Path(tempfile.mkdtemp()) / "t.sqlite3"

import services.farm as F  # noqa: E402

T = 1_800_000_000


class Fixed:
    """정해진 값만 내는 rng (random() · randint · choice · choices)."""

    def __init__(self, x: float):
        self.x = x

    def random(self):
        return self.x

    def randint(self, a, b):
        return a

    def choice(self, seq):
        return seq[0]

    def choices(self, seq, weights=None):
        return [seq[0]]


def run(coro):
    return asyncio.run(coro)


def test_tables():
    assert all(sum(v[3].values()) == 12 for v in F.LANDS.values())          # 땅마다 자원 합이 같다
    assert F.time_mult(5) < F.time_mult(3) == 1 < F.time_mult(1)
    assert F.CROPS["carrot"][2] == 0                                         # 무료 씨앗이 있어야 코인 0 에서도 안 막힌다
    for k in F.ITEMS:                                                        # 합치면 3개 값보다 비싸다
        assert all(F.value(k, t + 1) > 3 * F.value(k, t) for t in range(1, F.TIER_MAX))
    assert [F.expand_cost(p) for p in (6, 7, 8)] == [150, 400, 750]
    assert F.extra_p(10) > F.extra_p(0) and F.high_p(10) > F.high_p(0) and F.chest_p(5) > F.chest_p(1)
    for tier in F.CHESTS:
        assert len(F.roll_chest(tier, False, random.Random(1))) == F.CHEST_REWARDS[tier]
    rng = random.Random(7)                                                   # 행운 열기는 평균 값이 더 크다
    plain = sum(r["worth"] for _ in range(400) for r in F.roll_chest("silver", False, rng))
    lucky = sum(r["worth"] for _ in range(400) for r in F.roll_chest("silver", True, rng))
    assert lucky > plain * 1.15
    for tier, (t, pool) in F.CHEST_ITEMS.items():
        assert 1 <= t <= F.TIER_MAX and all(k in F.ITEMS for k in pool)


def test_start_and_free_play():
    """돈 · 코인이 0 이어도: 무료 당근 → 수확 → 판매 → 코인."""
    db = F.FarmDB()

    async def flow():
        U = 9001
        assert await db.get(U, T) is None
        assert (await db.create(U, "moon", T))["reason"] == "land"
        assert (await db.create(U, "plain", T))["ok"] and (await db.create(U, "river", T))["reason"] == "exists"
        f = await db.get(U, T)
        assert (f["coins"], f["energy"], f["plots"], f["balance"], f["chests"]["wood"]) == (0, 100, 6, 0, 1)
        assert (await db.plant(U, "corn", T))["reason"] == "coins"
        r = await db.plant(U, "carrot", T)
        assert r["ok"] and r["count"] == 6 and r["cost"] == 0
        assert r["ready_ts"] == T + int(300 * F.time_mult(5))                 # 평야: 비옥도 5 → 빨리 자란다
        assert (await db.plant(U, "carrot", T))["reason"] == "full"
        assert (await db.harvest(U, T + 10, Fixed(0.99)))["reason"] == "nothing"
        h = await db.harvest(U, r["ready_ts"], Fixed(0.99))
        assert h["ok"] and h["got"] == {("carrot", 1): 6} and not h["chests"] and h["extra"] == h["high"] == 0
        assert h["daily"] and (await db.get(U, r["ready_ts"]))["chests"]["wood"] == 2      # 하루 첫 거두기 선물
        await db.plant(U, "carrot", r["ready_ts"])
        assert not (await db.harvest(U, r["ready_ts"] + 600, Fixed(0.99)))["daily"]       # 같은 날엔 한 번만
        assert (await db.harvest(U, r["ready_ts"] + 600, Fixed(0.99)))["reason"] == "nothing"
        s = await db.sell(U, T)
        assert s["ok"] and s["coins"] == 12 * 4 and (await db.sell(U, T))["reason"] == "empty"
        assert (await db.get(U, T))["balance"] == 0                           # 축구 봇 돈은 건드리지 않는다
    run(flow())


def test_plant_n_energy_and_luck():
    db = F.FarmDB()

    async def flow():
        U = 9002
        await db.create(U, "mountain", T)
        await db.set_plant_n(U, 2)
        assert (await db.plant(U, "carrot", T))["count"] == 2
        await db.set_plant_n(U, 0)
        con = sqlite3.connect(edb.DB_PATH)
        con.execute("UPDATE farms SET energy=3, energy_ts=?, sickle=10 WHERE user_id=?", (T, U))
        con.commit(); con.close()
        assert (await db.plant(U, "carrot", T))["count"] == 3                 # 에너지가 되는 만큼만
        assert (await db.plant(U, "carrot", T))["reason"] == "energy"
        late = T + 3600                                                       # 한 시간 뒤: 에너지가 다시 찼다
        f = await db.get(U, late)
        assert f["energy"] == int(3600 / (120 * F.time_mult(3))) and f["energy_next"] > late
        h = await db.harvest(U, late, Fixed(0.0))                             # 운이 전부 터지면: ★2 · 2개씩 · 상자
        assert h["got"] == {("carrot", 2): 10} and h["extra"] == h["high"] == 5 and h["chests"] == ["wood"] * 5
        assert h["daily"] and (await db.get(U, late))["chests"]["wood"] == 1 + 5 + 1
    run(flow())


def test_animals_merge_expand():
    db = F.FarmDB()

    async def flow():
        U = 9003
        await db.create(U, "meadow", T)
        con = sqlite3.connect(edb.DB_PATH)
        con.execute("UPDATE farms SET coins=100000 WHERE user_id=?", (U,))
        con.commit(); con.close()
        a = await db.plant(U, "chicken", T)
        assert a["ok"] and a["animal"] and a["count"] == 1 and a["cost"] == 300
        assert a["ready_ts"] == T + int(3600 * F.time_mult(5))                # 목초지: 동물이 빨리 만든다
        assert (await db.plant(U, "cow", T))["ok"]
        assert (await db.plant(U, "sheep", T))["reason"] == "cap"             # 땅 3칸당 1마리 (6칸 → 2마리)
        h = await db.harvest(U, a["ready_ts"], Fixed(0.99))
        assert h["got"] == {("egg", 1): 1}
        f = await db.get(U, a["ready_ts"])
        assert f["fields"][0]["kind"] == "animal" and not f["fields"][0]["ready"]   # 동물은 남아서 다시 만든다
        # 합치기: 3개 → 한 등급 위 1개
        con = sqlite3.connect(edb.DB_PATH)
        con.execute("INSERT OR REPLACE INTO farm_items VALUES(?,?,?,?)", (U, "corn", 1, 7))
        con.commit(); con.close()
        m = await db.merge(U, "corn", 1, T)
        assert m["ok"] and m["made"] == 2 and m["tier"] == 2
        items = {(k, t): q for k, t, q in (await db.get(U, T))["items"]}
        assert items[("corn", 1)] == 1 and items[("corn", 2)] == 2
        assert (await db.merge(U, "corn", 2, T))["reason"] == "few" and (await db.merge(U, "corn", 5, T))["reason"] == "max"
        s = await db.sell(U, T, [("corn", 2)])
        assert s["coins"] == 2 * F.value("corn", 2)
        # 확장: 코인 → 한 칸 + 은 상자 · 동물 한도도 같이
        before = (await db.get(U, T))["coins"]
        for _ in range(3):
            e = await db.expand(U, T)
        f = await db.get(U, T)
        assert e["plots"] == 9 and f["animal_cap"] == 3 and f["chests"]["silver"] == 3
        assert before - f["coins"] == 150 + 400 + 750
        x = await db.sell_animal(U, 0, T)
        assert x["ok"] and x["coins"] == 150 and (await db.sell_animal(U, 0, T))["reason"] == "slot"
    run(flow())


def test_sickle_chest_exchange():
    db = F.FarmDB()

    async def flow():
        U = 9004
        await db.create(U, "river", T)
        assert (await db.enhance(U, T))["reason"] == "coins"
        assert (await db.enhance(U, T, use_whet=True))["reason"] == "whet"
        con = sqlite3.connect(edb.DB_PATH)
        con.execute("UPDATE farms SET coins=5000, whet=2, c_gold=2 WHERE user_id=?", (U,))
        con.commit(); con.close()
        assert (await db.enhance(U, T, charm=True))["reason"] == "money"      # 행운 부적은 축구 봇 돈
        r = await db.enhance(U, T, rng=Fixed(0.0))
        assert r["ok"] and r["success"] and r["level"] == 1
        r = await db.enhance(U, T, rng=Fixed(0.999))                          # 실패해도 단계는 그대로
        assert r["ok"] and not r["success"] and r["level"] == 1
        f = await db.get(U, T)
        assert f["sickle"] == 1 and f["coins"] == 5000 - 150 - 600
        await db.add_balance(U, 10_000_000)
        r = await db.enhance(U, T, use_whet=True, charm=True, rng=Fixed(0.95))   # 90% + 10%p → 성공
        assert r["success"] and r["fee"] == F.CHARM_FEE * 2
        f = await db.get(U, T)
        assert f["whet"] == 0 and f["balance"] == 10_000_000 - F.CHARM_FEE * 2
        # 상자: 그냥 열기 · 행운 열기(돈)
        c = await db.open_chest(U, "gold", T, rng=random.Random(3))
        assert c["ok"] and len(c["rewards"]) == 3 and c["fee"] == 0
        c = await db.open_chest(U, "gold", T, lucky=True, rng=random.Random(4))
        assert c["ok"] and c["fee"] == F.LUCKY_FEE["gold"]
        assert (await db.open_chest(U, "gold", T))["reason"] == "empty"
        f = await db.get(U, T)
        assert f["balance"] == 10_000_000 - F.CHARM_FEE * 2 - F.LUCKY_FEE["gold"]
        # 환전: 코인 → 돈
        assert (await db.exchange(U, f["coins"] + 1, T))["reason"] == "coins"
        assert (await db.exchange(U, 0, T))["reason"] == "amount"
        x = await db.exchange(U, 100, T)
        assert x["ok"] and x["won"] == 100 * F.RATE and x["balance"] == f["balance"] + 100 * F.RATE
    run(flow())


def test_threads_and_old_table():
    db = F.FarmDB()

    async def flow():
        await db.set_thread(1, 10, 555)
        assert await db.get_thread(1, 10) == 555 and await db.get_thread(1, 11) is None
        assert await db.thread_owner(555) == 1 and await db.thread_owner(556) is None
        await db.set_thread(1, 10, 777)                                       # 스레드를 새로 열면 바뀐다
        assert await db.thread_owner(555) is None and await db.thread_owner(777) == 1
    run(flow())
    # PR #118 의 옛 밭 표가 있으면: 심어 둔 씨앗 값을 돌려주고 지운다
    con = sqlite3.connect(edb.DB_PATH)
    con.execute("CREATE TABLE farm_plots (user_id INTEGER, slot INTEGER, crop TEXT, planted_ts INTEGER, PRIMARY KEY(user_id, slot))")
    con.execute("INSERT INTO farm_plots VALUES(42, 0, 'corn', 1), (42, 1, 'carrot', 1)")
    con.commit(); con.close()
    db2 = F.FarmDB()
    assert run(db2.get_balance(42)) == 110_000
    con = sqlite3.connect(edb.DB_PATH)
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='farm_plots'").fetchone()
    con.close()


def test_ui_smoke():
    """농장 화면: 땅 고르기 → 스레드 → 심기 · 거두기 · 작은 화면들이 오류 없이 그려진다 (디스코드 없이 가짜 상호작용으로)."""
    from unittest.mock import AsyncMock, MagicMock

    import discord

    import cogs.farm as C

    class Resp:
        def __init__(self):
            self.done, self.calls = False, []

        def is_done(self):
            return self.done

        async def _do(self, name, **kw):
            self.done = True
            self.calls.append((name, kw))

        async def send_message(self, *a, **kw):
            await self._do("send", content=a[0] if a else None, **kw)

        async def edit_message(self, **kw):
            await self._do("edit", **kw)

        async def defer(self, **kw):
            await self._do("defer", **kw)

        async def send_modal(self, modal):
            await self._do("modal", modal=modal)

    def inter(user, channel, guild, values=None, message=None):
        i = MagicMock()
        i.user, i.channel, i.guild, i.guild_id, i.channel_id = user, channel, guild, guild.id, channel.id
        i.data = {"values": values or []}
        i.response, i.followup, i.message = Resp(), MagicMock(send=AsyncMock()), message
        return i

    async def flow():
        bot = MagicMock()
        cog = C.Farm(bot)
        assert bot.add_view.called and all(x.custom_id.startswith("farm:") for x in cog.panel.children)   # 재시작 뒤에도 눌린다
        guild = MagicMock(id=release_guild())
        user = MagicMock(id=9100, display_name="규완", mention="<@9100>", guild=guild)
        thread = MagicMock(spec=discord.Thread, id=4242, archived=False, mention="<#4242>")
        thread.send, thread.edit = AsyncMock(), AsyncMock()
        channel = MagicMock(spec=discord.TextChannel, id=77)
        channel.create_thread = AsyncMock(return_value=thread)
        guild.get_thread = MagicMock(return_value=None)
        guild.fetch_channel = AsyncMock(return_value=thread)

        # 처음: 땅 고르기 화면 → 고르고 → 시작 → 스레드가 열리고 농장 화면이 올라간다
        i = inter(user, channel, guild)
        await C.Farm.farm.callback(cog, i)
        pick = i.response.calls[0][1]["view"]
        assert isinstance(pick, C.LandPick) and i.response.calls[0][1]["ephemeral"]
        i = inter(user, channel, guild, ["mountain"])
        await pick._picked(i)
        i = inter(user, channel, guild)
        await pick._start(i)
        channel.create_thread.assert_awaited_once()
        assert channel.create_thread.await_args.kwargs["type"] == discord.ChannelType.public_thread
        assert await cog.db.thread_owner(4242) == 9100
        sent = thread.send.await_args.kwargs
        assert isinstance(sent["view"], C.FarmPanel) and "바위 산지" in sent["embed"].description
        assert "<#4242>" in i.followup.send.await_args.args[0]

        # 다른 채널에서 다시 /농사 → 있던 스레드로 안내 · 스레드 안에서 /농사 → 화면을 새로 올린다
        i = inter(user, channel, guild)
        await C.Farm.farm.callback(cog, i)
        assert channel.create_thread.await_count == 1 and "<#4242>" in i.followup.send.await_args.args[0]
        tch = MagicMock(spec=discord.Thread, id=4242)
        i = inter(user, tch, guild)
        await C.Farm.farm.callback(cog, i)
        assert i.response.calls[0][0] == "send" and isinstance(i.response.calls[0][1]["view"], C.FarmPanel)

        # 주인만 누를 수 있다
        panel = cog.panel
        other = MagicMock(id=1, display_name="남", guild=guild)
        i = inter(other, tch, guild)
        assert not await panel.interaction_check(i) and i.response.calls[0][1]["ephemeral"]
        assert await panel.interaction_check(inter(user, tch, guild))

        # 심기 → 거두기 → 넓히기(코인 부족) → 칸 수 바꾸기
        msg = MagicMock(edit=AsyncMock())
        i = inter(user, tch, guild, ["carrot"], msg)
        await panel._plant(i)
        assert "6칸" in i.response.calls[0][1]["embed"].description
        import sqlite3
        con = sqlite3.connect(edb.DB_PATH)
        con.execute("UPDATE farm_fields SET ready_ts=0 WHERE user_id=9100")
        con.execute("UPDATE farms SET coins=50000, whet=3, c_silver=2 WHERE user_id=9100")
        con.execute("INSERT OR REPLACE INTO farm_items VALUES(9100, 'egg', 1, 9)")
        con.commit(); con.close()
        i = inter(user, tch, guild, message=msg)
        await panel._harvest(i)
        assert "거뒀어요" in i.response.calls[0][1]["embed"].description
        for cb, vals in ((panel._expand, None), (panel._qty, ["2"]), (panel._plant, ["chicken"]), (panel._refresh, None),
                         (panel._help, None)):
            i = inter(user, tch, guild, vals, msg)
            await cb(i)
            assert i.response.calls
        # 작은 화면들
        for opener, cls in ((panel._merge, C.MergeView), (panel._store, C.StoreView), (panel._chest, C.ChestView),
                            (panel._sickle, C.SickleView)):
            i = inter(user, tch, guild, message=msg)
            await opener(i)
            view = i.response.calls[0][1]["view"]
            assert isinstance(view, cls) and i.response.calls[0][1]["ephemeral"]
            if cls is C.MergeView:
                i = inter(user, tch, guild, ["egg:1"])
                await view._pick(i)
                assert "★2" in i.response.calls[0][1]["embed"].description
            elif cls is C.StoreView:
                i = inter(user, tch, guild, ["egg:2"])
                await view._sell(i)
                assert "코인" in i.response.calls[0][1]["embed"].description
                i = inter(user, tch, guild)
                await view._sell_all(i)
                i = inter(user, tch, guild, ["0"])
                await view._release(i)
            elif cls is C.ChestView:
                i = inter(user, tch, guild)
                await view._toggle(i)                       # 행운 열기 켬 → 돈이 없어서 못 연다
                i = inter(user, tch, guild)
                await view._open("silver")(i)
                assert "모자라요" in i.response.calls[0][1]["embed"].description
                i = inter(user, tch, guild)
                await view._toggle(i)
                i = inter(user, tch, guild)
                await view._open("silver")(i)
                assert "열었어요" in i.response.calls[0][1]["embed"].description
            else:
                i = inter(user, tch, guild)
                await view._go(True)(i)
                i = inter(user, tch, guild)
                await view._toggle(i)
                i = inter(user, tch, guild)
                await view._go(False)(i)
            assert msg.edit.await_count                     # 농장 화면도 같이 고쳐진다
        # 환전
        i = inter(user, tch, guild, message=msg)
        await panel._exchange(i)
        modal = i.response.calls[0][1]["modal"]
        modal.amount._value = "전부"
        i = inter(user, tch, guild, message=msg)
        await modal.on_submit(i)
        assert "원" in i.response.calls[0][1]["embed"].description
        f = await cog.db.get(9100, T)
        assert f["coins"] == 0 and f["balance"] > 0
    run(flow())


def release_guild():
    import release
    return release.TEST_GUILD
