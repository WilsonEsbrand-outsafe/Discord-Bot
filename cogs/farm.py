# cogs/farm.py — /농사 (3.8.1, 테스트 서버 먼저)
# /농사 → 땅 고르기 → 그 채널에 내 농장 스레드(공개 · 조작은 주인만)가 열리고, 농장 화면의 버튼 · 메뉴로 논다.
# 농장 화면의 버튼은 custom_id 가 고정(farm:…)이라 봇이 재시작해도 계속 눌린다. 규칙 · 수치는 services/farm.py.
import time
from collections import Counter
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import release
from services import farm as F
from services import ui

SECTION = "농사"
ROW = 6   # 땅 그림 한 줄 칸 수


def _dur(sec: float) -> str:
    m = max(1, round(sec / 60))
    h, m = divmod(m, 60)
    return (f"{h}시간" + (f" {m}분" if m else "")) if h else f"{m}분"


def _item(key: str, tier: int = 1) -> str:
    return F.ITEMS[key][1] + (f" ★{tier}" if tier > 1 else "")


def _res_line(res: dict) -> str:
    return " · ".join(f"{F.RES[k][1]} {'★' * res[k]}{'☆' * (5 - res[k])}" for k in F.RES)


def land_embed(user, pick: Optional[str] = None) -> discord.Embed:
    lines = []
    for k, (e, n, desc, res) in F.LANDS.items():
        lines.append(f"**{n}**{' (선택)' if k == pick else ''} — {desc}\n　{_res_line(res)}")
    guide = " · ".join(f"{n}: {d}" for _, n, d in F.RES.values())
    e = ui.card("농사를 시작할 땅을 골라 주세요", "\n\n".join(lines) + f"\n\n-# {guide}\n-# 땅은 한 번 고르면 바꿀 수 없어요.",
                ui.INFO, user, SECTION)
    return e


def panel_embed(user, f: dict, now: int, note: str = "") -> discord.Embed:
    le, ln, _, res = F.LANDS[f["land"]]
    head = f"**{ln}** · " + " · ".join(f"{F.RES[k][1]} {res[k]}" for k in F.RES)
    energy = f"에너지 **{f['energy']}**/{F.ENERGY_MAX}" + (f" (+1 <t:{f['energy_next']}:R>)" if f["energy_next"] else "")
    chests = sum(f["chests"].values())
    stat = (f"{energy} · 코인 **{f['coins']:,}** · 낫 **+{f['sickle']}**" + (f" · 숫돌 {f['whet']}" if f["whet"] else "")
            + (f" · 상자 **{chests}**" if chests else ""))
    cells, ready, growing, animals = [], Counter(), {}, {}
    for p in f["fields"]:
        if p is None:
            cells.append("🟫")
        elif p["kind"] == "crop":
            cells.append(F.CROPS[p["key"]][0] if p["ready"] else "🌱")
            if p["ready"]:
                ready[p["key"]] += 1
            else:
                n, ts = growing.get(p["key"], (0, p["ready_ts"]))
                growing[p["key"]] = (n + 1, min(ts, p["ready_ts"]))
        else:
            prod = F.ANIMALS[p["key"]][3]
            cells.append(F.PRODUCTS[prod][0] if p["ready"] else F.ANIMALS[p["key"]][0])
            if p["ready"]:
                ready[prod] += 1
            n, ts = animals.get(p["key"], (0, None))
            animals[p["key"]] = (n + 1, ts if p["ready"] else min(ts or p["ready_ts"], p["ready_ts"]))
    grid = "\n".join("".join(cells[i:i + ROW]) for i in range(0, len(cells), ROW))
    lines = []
    if ready:
        lines.append("**거둘 수 있어요:** " + " · ".join(f"{_item(k)} ×{n}" for k, n in ready.items()))
    if growing:
        lines.append("**자라는 중:** " + " · ".join(f"{_item(k)} ×{n} <t:{ts}:R>" for k, (n, ts) in growing.items()))
    if animals:
        lines.append("**동물** ({}/{}): ".format(f["animals"], f["animal_cap"]) + " · ".join(
            f"{F.ANIMALS[k][1]} ×{n}" + (f" <t:{ts}:R>" if ts else "") for k, (n, ts) in animals.items()))
    if f["items"]:
        worth = sum(F.value(k, t) * q for k, t, q in f["items"])
        shown = " · ".join(f"{_item(k, t)} ×{q}" for k, t, q in f["items"][:12])
        more = f" 외 {len(f['items']) - 12}가지" if len(f["items"]) > 12 else ""
        lines.append(f"**창고** (팔면 {worth:,} 코인): {shown}{more}")
    if not (ready or growing or animals or f["items"]):
        lines.append("아래 메뉴에서 작물을 골라 심어 보세요. 당근 씨앗은 무료예요.")
    e = ui.card(f"{user.display_name}의 농장", (f"{note}\n\n" if note else "") + f"{head}\n{stat}\n\n{grid}\n\n" + "\n".join(lines),
                ui.WIN, user, SECTION)
    e.set_footer(text="농장 주인만 누를 수 있어요 · 심기 · 거두기 · 합치기는 한 번에 에너지 1")
    return e


def help_embed(user) -> discord.Embed:
    return ui.card("농사 도움말", (
        "**심기** — 메뉴에서 작물을 고르면 빈 땅에 심어요. 시간이 지나면 다 자라요.\n"
        "**동물** — 입양하면 땅 한 칸에 살면서 달걀 · 양털 · 우유를 계속 만들어요 (땅 3칸당 1마리).\n"
        "**거두기** — 다 자란 작물과 동물이 만든 것을 창고로 옮겨요.\n"
        "**합치기** — 같은 물건 3개를 합치면 한 등급 위 1개가 돼요 (★5 까지). 등급이 높을수록 훨씬 비싸요.\n"
        "**창고** — 물건을 농장 코인으로 팔아요. 코인으로 씨앗 · 동물 · 땅 · 낫 강화를 해요.\n"
        f"**에너지** — 심기 · 거두기 · 합치기 한 번에 1. 시간이 지나면 다시 차요 (최대 {F.ENERGY_MAX}).\n"
        "**땅 넓히기** — 코인으로 한 칸씩 넓혀요. 넓힐 때마다 은 상자를 하나 발견해요.\n"
        "**낫 강화** — 강화할수록 거둘 때 하나 더 나올 확률과 ★2 로 나올 확률이 올라요.\n"
        "**상자** — 거두다 가끔 나오고, 하루 첫 거두기에 하나를 받아요. 코인 · 에너지 · 숫돌 · 높은 등급 물건이 들어 있어요.\n"
        f"**환전** — 코인 1 = 축구 봇 돈 {F.RATE:,}원. 환전하지 않아도 농사는 코인만으로 다 할 수 있어요.\n"
        "**행운** — 축구 봇 돈을 쓰면 상자 보상과 낫 강화 확률이 조금 좋아져요. 안 써도 괜찮아요."), ui.INFO, user, SECTION)


# ───────────── 농장 화면 (봇이 재시작해도 눌리는 버튼) ─────────────
class FarmPanel(discord.ui.View):
    def __init__(self, cog: "Farm", f: Optional[dict] = None):
        super().__init__(timeout=None)
        self.cog = cog
        soil = F.time_mult(f["res"]["soil"]) if f else 1
        grass = F.time_mult(f["res"]["grass"]) if f else 1
        opts = [discord.SelectOption(label=f"{n} 심기", value=k,
                                     description=f"씨앗 {f'{price:,} 코인' if price else '무료'} · {_dur(sec * soil)} · 팔면 {sell} 코인")
                for k, (e, n, price, sec, sell) in F.CROPS.items()]
        opts += [discord.SelectOption(label=f"{n} 입양", value=k,
                                      description=f"{price:,} 코인 · {_dur(sec * grass)}마다 {F.PRODUCTS[prod][1]} ({F.PRODUCTS[prod][2]} 코인)")
                 for k, (e, n, price, prod, sec) in F.ANIMALS.items()]
        plant = discord.ui.Select(custom_id="farm:plant", placeholder="심을 작물 · 입양할 동물 고르기", row=0, options=opts)
        plant.callback = self._plant
        self.add_item(plant)
        n = f["plant_n"] if f else 0
        qty = discord.ui.Select(custom_id="farm:qty", placeholder="한 번에 심을 칸 수", row=1, options=[
            discord.SelectOption(label="한 번에 빈 땅 모두 심기" if v == 0 else f"한 번에 {v}칸씩 심기", value=str(v), default=v == n)
            for v in (0, 1, 2, 3, 6)])
        qty.callback = self._qty
        self.add_item(qty)
        chests = sum(f["chests"].values()) if f else 0
        cost = F.expand_cost(f["plots"]) if f and f["plots"] < F.PLOTS_MAX else None
        for cid, label, style, row, cb in (
                ("harvest", "거두기", discord.ButtonStyle.success, 2, self._harvest),
                ("merge", "합치기", discord.ButtonStyle.primary, 2, self._merge),
                ("store", "창고 · 판매", discord.ButtonStyle.primary, 2, self._store),
                ("chest", f"상자 ({chests})" if chests else "상자", discord.ButtonStyle.primary, 2, self._chest),
                ("refresh", "새로고침", discord.ButtonStyle.secondary, 2, self._refresh),
                ("expand", f"땅 넓히기 ({cost:,} 코인)" if cost else "땅 넓히기", discord.ButtonStyle.secondary, 3, self._expand),
                ("sickle", "낫 강화", discord.ButtonStyle.secondary, 3, self._sickle),
                ("exchange", "환전", discord.ButtonStyle.secondary, 3, self._exchange),
                ("help", "도움말", discord.ButtonStyle.secondary, 3, self._help)):
            b = discord.ui.Button(custom_id=f"farm:{cid}", label=label, style=style, row=row)
            b.callback = cb
            self.add_item(b)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not release.preview_381(interaction.guild_id):   # 공개 전엔 테스트 서버의 농장만
            await interaction.response.send_message("/농사 는 아직 이 서버에서 쓸 수 없어요.", ephemeral=True)
            return False
        if await self.cog.db.thread_owner(interaction.channel_id) != interaction.user.id:
            await interaction.response.send_message("농장 주인만 누를 수 있어요. `/농사`로 내 농장을 열어 보세요.", ephemeral=True)
            return False
        return True

    async def _plant(self, interaction: discord.Interaction):
        key = interaction.data["values"][0]
        r = await self.cog.db.plant(interaction.user.id, key, int(time.time()))
        if not r["ok"]:
            note = {"full": "빈 땅이 없어요. 다 자란 것을 거두거나 땅을 넓혀 보세요.",
                    "energy": "에너지가 없어요. 조금 기다리면 다시 차요.",
                    "coins": f"코인이 모자라요 (하나에 {r.get('price', 0):,} 코인). 당근 씨앗은 무료예요.",
                    "cap": f"동물은 땅 {F.ANIMAL_PER_PLOTS}칸당 1마리까지예요 (지금 {r.get('cap', 0)}마리까지). 땅을 넓혀 보세요.",
                    }.get(r["reason"], "심을 수 없어요.")
        elif r["animal"]:
            _, n, _, prod, _ = F.ANIMALS[key]
            note = f"**{n}**을(를) 입양했어요 (코인 -{r['cost']:,}) · 첫 {_item(prod)} <t:{r['ready_ts']}:R>"
        else:
            note = (f"{_item(key)} **{r['count']}칸**을 심었어요" + (f" (코인 -{r['cost']:,})" if r["cost"] else "")
                    + f" · <t:{r['ready_ts']}:R> 다 자라요")
        await self.cog.show(interaction, note)

    async def _qty(self, interaction: discord.Interaction):
        await self.cog.db.set_plant_n(interaction.user.id, int(interaction.data["values"][0]))
        await self.cog.show(interaction)

    async def _harvest(self, interaction: discord.Interaction):
        r = await self.cog.db.harvest(interaction.user.id, int(time.time()))
        if not r["ok"]:
            note = "에너지가 없어요. 조금 기다리면 다시 차요." if r["reason"] == "energy" else "아직 거둘 것이 없어요."
        else:
            note = f"**{r['count']}칸**을 거뒀어요: " + " · ".join(f"{_item(k, t)} ×{q}" for (k, t), q in r["got"].items())
            if r["extra"] or r["high"]:
                note += "\n낫 덕분에 " + " · ".join(x for x in (f"하나 더 ×{r['extra']}" if r["extra"] else "",
                                                               f"★2 ×{r['high']}" if r["high"] else "") if x)
            if r["chests"]:
                note += "\n상자를 발견했어요: " + " · ".join(
                    f"{F.CHESTS[c][1]} ×{n}" for c, n in Counter(r["chests"]).items())
            if r["daily"]:
                c = F.CHESTS[F.DAILY_CHEST]
                note += f"\n오늘 첫 수확 선물: {c[1]}"
            if r["left"]:
                note += f"\n에너지가 모자라서 {r['left']}칸은 남았어요."
        await self.cog.show(interaction, note)

    async def _refresh(self, interaction: discord.Interaction):
        await self.cog.show(interaction)

    async def _expand(self, interaction: discord.Interaction):
        r = await self.cog.db.expand(interaction.user.id, int(time.time()))
        if r["ok"]:
            c = F.CHESTS[r["chest"]]
            note = f"땅이 **{r['plots']}칸**으로 넓어졌어요 (코인 -{r['cost']:,}) · {c[1]}를 발견했어요"
        else:
            note = (f"땅은 {F.PLOTS_MAX}칸이 끝이에요." if r["reason"] == "max"
                    else f"코인이 모자라요 (땅 넓히기 {r.get('cost', 0):,} 코인).")
        await self.cog.show(interaction, note)

    async def _sub(self, interaction: discord.Interaction, cls):
        f = await self.cog.db.get(interaction.user.id, int(time.time()))
        view = cls(self.cog, interaction.user, interaction.message, f)
        await interaction.response.send_message(embed=view.embed(f), view=view, ephemeral=True)

    async def _merge(self, interaction: discord.Interaction):
        await self._sub(interaction, MergeView)

    async def _store(self, interaction: discord.Interaction):
        await self._sub(interaction, StoreView)

    async def _chest(self, interaction: discord.Interaction):
        await self._sub(interaction, ChestView)

    async def _sickle(self, interaction: discord.Interaction):
        await self._sub(interaction, SickleView)

    async def _exchange(self, interaction: discord.Interaction):
        f = await self.cog.db.get(interaction.user.id, int(time.time()))
        await interaction.response.send_modal(ExchangeModal(self.cog, f["coins"]))

    async def _help(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=help_embed(interaction.user), ephemeral=True)


# ───────────── 작은 화면들 (누른 사람에게만 보인다 — 끝나면 농장 화면도 고친다) ─────────────
class _Sub(discord.ui.View):
    def __init__(self, cog: "Farm", user, panel, f: dict):
        super().__init__(timeout=180)
        self.cog, self.user, self.panel, self.note, self.win = cog, user, panel, "", False
        self.build(f)

    def build(self, f: dict):
        raise NotImplementedError

    def embed(self, f: dict) -> discord.Embed:
        raise NotImplementedError

    async def redraw(self, interaction: discord.Interaction, note: str = "", panel_note: str = "", win: bool = False):
        """이 화면과 농장 화면을 지금 상태로 다시 그린다."""
        now = int(time.time())
        f = await self.cog.db.get(self.user.id, now)
        self.note, self.win = note, win
        self.clear_items()
        self.build(f)
        await interaction.response.edit_message(embed=self.embed(f), view=self)
        try:
            await self.panel.edit(embed=panel_embed(self.user, f, now, panel_note), view=FarmPanel(self.cog, f))
        except discord.HTTPException:
            pass


class MergeView(_Sub):
    def build(self, f: dict):
        able = [(k, t, q) for k, t, q in f["items"] if q >= F.MERGE_NEED and t < F.TIER_MAX][:25]
        if able:
            sel = discord.ui.Select(placeholder="합칠 물건 고르기", options=[
                discord.SelectOption(label=f"{F.ITEMS[k][1]} ★{t} ×{q} → ★{t + 1} ×{q // F.MERGE_NEED}", value=f"{k}:{t}",
                                     description=f"에너지 {q // F.MERGE_NEED} · 하나에 {F.value(k, t + 1):,} 코인")
                for k, t, q in able])
            sel.callback = self._pick
            self.add_item(sel)

    def embed(self, f: dict) -> discord.Embed:
        able = [(k, t, q) for k, t, q in f["items"] if q >= F.MERGE_NEED and t < F.TIER_MAX]
        body = ("\n".join(f"{_item(k, t)} ×{q}" for k, t, q in able) if able
                else f"지금은 합칠 수 있는 것이 없어요. 같은 물건 · 같은 등급이 {F.MERGE_NEED}개 있어야 해요.")
        return ui.card("합치기", (f"{self.note}\n\n" if self.note else "")
                       + f"같은 물건 {F.MERGE_NEED}개 → 한 등급 위 1개 (한 번에 에너지 1 · ★{F.TIER_MAX} 까지)\n에너지 **{f['energy']}**\n\n{body}",
                       ui.INFO, self.user, SECTION)

    async def _pick(self, interaction: discord.Interaction):
        k, t = interaction.data["values"][0].split(":")
        r = await self.cog.db.merge(self.user.id, k, int(t), int(time.time()))
        note = (f"{_item(k, int(t))} ×{r['used']} → **{_item(k, r['tier'])} ×{r['made']}**" if r["ok"] else
                {"energy": "에너지가 없어요.", "few": f"{F.MERGE_NEED}개가 있어야 합칠 수 있어요."}.get(r["reason"], "합칠 수 없어요."))
        await self.redraw(interaction, note, note if r["ok"] else "")


class StoreView(_Sub):
    def build(self, f: dict):
        items = f["items"][:25]
        if items:
            sel = discord.ui.Select(placeholder="팔 물건 고르기 (여러 개 가능)", min_values=1, max_values=len(items), row=0, options=[
                discord.SelectOption(label=f"{F.ITEMS[k][1]} ★{t} ×{q}", value=f"{k}:{t}",
                                     description=f"하나에 {F.value(k, t):,} · 모두 {F.value(k, t) * q:,} 코인") for k, t, q in items])
            sel.callback = self._sell
            self.add_item(sel)
            b = discord.ui.Button(label="모두 팔기", style=discord.ButtonStyle.success, row=2)
            b.callback = self._sell_all
            self.add_item(b)
        pets = [(i, p) for i, p in enumerate(f["fields"]) if p and p["kind"] == "animal"][:25]
        if pets:
            sel = discord.ui.Select(placeholder="내보낼 동물 고르기 (입양 값의 절반을 돌려받아요)", row=1, options=[
                discord.SelectOption(label=f"{i + 1}번 땅 {F.ANIMALS[p['key']][1]}", value=str(i),
                                     description=f"{int(F.ANIMALS[p['key']][2] * F.ANIMAL_REFUND):,} 코인") for i, p in pets])
            sel.callback = self._release
            self.add_item(sel)

    def embed(self, f: dict) -> discord.Embed:
        rows = [f"{_item(k, t)} ×{q} — 하나에 {F.value(k, t):,} 코인" for k, t, q in f["items"]]
        worth = sum(F.value(k, t) * q for k, t, q in f["items"])
        return ui.card("창고 · 판매", (f"{self.note}\n\n" if self.note else "")
                       + ("\n".join(rows) + f"\n\n모두 팔면 **{worth:,} 코인**" if rows else "창고가 비어 있어요.")
                       + f"\n가진 코인 **{f['coins']:,}**\n-# 3개를 모아 합치면 더 비싸게 팔 수 있어요.", ui.INFO, self.user, SECTION)

    async def _done(self, interaction: discord.Interaction, r: dict):
        note = (f"**+{r['coins']:,} 코인** — " + " · ".join(f"{_item(k, t)} ×{q}" for k, t, q in r["sold"]) if r["ok"]
                else "팔 물건이 없어요.")
        await self.redraw(interaction, note, note if r["ok"] else "")

    async def _sell(self, interaction: discord.Interaction):
        picks = [(v.split(":")[0], int(v.split(":")[1])) for v in interaction.data["values"]]
        await self._done(interaction, await self.cog.db.sell(self.user.id, int(time.time()), picks))

    async def _sell_all(self, interaction: discord.Interaction):
        await self._done(interaction, await self.cog.db.sell(self.user.id, int(time.time())))

    async def _release(self, interaction: discord.Interaction):
        r = await self.cog.db.sell_animal(self.user.id, int(interaction.data["values"][0]), int(time.time()))
        note = (f"{F.ANIMALS[r['key']][1]}을(를) 내보냈어요 (코인 +{r['coins']:,})" if r["ok"]
                else "그 땅에는 동물이 없어요.")
        await self.redraw(interaction, note, note if r["ok"] else "")


class ChestView(_Sub):
    lucky = False

    def build(self, f: dict):
        for tier, (_, n) in F.CHESTS.items():
            have = f["chests"][tier]
            label = f"{n} 열기 ({have})" + (f" · {F.LUCKY_FEE[tier]:,}원" if self.lucky else "")
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.success, disabled=have <= 0, row=0)
            b.callback = self._open(tier)
            self.add_item(b)
        t = discord.ui.Button(label="행운 열기: 켜짐" if self.lucky else "행운 열기: 꺼짐", row=1,
                              style=discord.ButtonStyle.primary if self.lucky else discord.ButtonStyle.secondary)
        t.callback = self._toggle
        self.add_item(t)

    def embed(self, f: dict) -> discord.Embed:
        have = " · ".join(f"{n} **{f['chests'][k]}**" for k, (_, n) in F.CHESTS.items())
        fees = " · ".join(f"{n} {F.LUCKY_FEE[k]:,}원" for k, (_, n) in F.CHESTS.items())
        return ui.card("상자", (f"{self.note}\n\n" if self.note else "") + f"{have}\n\n"
                       "상자에는 코인 · 에너지 · 숫돌 · 높은 등급 물건이 들어 있어요. 좋은 상자일수록 보상이 많아요.\n"
                       f"**행운 열기** — 축구 봇 돈을 내면 보상마다 두 번 뽑아 더 좋은 쪽을 받아요.\n-# {fees} · 내 잔액 {f['balance']:,}원",
                       ui.GOLD if self.win else ui.INFO, self.user, SECTION)

    def _open(self, tier: str):
        async def cb(interaction: discord.Interaction):
            r = await self.cog.db.open_chest(self.user.id, tier, int(time.time()), lucky=self.lucky)
            if not r["ok"]:
                note = (f"축구 봇 돈이 모자라요 (행운 열기 {r.get('fee', 0):,}원). 행운 열기를 끄면 그냥 열 수 있어요."
                        if r["reason"] == "money" else "그 상자가 없어요.")
                return await self.redraw(interaction, note)
            got = []
            for x in r["rewards"]:
                got.append({"coins": f"코인 +{x['n']:,}", "energy": f"에너지 +{x['n']}", "whet": f"숫돌 +{x['n']}"}.get(x["kind"])
                           or f"{_item(x['key'], x['tier'])} ×{x['n']}")
            n = F.CHESTS[tier][1]
            note = f"**{n}**를 열었어요" + (f" (행운 -{r['fee']:,}원)" if r["fee"] else "") + ": " + " · ".join(f"**{g}**" for g in got)
            await self.redraw(interaction, note, note, win=True)
        return cb

    async def _toggle(self, interaction: discord.Interaction):
        self.lucky = not self.lucky
        await self.redraw(interaction)


class SickleView(_Sub):
    charm = False

    def build(self, f: dict):
        lv = f["sickle"]
        done = lv >= F.SICKLE_MAX
        for label, whet in ((f"코인으로 강화 ({F.sickle_cost(lv):,})" if not done else "코인으로 강화", False),
                            (f"숫돌로 강화 ({F.sickle_whet(lv)}개)" if not done else "숫돌로 강화", True)):
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.success, disabled=done, row=0)
            b.callback = self._go(whet)
            self.add_item(b)
        fee = "" if done else f" · {F.CHARM_FEE * (lv + 1):,}원"
        t = discord.ui.Button(label=("행운 부적: 켜짐" if self.charm else "행운 부적: 꺼짐") + (fee if self.charm else ""),
                              style=discord.ButtonStyle.primary if self.charm else discord.ButtonStyle.secondary, disabled=done, row=1)
        t.callback = self._toggle
        self.add_item(t)

    def embed(self, f: dict) -> discord.Embed:
        lv = f["sickle"]
        now = f"거둘 때 하나 더 **{F.extra_p(lv):.0%}** · ★2 로 **{F.high_p(lv):.0%}**"
        if lv >= F.SICKLE_MAX:
            body = f"낫 **+{lv}** (최고 단계)\n{now}"
        else:
            p = min(1.0, F.SICKLE_CHANCE[lv] + (F.CHARM_BONUS if self.charm else 0))
            body = (f"낫 **+{lv}** → **+{lv + 1}**\n{now}\n강화하면 하나 더 **{F.extra_p(lv + 1):.0%}** · ★2 로 **{F.high_p(lv + 1):.0%}**\n\n"
                    f"`성공 확률` **{p:.0%}**" + (" (부적 포함)" if self.charm else "") + " · 실패해도 단계는 내려가지 않아요\n"
                    f"`재료` {F.sickle_cost(lv):,} 코인 또는 숫돌 {F.sickle_whet(lv)}개\n"
                    f"**행운 부적** — 축구 봇 돈 {F.CHARM_FEE * (lv + 1):,}원을 내면 성공 확률 +{F.CHARM_BONUS:.0%}p")
        return ui.card("낫 강화", (f"{self.note}\n\n" if self.note else "") + body
                       + f"\n\n코인 **{f['coins']:,}** · 숫돌 **{f['whet']}** · 내 잔액 {f['balance']:,}원",
                       ui.GOLD if self.win else ui.INFO, self.user, SECTION)

    def _go(self, whet: bool):
        async def cb(interaction: discord.Interaction):
            r = await self.cog.db.enhance(self.user.id, int(time.time()), use_whet=whet, charm=self.charm)
            if not r["ok"]:
                note = {"coins": f"코인이 모자라요 ({r.get('cost', 0):,} 코인).",
                        "whet": f"숫돌이 모자라요 ({r.get('need', 0)}개). 숫돌은 상자에서 나와요.",
                        "money": f"축구 봇 돈이 모자라요 (행운 부적 {r.get('fee', 0):,}원). 부적을 끄면 그냥 강화할 수 있어요.",
                        "max": "이미 최고 단계예요."}.get(r["reason"], "강화할 수 없어요.")
                return await self.redraw(interaction, note)
            note = (f"강화 성공! 낫이 **+{r['level']}**이 됐어요." if r["success"]
                    else f"강화 실패… 낫은 **+{r['level']}** 그대로예요.")
            await self.redraw(interaction, note, note if r["success"] else "", win=r["success"])
        return cb

    async def _toggle(self, interaction: discord.Interaction):
        self.charm = not self.charm
        await self.redraw(interaction)


class ExchangeModal(discord.ui.Modal):
    def __init__(self, cog: "Farm", coins: int):
        super().__init__(title="농장 코인 환전")
        self.cog = cog
        self.amount = discord.ui.TextInput(label=f"바꿀 코인 (코인 1 = {F.RATE:,}원)"[:45], max_length=12,
                                           placeholder=f"가진 코인 {coins:,} · '전부'라고 적으면 모두 바꿔요")
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.amount.value).replace(",", "").replace("코인", "").strip()
        now = int(time.time())
        if raw in ("전부", "모두", "all"):
            raw = str((await self.cog.db.get(interaction.user.id, now))["coins"])
        if not raw.isdigit():
            return await interaction.response.send_message("바꿀 코인은 숫자로 적어 주세요.", ephemeral=True)
        r = await self.cog.db.exchange(interaction.user.id, int(raw), now)
        note = (f"{r['coins']:,} 코인 → **{r['won']:,}원** · 잔액 {r['balance']:,}원" if r["ok"] else
                f"코인이 모자라요 (가진 코인 {r.get('have', 0):,})." if r["reason"] == "coins" else "1 코인 이상 적어 주세요.")
        await self.cog.show(interaction, note)


class LandPick(discord.ui.View):
    """처음 한 번: 땅을 고르고 [시작]."""

    def __init__(self, cog: "Farm", user):
        super().__init__(timeout=300)
        self.cog, self.user, self.pick = cog, user, None
        self._draw()

    def _draw(self):
        self.clear_items()
        sel = discord.ui.Select(placeholder="땅 고르기", options=[
            discord.SelectOption(label=n, value=k, description=desc[:100], default=k == self.pick)
            for k, (_, n, desc, _) in F.LANDS.items()])
        sel.callback = self._picked
        self.add_item(sel)
        go = discord.ui.Button(label=f"{F.LANDS[self.pick][1]}에서 시작하기" if self.pick else "땅을 먼저 골라 주세요",
                               style=discord.ButtonStyle.success, disabled=not self.pick)
        go.callback = self._start
        self.add_item(go)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user.id

    async def _picked(self, interaction: discord.Interaction):
        self.pick = interaction.data["values"][0]
        self._draw()
        await interaction.response.edit_message(embed=land_embed(self.user, self.pick), view=self)

    async def _start(self, interaction: discord.Interaction):
        self.stop()
        r = await self.cog.db.create(self.user.id, self.pick, int(time.time()))
        _, n, desc, _ = F.LANDS[self.pick]
        msg = f"**{n}**에 농장을 열었어요. {desc}." if r["ok"] else "이미 농장이 있어요."
        await interaction.response.edit_message(embed=ui.card("농사 시작", msg, ui.GOLD, self.user, SECTION), view=None)
        await self.cog.open(interaction, self.user)


class Farm(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = F.FarmDB()
        self.panel = FarmPanel(self)
        bot.add_view(self.panel)   # 재시작 전에 올라간 농장 화면의 버튼도 받는다

    def cog_unload(self):
        self.panel.stop()

    async def show(self, interaction: discord.Interaction, note: str = ""):
        """누른 농장 화면을 지금 상태로 다시 그린다."""
        now = int(time.time())
        f = await self.db.get(interaction.user.id, now)
        await interaction.response.edit_message(embed=panel_embed(interaction.user, f, now, note), view=FarmPanel(self, f))

    async def open(self, interaction: discord.Interaction, user):
        """내 농장 스레드를 연다 (없으면 이 채널에 만든다). 내 스레드 안에서 부르면 농장 화면을 새로 올린다."""
        guild, ch, now = interaction.guild, interaction.channel, int(time.time())
        f = await self.db.get(user.id, now)
        tid = await self.db.get_thread(user.id, guild.id)
        if tid and ch.id == tid and not interaction.response.is_done():
            return await interaction.response.send_message(embed=panel_embed(user, f, now), view=FarmPanel(self, f))
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        thread = None
        if tid:
            thread = guild.get_thread(tid)
            if thread is None:
                try:
                    thread = await guild.fetch_channel(tid)
                except discord.HTTPException:   # 지워진 스레드
                    thread = None
            if not isinstance(thread, discord.Thread):
                thread = None
        created = thread is None
        try:
            if created:
                parent = ch.parent if isinstance(ch, discord.Thread) else ch
                if not isinstance(parent, discord.TextChannel):
                    return await interaction.followup.send(
                        "농장 스레드는 일반 채팅 채널에서만 열 수 있어요. 채팅 채널에서 `/농사`를 써 주세요.", ephemeral=True)
                thread = await parent.create_thread(name=f"{user.display_name}의 농장"[:100],
                                                    type=discord.ChannelType.public_thread, auto_archive_duration=10080,
                                                    reason=f"/농사 — {user}")
                await self.db.set_thread(user.id, guild.id, thread.id)
            elif thread.archived:
                await thread.edit(archived=False)
            await thread.send(content=user.mention if created else None, embed=panel_embed(user, f, now),
                              view=FarmPanel(self, f))
        except discord.Forbidden:
            return await interaction.followup.send(
                "봇이 이 채널에 스레드를 만들거나 글을 쓸 수 없어요. 서버 관리자가 봇 역할에 **공개 스레드 만들기**와 "
                "**스레드에서 메시지 보내기** 권한을 켜 주세요.", ephemeral=True)
        except discord.HTTPException as ex:
            print(f"[농사] 스레드 열기 오류: {type(ex).__name__}: {ex}")
            return await interaction.followup.send("농장 스레드를 열지 못했어요. 잠시 뒤에 다시 해 주세요.", ephemeral=True)
        await interaction.followup.send(f"{'농장이 열렸어요' if created else '내 농장은 여기예요'}: {thread.mention}", ephemeral=True)

    @app_commands.command(name="농사", description="내 농장 — 땅을 골라 작물과 동물을 기르고, 합치고, 넓혀요")
    async def farm(self, interaction: discord.Interaction):
        if not release.preview_381(interaction.guild_id):
            return await interaction.response.send_message("/농사 는 아직 이 서버에서 쓸 수 없어요.", ephemeral=True)
        if interaction.guild is None:
            return await interaction.response.send_message("/농사 는 서버 채널에서 써 주세요.", ephemeral=True)
        user = interaction.user
        if await self.db.get(user.id, int(time.time())) is None:
            return await interaction.response.send_message(embed=land_embed(user), view=LandPick(self, user), ephemeral=True)
        await self.open(interaction, user)


async def setup(bot: commands.Bot):
    await bot.add_cog(Farm(bot))
