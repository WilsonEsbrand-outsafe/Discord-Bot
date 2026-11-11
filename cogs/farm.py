# cogs/farm.py — /농사 (3.8.1, 테스트 서버 먼저)
# /농사 → 땅 고르기 → 그 채널에 내 농장 스레드(공개 · 조작은 주인만)가 열리고, 농장 화면의 버튼 · 메뉴로 논다.
# 다른 사람 농장 스레드에서는 [인수 제안]만 누를 수 있다 (명문구단처럼 제안 → 주인이 수락 · 적정 인수 금액 이상이면 바로).
# 농장은 여러 개 가질 수 있다 — 스레드 하나가 농장 하나 (farm_threads).
# 씨앗 · 동물은 서버마다 하나 있는 '농사 상점' 스레드에서 산다 (farm_shops) — 공개되면 모든 서버의 패치노트 채널에 생긴다.
# 농장 화면 · 상점의 버튼은 custom_id 가 고정(farm:…)이라 봇이 재시작해도 계속 눌린다. 규칙 · 수치는 services/farm.py.
import asyncio
import time
from collections import Counter
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import release
from cogs.patch_notes import PATCH_THREADS
from services import farm as F
from services import ui

SECTION = "농사"
ROW = 6   # 땅 그림 한 줄 칸 수
SHOP_NAME = "농사 상점"
SHOPS: dict[int, int] = {}   # 서버 → 농사 상점 스레드 (farm_shops 표를 읽어 둔 것)


def _name(key: str) -> str:
    return (F.CROPS.get(key) or F.ANIMALS[key])[1]


def _shop_link(user) -> str:
    tid = SHOPS.get(getattr(getattr(user, "guild", None), "id", None))
    return f"<#{tid}>" if tid else f"**{SHOP_NAME}** 스레드"


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
    if f["seeds"]:
        lines.append("**사 둔 씨앗 · 동물:** " + " · ".join(f"{_name(k)} ×{q}" for k, q in f["seeds"].items()))
    if not (ready or growing or animals or f["items"]):
        lines.append("아래 메뉴에서 당근을 골라 심어 보세요. 당근은 씨앗이 필요 없어요.")
    lines.append(f"-# 씨앗 · 동물은 {_shop_link(user)}에서 사요")
    e = ui.card(f"{user.display_name}의 농장", (f"{note}\n\n" if note else "") + f"{head}\n{stat}\n\n{grid}\n\n" + "\n".join(lines),
                ui.WIN, user, SECTION)
    e.set_footer(text="농장 주인만 누를 수 있어요 (인수 제안은 누구나) · 심기 · 거두기 · 합치기는 한 번에 에너지 1")
    return e


def help_embed(user) -> discord.Embed:
    return ui.card("농사 도움말", (
        "**심기** — 메뉴에서 작물을 고르면 빈 땅에 심어요. 시간이 지나면 다 자라요.\n"
        f"**농사 상점** — 씨앗과 동물은 {_shop_link(user)}에서 농장 코인으로 사요. 당근은 씨앗 없이 바로 심을 수 있어요.\n"
        "**동물** — 상점에서 산 동물을 입양하면 땅 한 칸에 살면서 달걀 · 양털 · 우유를 계속 만들어요 (땅 3칸당 1마리).\n"
        "**거두기** — 다 자란 작물과 동물이 만든 것을 창고로 옮겨요.\n"
        "**합치기** — 같은 물건 3개를 합치면 한 등급 위 1개가 돼요 (★5 까지). 등급이 높을수록 훨씬 비싸요.\n"
        "**창고** — 물건을 농장 코인으로 팔아요. 코인으로 씨앗 · 동물을 사고, 땅을 넓히고, 낫을 강화해요.\n"
        f"**에너지** — 심기 · 거두기 · 합치기 한 번에 1. 시간이 지나면 다시 차요 (최대 {F.ENERGY_MAX}).\n"
        "**땅 넓히기** — 코인으로 한 칸씩 넓혀요. 넓힐 때마다 은 상자를 하나 발견해요.\n"
        "**낫 강화** — 강화할수록 거둘 때 하나 더 나올 확률과 ★2 로 나올 확률이 올라요.\n"
        "**상자** — 거두다 가끔 나오고, 하루 첫 거두기에 하나를 받아요. 코인 · 에너지 · 숫돌 · 높은 등급 물건이 들어 있어요.\n"
        f"**환전** — 코인 1 = 축구 봇 돈 {F.RATE:,}원. 환전하지 않아도 농사는 코인만으로 다 할 수 있어요.\n"
        "**행운** — 축구 봇 돈을 쓰면 상자 보상과 낫 강화 확률이 조금 좋아져요. 안 써도 괜찮아요.\n"
        "**인수** — 다른 사람 농장 스레드에서 **인수 제안**을 눌러 금액을 제안해요. 적정 인수 금액 이상이면 바로 인수되고, "
        "그보다 적으면 주인이 수락해야 해요. 농장은 여러 개 가질 수 있어요."), ui.INFO, user, SECTION)


# ───────────── 농장 화면 (봇이 재시작해도 눌리는 버튼) ─────────────
class FarmPanel(discord.ui.View):
    def __init__(self, cog: "Farm", f: Optional[dict] = None):
        super().__init__(timeout=None)
        self.cog = cog
        soil = F.time_mult(f["res"]["soil"]) if f else 1
        grass = F.time_mult(f["res"]["grass"]) if f else 1
        have = f["seeds"] if f else {}
        opts = [discord.SelectOption(label=f"{n} 심기", value=k,
                                     description=(f"씨앗 {have.get(k, 0)}개" if price else "씨앗 필요 없음")
                                     + f" · {_dur(sec * soil)} · 팔면 {sell} 코인")
                for k, (e, n, price, sec, sell) in F.CROPS.items()]
        opts += [discord.SelectOption(label=f"{n} 입양", value=k,
                                      description=f"사 둔 {have.get(k, 0)}마리 · {_dur(sec * grass)}마다 {F.PRODUCTS[prod][1]} "
                                                  f"({F.PRODUCTS[prod][2]} 코인)")
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
        offers = f.get("offers", 0) if f else 0
        for cid, label, style, row, cb in (
                ("harvest", "거두기", discord.ButtonStyle.success, 2, self._harvest),
                ("merge", "합치기", discord.ButtonStyle.primary, 2, self._merge),
                ("store", "창고 · 판매", discord.ButtonStyle.primary, 2, self._store),
                ("chest", f"상자 ({chests})" if chests else "상자", discord.ButtonStyle.primary, 2, self._chest),
                ("refresh", "새로고침", discord.ButtonStyle.secondary, 2, self._refresh),
                ("expand", f"땅 넓히기 ({cost:,} 코인)" if cost else "땅 넓히기", discord.ButtonStyle.secondary, 3, self._expand),
                ("sickle", "낫 강화", discord.ButtonStyle.secondary, 3, self._sickle),
                ("exchange", "환전", discord.ButtonStyle.secondary, 3, self._exchange),
                ("help", "도움말", discord.ButtonStyle.secondary, 3, self._help),
                ("offer", "인수 제안", discord.ButtonStyle.secondary, 4, self._offer),
                ("offers", f"받은 인수 제안 ({offers})" if offers else "받은 인수 제안", discord.ButtonStyle.secondary, 4,
                 self._offers)):
            b = discord.ui.Button(custom_id=f"farm:{cid}", label=label, style=style, row=row)
            b.callback = cb
            self.add_item(b)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not release.preview_381(interaction.guild_id):   # 공개 전엔 테스트 서버의 농장만
            await interaction.response.send_message("/농사 는 아직 이 서버에서 쓸 수 없어요.", ephemeral=True)
            return False
        owner = await self.cog.db.thread_owner(interaction.channel_id)
        if (interaction.data or {}).get("custom_id") == "farm:offer":   # 인수 제안은 주인 말고 누구나
            if owner is None:
                await interaction.response.send_message("이 스레드에는 농장이 없어요.", ephemeral=True)
                return False
            if owner == interaction.user.id:
                await interaction.response.send_message("내 농장이에요. 들어온 제안은 **받은 인수 제안**에서 볼 수 있어요.", ephemeral=True)
                return False
            return True
        if owner != interaction.user.id:
            await interaction.response.send_message("농장 주인만 누를 수 있어요. `/농사`로 내 농장을 열어 보세요.", ephemeral=True)
            return False
        return True

    async def _fid(self, interaction: discord.Interaction) -> int:
        """누른 스레드의 농장 번호."""
        return await self.cog.db.thread_farm(interaction.channel_id) or 0

    async def _plant(self, interaction: discord.Interaction):
        key, fid = interaction.data["values"][0], await self._fid(interaction)
        r = await self.cog.db.plant(interaction.user.id, fid, key, int(time.time()))
        if not r["ok"]:
            what = f"사 둔 {_name(key)}이(가)" if key in F.ANIMALS else f"{_name(key)} 씨앗이"
            note = {"full": "빈 땅이 없어요. 다 자란 것을 거두거나 땅을 넓혀 보세요.",
                    "energy": "에너지가 없어요. 조금 기다리면 다시 차요.",
                    "seeds": f"{what} 없어요. {_shop_link(interaction.user)}에서 사 주세요. 당근은 씨앗 없이 심을 수 있어요.",
                    "cap": f"동물은 땅 {F.ANIMAL_PER_PLOTS}칸당 1마리까지예요 (지금 {r.get('cap', 0)}마리까지). 땅을 넓혀 보세요.",
                    }.get(r["reason"], "심을 수 없어요.")
        elif r["animal"]:
            _, n, _, prod, _ = F.ANIMALS[key]
            note = f"**{n}**을(를) 입양했어요 · 첫 {_item(prod)} <t:{r['ready_ts']}:R>"
        else:
            note = (f"{_item(key)} **{r['count']}칸**을 심었어요" + (f" (남은 씨앗 {r['left']}개)" if r["left"] is not None else "")
                    + f" · <t:{r['ready_ts']}:R> 다 자라요")
        await self.cog.show(interaction, fid, note)

    async def _qty(self, interaction: discord.Interaction):
        fid = await self._fid(interaction)
        await self.cog.db.set_plant_n(interaction.user.id, fid, int(interaction.data["values"][0]))
        await self.cog.show(interaction, fid)

    async def _harvest(self, interaction: discord.Interaction):
        fid = await self._fid(interaction)
        r = await self.cog.db.harvest(interaction.user.id, fid, int(time.time()))
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
        await self.cog.show(interaction, fid, note)

    async def _refresh(self, interaction: discord.Interaction):
        await self.cog.show(interaction, await self._fid(interaction))

    async def _expand(self, interaction: discord.Interaction):
        fid = await self._fid(interaction)
        r = await self.cog.db.expand(interaction.user.id, fid, int(time.time()))
        if r["ok"]:
            c = F.CHESTS[r["chest"]]
            note = f"땅이 **{r['plots']}칸**으로 넓어졌어요 (코인 -{r['cost']:,}) · {c[1]}를 발견했어요"
        else:
            note = (f"땅은 {F.PLOTS_MAX}칸이 끝이에요." if r["reason"] == "max"
                    else f"코인이 모자라요 (땅 넓히기 {r.get('cost', 0):,} 코인).")
        await self.cog.show(interaction, fid, note)

    async def _sub(self, interaction: discord.Interaction, cls):
        f = await self.cog.db.get(await self._fid(interaction), int(time.time()))
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
        f = await self.cog.db.get(await self._fid(interaction), int(time.time()))
        await interaction.response.send_modal(ExchangeModal(self.cog, f["id"], f["coins"]))

    async def _help(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=help_embed(interaction.user), ephemeral=True)

    async def _offer(self, interaction: discord.Interaction):
        f = await self.cog.db.get(await self._fid(interaction), int(time.time()))
        bal = await self.cog.db.get_balance(interaction.user.id)
        view = OfferStart(self.cog, interaction.user, f, interaction.message)
        await interaction.response.send_message(embed=view.embed(f, bal), view=view, ephemeral=True)

    async def _offers(self, interaction: discord.Interaction):
        now = int(time.time())
        f = await self.cog.db.get(await self._fid(interaction), now)
        offers = await self.cog.db.farm_offers(interaction.user.id, f["id"], now)
        view = OwnerOffers(self.cog, interaction.user, f, offers, interaction.message)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)


# ───────────── 작은 화면들 (누른 사람에게만 보인다 — 끝나면 농장 화면도 고친다) ─────────────
class _Sub(discord.ui.View):
    def __init__(self, cog: "Farm", user, panel, f: dict):
        super().__init__(timeout=180)
        self.cog, self.user, self.panel, self.note, self.win = cog, user, panel, "", False
        self.fid = f["id"]
        self.build(f)

    def build(self, f: dict):
        raise NotImplementedError

    def embed(self, f: dict) -> discord.Embed:
        raise NotImplementedError

    async def redraw(self, interaction: discord.Interaction, note: str = "", panel_note: str = "", win: bool = False):
        """이 화면과 농장 화면을 지금 상태로 다시 그린다."""
        now = int(time.time())
        f = await self.cog.db.get(self.fid, now)
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
        r = await self.cog.db.merge(self.user.id, self.fid, k, int(t), int(time.time()))
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
            sel = discord.ui.Select(placeholder="내보낼 동물 고르기 (산 값의 절반을 돌려받아요)", row=1, options=[
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
        await self._done(interaction, await self.cog.db.sell(self.user.id, self.fid, int(time.time()), picks))

    async def _sell_all(self, interaction: discord.Interaction):
        await self._done(interaction, await self.cog.db.sell(self.user.id, self.fid, int(time.time())))

    async def _release(self, interaction: discord.Interaction):
        r = await self.cog.db.sell_animal(self.user.id, self.fid, int(interaction.data["values"][0]), int(time.time()))
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
            r = await self.cog.db.open_chest(self.user.id, self.fid, tier, int(time.time()), lucky=self.lucky)
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
            r = await self.cog.db.enhance(self.user.id, self.fid, int(time.time()), use_whet=whet, charm=self.charm)
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
    def __init__(self, cog: "Farm", fid: int, coins: int):
        super().__init__(title="농장 코인 환전")
        self.cog, self.fid = cog, fid
        self.amount = discord.ui.TextInput(label=f"바꿀 코인 (코인 1 = {F.RATE:,}원)"[:45], max_length=12,
                                           placeholder=f"가진 코인 {coins:,} · '전부'라고 적으면 모두 바꿔요")
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.amount.value).replace(",", "").replace("코인", "").strip()
        now = int(time.time())
        if raw in ("전부", "모두", "all"):
            raw = str((await self.cog.db.get(self.fid, now))["coins"])
        if not raw.isdigit():
            return await interaction.response.send_message("바꿀 코인은 숫자로 적어 주세요.", ephemeral=True)
        r = await self.cog.db.exchange(interaction.user.id, self.fid, int(raw), now)
        if not r["ok"] and r["reason"] == "none":   # 그 사이 농장 주인이 바뀌었다
            return await interaction.response.send_message("내 농장이 아니에요.", ephemeral=True)
        note = (f"{r['coins']:,} 코인 → **{r['won']:,}원** · 잔액 {r['balance']:,}원" if r["ok"] else
                f"코인이 모자라요 (가진 코인 {r.get('have', 0):,})." if r["reason"] == "coins" else "1 코인 이상 적어 주세요.")
        await self.cog.show(interaction, self.fid, note)


# ───────────── 인수 (명문구단처럼: 제안 → 주인이 수락 · 적정 인수 금액 이상이면 바로) ─────────────
def _farm_line(f: dict) -> str:
    return (f"**{F.LANDS[f['land']][1]}** · 땅 {f['plots']}칸 · 코인 {f['coins']:,} · 낫 +{f['sickle']}"
            + (f" · 동물 {f['animals']}마리" if f["animals"] else ""))


def _won(raw) -> Optional[int]:
    raw = str(raw).replace(",", "").replace("원", "").strip()
    return int(raw) if raw.isdigit() else None


_OFFER_FAIL = {"mine": "이미 내 농장이에요.", "amount": "제안 금액은 1원 이상이어야 해요.",
               "gone": "없어졌거나 만료된 제안이에요."}


def _offer_fail(r: dict) -> str:
    if r["reason"] == "balance":
        return f"제안 금액 **{r.get('cost', r.get('amount', 0)):,}원**만큼 잔액이 있어야 해요."
    return _OFFER_FAIL.get(r["reason"], "인수할 수 없어요.")


class OfferModal(discord.ui.Modal):
    """인수 금액 제안 — 적정 인수 금액 이상이면 바로 인수."""

    def __init__(self, cog: "Farm", user, f: dict, panel):
        super().__init__(title="농장 인수 제안")
        self.cog, self.user, self.f, self.panel = cog, user, f, panel
        self.amount = discord.ui.TextInput(label="제안 금액 (원)", max_length=15,
                                           placeholder=f"적정 인수 금액 {f['fair']:,}원 이상이면 바로 인수")
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        amount = _won(self.amount.value)
        if amount is None:
            return await interaction.response.send_message("제안 금액은 숫자로 적어 주세요.", ephemeral=True)
        await self.cog.make_offer(interaction, self.user, self.f["id"], amount, self.panel)


class OfferStart(discord.ui.View):
    """다른 사람 농장: 적정 인수 금액으로 바로 인수하거나, 금액을 제안한다 (누른 사람에게만 보인다)."""

    def __init__(self, cog: "Farm", user, f: dict, panel):
        super().__init__(timeout=180)
        self.cog, self.user, self.f, self.panel = cog, user, f, panel
        now = discord.ui.Button(label=f"{f['fair']:,}원에 바로 인수"[:80], style=discord.ButtonStyle.success)
        now.callback = self._now
        self.add_item(now)
        propose = discord.ui.Button(label="금액 제안", style=discord.ButtonStyle.primary)
        propose.callback = self._propose
        self.add_item(propose)

    def embed(self, f: dict, balance: int) -> discord.Embed:
        return ui.card("농장 인수 제안", (
            f"<@{f['owner']}>님의 농장 · {_farm_line(f)}\n\n"
            f"적정 인수 금액 **{f['fair']:,}원** — 이 금액 이상이면 바로 인수돼요.\n"
            "그보다 적은 금액은 주인이 수락해야 인수돼요.\n"
            f"인수하면 코인 · 창고 · 밭 · 동물 · 낫 · 상자가 모두 넘어와요.\n-# 내 잔액 {balance:,}원"), ui.INFO, self.user, SECTION)

    async def _now(self, interaction: discord.Interaction):
        self.stop()
        await self.cog.make_offer(interaction, self.user, self.f["id"], self.f["fair"], self.panel, edit=True)

    async def _propose(self, interaction: discord.Interaction):
        await interaction.response.send_modal(OfferModal(self.cog, self.user, self.f, self.panel))


class AskModal(discord.ui.Modal):
    """주인이 적정 인수 금액을 정한다 — 이 금액 이상 제안이 오면 바로 인수된다."""

    def __init__(self, cog: "Farm", user, f: dict):
        super().__init__(title="적정 인수 금액 정하기")
        self.cog, self.user, self.fid = cog, user, f["id"]
        lo, hi = F.farm_ask_range(f["worth"])
        self.amount = discord.ui.TextInput(label="적정 인수 금액 (원)", default=str(f["fair"]), max_length=15,
                                           placeholder=f"{lo:,} ~ {hi:,}"[:100])
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        amount = _won(self.amount.value)
        if amount is None:
            return await interaction.response.send_message("금액은 숫자로 적어 주세요.", ephemeral=True)
        r = await self.cog.db.set_farm_ask(self.user.id, self.fid, amount, int(time.time()))
        if r["ok"]:
            e = ui.card("적정 인수 금액", f"**{r['ask']:,}원**으로 정했어요.\n"
                        "이 금액 이상을 제안하면 바로 인수되고, 그보다 적은 제안은 수락 · 거절할 수 있어요.", ui.WIN, self.user, SECTION)
        else:
            e = ui.card("적정 인수 금액", "내 농장이 아니에요." if r["reason"] == "none" else
                        f"**{r['lo']:,}원 ~ {r['hi']:,}원** 사이로 정해 주세요.", ui.LOSE, self.user, SECTION)
        await interaction.response.send_message(embed=e, ephemeral=True)


class OwnerOffers(discord.ui.View):
    """내 농장에 들어온 인수 제안 — 골라서 [수락] · [거절], 적정 인수 금액 정하기 (주인에게만 보인다)."""

    def __init__(self, cog: "Farm", user, f: dict, offers: list[dict], panel):
        super().__init__(timeout=300)
        self.cog, self.user, self.f, self.offers, self.panel = cog, user, f, offers, panel
        self.pick: Optional[int] = offers[0]["id"] if offers else None
        if offers:
            sel = discord.ui.Select(placeholder="받은 인수 제안 고르기", row=0, options=[
                discord.SelectOption(label=f"{o['amount']:,}원 — {cog.name_of(user, o['buyer_id'])}"[:100], value=str(o["id"]),
                                     default=o["id"] == self.pick) for o in offers[:25]])
            sel.callback = self._picked
            self.add_item(sel)
            for label, style, ok in (("수락", discord.ButtonStyle.success, True), ("거절", discord.ButtonStyle.danger, False)):
                b = discord.ui.Button(label=label, style=style, row=1)
                b.callback = self._answer(ok)
                self.add_item(b)
        ask = discord.ui.Button(label="적정 인수 금액 정하기", style=discord.ButtonStyle.primary, row=2)
        ask.callback = self._ask
        self.add_item(ask)

    def embed(self) -> discord.Embed:
        f = self.f
        lo, hi = F.farm_ask_range(f["worth"])
        rows = "\n".join(f"<@{o['buyer_id']}> **{o['amount']:,}원** · <t:{o['ts'] + F.FARM_OFFER_HOURS * 3600}:R> 만료"
                         for o in self.offers[:25]) or "받은 인수 제안이 없어요."
        return ui.card("받은 인수 제안", (
            f"{rows}\n\n적정 인수 금액 **{f['fair']:,}원** — 이 금액 이상 제안이 오면 바로 인수돼요.\n"
            f"-# 적정 인수 금액은 {lo:,}원 ~ {hi:,}원 사이로 정할 수 있어요 · 수락하면 농장이 통째로 넘어가요"),
            ui.INFO, self.user, SECTION)

    async def _picked(self, interaction: discord.Interaction):
        self.pick = int(interaction.data["values"][0])
        await interaction.response.defer()

    def _answer(self, accept: bool):
        async def cb(interaction: discord.Interaction):
            self.stop()
            await self.cog.answer_offer(interaction, self.user, self.pick, accept, self.panel)
        return cb

    async def _ask(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AskModal(self.cog, self.user, self.f))


# ───────────── 농사 상점 (서버마다 스레드 하나 — 봇이 재시작해도 눌리는 버튼) ─────────────
def shop_embed() -> discord.Embed:
    seeds = "\n".join(f"{n} — {price:,} 코인 · {_dur(sec)} · 팔면 {sell} 코인"
                      for _, n, price, sec, sell in F.CROPS.values() if price)
    pets = "\n".join(f"{n} — {price:,} 코인 · {_dur(sec)}마다 {F.PRODUCTS[prod][1]} ({F.PRODUCTS[prod][2]} 코인)"
                     for _, n, price, prod, sec in F.ANIMALS.values())
    return ui.card(SHOP_NAME, (
        "씨앗과 동물을 농장 코인으로 사요. 아래에서 살 것을 누르면 돼요.\n"
        "산 것은 내 농장의 **심기 · 입양** 메뉴에서 써요.\n\n"
        f"**씨앗**\n{seeds}\n\n**동물** (땅 {F.ANIMAL_PER_PLOTS}칸당 1마리)\n{pets}\n\n"
        "-# 당근은 씨앗 없이 바로 심을 수 있어요 · 걸리는 시간은 땅에 따라 조금 달라요 · 농장은 `/농사`로 열어요"), ui.INFO, None, SECTION)


class ShopPanel(discord.ui.View):
    def __init__(self, cog: "Farm"):
        super().__init__(timeout=None)
        self.cog = cog
        for row, keys in enumerate(([k for k, v in F.CROPS.items() if v[2]], list(F.ANIMALS))):
            for k in keys:
                b = discord.ui.Button(custom_id=f"farm:shop:{k}", label=f"{_name(k)} · {F.shop_price(k):,} 코인",
                                      style=discord.ButtonStyle.success if row == 0 else discord.ButtonStyle.primary, row=row)
                b.callback = self._pick(k)
                self.add_item(b)

    def _pick(self, key: str):
        async def cb(interaction: discord.Interaction):
            if not release.preview_381(interaction.guild_id):
                return await interaction.response.send_message("농사 상점은 아직 이 서버에서 쓸 수 없어요.", ephemeral=True)
            farms = await self.cog.db.farms_of(interaction.user.id, int(time.time()))
            if not farms:
                return await interaction.response.send_message("먼저 `/농사`로 농장을 열어 주세요.", ephemeral=True)
            view = BuyView(self.cog, interaction.user, farms, key)
            await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)
        return cb


class BuyView(discord.ui.View):
    """농사 상점에서 고른 씨앗 · 동물을 내 농장 코인으로 산다 (누른 사람에게만 보인다)."""

    def __init__(self, cog: "Farm", user, farms: list[dict], key: str):
        super().__init__(timeout=180)
        self.cog, self.user, self.farms, self.key, self.note = cog, user, farms, key, ""
        self.fid = farms[0]["id"]
        self.unit = "마리" if key in F.ANIMALS else "개"
        self._draw()

    @property
    def farm(self) -> dict:
        return next((f for f in self.farms if f["id"] == self.fid), self.farms[0])

    def _draw(self):
        self.clear_items()
        f, price = self.farm, F.shop_price(self.key)
        most = f["coins"] // price
        for n in ((1,) if self.key in F.ANIMALS else (1, 5, 10)):
            b = discord.ui.Button(label=f"{n}{self.unit} ({price * n:,} 코인)", style=discord.ButtonStyle.success,
                                  disabled=most < n, row=0)
            b.callback = self._buy(n)
            self.add_item(b)
        if self.key in F.CROPS:
            b = discord.ui.Button(label=f"살 수 있는 만큼 ({most:,}개)" if most else "살 수 있는 만큼",
                                  style=discord.ButtonStyle.primary, disabled=most < 1, row=0)
            b.callback = self._buy(0)
            self.add_item(b)
        if len(self.farms) > 1:
            sel = discord.ui.Select(placeholder="어느 농장의 코인으로 살지 고르기", row=1, options=[
                discord.SelectOption(label=f"{F.LANDS[x['land']][1]} · 땅 {x['plots']}칸 · 코인 {x['coins']:,}"[:100],
                                     value=str(x["id"]), default=x["id"] == self.fid) for x in self.farms[:25]])
            sel.callback = self._farm
            self.add_item(sel)

    def embed(self) -> discord.Embed:
        f, price = self.farm, F.shop_price(self.key)
        return ui.card(f"{SHOP_NAME} · {_name(self.key)}", (f"{self.note}\n\n" if self.note else "") + (
            f"하나에 **{price:,} 코인**\n"
            f"{F.LANDS[f['land']][1]} 농장 · 코인 **{f['coins']:,}** · 사 둔 {_name(self.key)} "
            f"**{f['seeds'].get(self.key, 0)}{self.unit}**\n"
            "-# 산 것은 내 농장의 심기 · 입양 메뉴에서 써요 (농장 화면에서 새로고침을 누르면 보여요)"), ui.INFO, self.user, SECTION)

    async def _redraw(self, interaction: discord.Interaction, note: str = ""):
        self.farms = await self.cog.db.farms_of(self.user.id, int(time.time())) or self.farms
        self.note = note
        self._draw()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    def _buy(self, n: int):
        async def cb(interaction: discord.Interaction):
            now = int(time.time())
            count = n
            if not count:   # 살 수 있는 만큼
                f = await self.cog.db.get(self.fid, now)
                count = (f["coins"] // F.shop_price(self.key)) if f else 0
            r = await self.cog.db.buy(self.user.id, self.fid, self.key, count, now)
            if r["ok"]:
                note = f"**{_name(self.key)} {r['count']:,}{self.unit}**를 샀어요 (코인 -{r['cost']:,})"
            else:
                note = {"coins": f"코인이 모자라요 ({r.get('cost', 0):,} 코인이 필요해요).", "amount": "코인이 모자라요.",
                        "none": "내 농장이 아니에요."}.get(r["reason"], "살 수 없어요.")
            await self._redraw(interaction, note)
        return cb

    async def _farm(self, interaction: discord.Interaction):
        self.fid = int(interaction.data["values"][0])
        await self._redraw(interaction)


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
        if r.get("id"):
            await self.cog.open(interaction, self.user, r["id"])


class FarmPick(discord.ui.View):
    """농장이 여러 개일 때: 열 농장 고르기."""

    def __init__(self, cog: "Farm", user, farms: list[dict]):
        super().__init__(timeout=180)
        self.cog, self.user = cog, user
        sel = discord.ui.Select(placeholder="열 농장 고르기", options=[
            discord.SelectOption(label=f"{F.LANDS[f['land']][1]} · 땅 {f['plots']}칸 · 코인 {f['coins']:,}"[:100], value=str(f["id"]))
            for f in farms[:25]])
        sel.callback = self._picked
        self.add_item(sel)

    async def _picked(self, interaction: discord.Interaction):
        await self.cog.open(interaction, self.user, int(interaction.data["values"][0]))


class Farm(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = F.FarmDB()
        self.panel = FarmPanel(self)
        bot.add_view(self.panel)   # 재시작 전에 올라간 농장 화면의 버튼도 받는다
        self.shop = ShopPanel(self)
        bot.add_view(self.shop)    # 농사 상점 버튼도
        self._shop_lock = asyncio.Lock()

    async def cog_load(self):
        SHOPS.clear()
        SHOPS.update(await self.db.shop_threads())
        self.shop_loop.start()

    def cog_unload(self):
        self.panel.stop()
        self.shop.stop()
        self.shop_loop.cancel()

    # ───────────── 농사 상점 스레드 ─────────────
    @tasks.loop(minutes=5)
    async def shop_loop(self):
        """/농사 가 열린 서버(공개 전엔 테스트 서버, 공개되면 모든 서버)에 농사 상점 스레드가 없으면 만든다."""
        for guild in list(self.bot.guilds):
            if not release.preview_381(guild.id):
                continue
            try:
                await self.ensure_shop(guild)
            except Exception as ex:   # 한 서버가 안 돼도 나머지는 계속
                print(f"[농사 상점] {guild.id} 오류: {type(ex).__name__}: {ex}")

    @shop_loop.before_loop
    async def _before_shop(self):
        await self.bot.wait_until_ready()

    async def _shop_parents(self, guild, near=None) -> list:
        """상점 스레드를 만들 채널 후보: 패치노트가 가는 채널 (패치노트가 스레드로 가는 서버는 그 스레드의 채널) → near."""
        out = []
        tid = PATCH_THREADS.get(guild.id)
        if tid:
            th = guild.get_channel_or_thread(tid)
            if th is None:
                try:
                    th = await guild.fetch_channel(tid)
                except discord.HTTPException:
                    th = None
            out.append(getattr(th, "parent", None))
        notes = self.bot.get_cog("PatchNotesCog")
        if notes is not None:
            out.append(await asyncio.to_thread(notes._pick_channel, guild))
        out.append(near)
        return [c for n, c in enumerate(out) if isinstance(c, discord.TextChannel) and c not in out[:n]]

    async def ensure_shop(self, guild, near=None):
        """이 서버의 농사 상점 스레드 (없으면 만든다 · 보관됐으면 다시 연다). 만들 수 없으면 None."""
        async with self._shop_lock:
            tid = SHOPS.get(guild.id)
            if tid:
                th = guild.get_thread(tid)
                if th is None:
                    try:
                        th = await guild.fetch_channel(tid)
                    except discord.NotFound:          # 지워진 스레드 → 새로 만든다
                        th = None
                    except discord.HTTPException:     # 잠깐 안 되는 것일 수 있다 — 새로 만들지 않는다
                        return None
                if isinstance(th, discord.Thread):
                    if th.archived:
                        try:
                            await th.edit(archived=False)
                        except discord.HTTPException:
                            pass
                    return th
            for parent in await self._shop_parents(guild, near):
                perms = parent.permissions_for(guild.me)
                if not (perms.view_channel and perms.create_public_threads and perms.send_messages_in_threads):
                    continue
                try:
                    if parent.is_news():   # 공지 채널의 스레드는 글에서만 열 수 있다
                        first = await parent.send(f"**{SHOP_NAME}**이 열렸어요. 씨앗과 동물은 이 글의 스레드에서 사요.")
                        th = await first.create_thread(name=SHOP_NAME, auto_archive_duration=10080, reason=SHOP_NAME)
                    else:
                        th = await parent.create_thread(name=SHOP_NAME, type=discord.ChannelType.public_thread,
                                                        auto_archive_duration=10080, reason=SHOP_NAME)
                    await th.send(embed=shop_embed(), view=ShopPanel(self))
                except discord.HTTPException as ex:
                    print(f"[농사 상점] {guild.id} 스레드 만들기 오류: {type(ex).__name__}: {ex}")
                    continue
                await self.db.set_shop_thread(guild.id, th.id)
                SHOPS[guild.id] = th.id
                print(f"[농사 상점] {guild.id} 스레드 {th.id} 만듦 (#{parent.name})")
                return th
            return None

    async def show(self, interaction: discord.Interaction, fid: int, note: str = ""):
        """누른 농장 화면을 지금 상태로 다시 그린다."""
        now = int(time.time())
        f = await self.db.get(fid, now)
        await interaction.response.edit_message(embed=panel_embed(interaction.user, f, now, note), view=FarmPanel(self, f))

    async def open(self, interaction: discord.Interaction, user, fid: int):
        """내 농장 fid 의 스레드를 연다 (없으면 이 채널에 만든다). 그 스레드 안에서 부르면 농장 화면을 새로 올린다."""
        try:
            await self._open(interaction, user, fid)
        finally:
            guild, ch = interaction.guild, interaction.channel
            if guild.id not in SHOPS:   # 패치노트 채널에 상점을 못 만든 서버 — 농장을 여는 이 채널에라도 만든다
                try:
                    await self.ensure_shop(guild, ch.parent if isinstance(ch, discord.Thread) else ch)
                except Exception as ex:
                    print(f"[농사 상점] {guild.id} 오류: {type(ex).__name__}: {ex}")

    async def _open(self, interaction: discord.Interaction, user, fid: int):
        guild, ch, now = interaction.guild, interaction.channel, int(time.time())
        f = await self.db.get(fid, now)
        tid = await self.db.get_thread(fid, guild.id)
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
                await self.db.set_thread(fid, guild.id, thread.id)
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

    # ───────────── 인수 ─────────────
    def name_of(self, near, uid: int) -> str:
        """uid 의 이름 (near 와 같은 서버의 별명 → 봇이 아는 이름 → id)."""
        guild = getattr(near, "guild", None)
        m = (guild.get_member(uid) if guild else None) or self.bot.get_user(uid)
        return m.display_name if m else str(uid)

    async def _user(self, guild, uid: int):
        m = guild.get_member(uid) if guild else None
        if m is None:
            try:
                m = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
            except discord.HTTPException:
                m = None
        return m

    async def _dm(self, uid: int, e: discord.Embed):
        try:
            user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
            await user.send(embed=e)
        except discord.HTTPException:
            pass

    async def _moved(self, interaction: discord.Interaction, fid: int, owner_id: int, panel, note: str):
        """농장 주인이 바뀐 뒤: 농장 화면을 새 주인 것으로 고치고, 그 농장의 스레드 이름도 새 주인으로."""
        now = int(time.time())
        owner = await self._user(interaction.guild, owner_id)
        if owner is None:
            return
        f = await self.db.get(fid, now)
        try:
            if panel is not None and f:
                await panel.edit(embed=panel_embed(owner, f, now, note), view=FarmPanel(self, f))
        except discord.HTTPException:
            pass
        for tid in await self.db.threads_of(fid):
            th = self.bot.get_channel(tid)
            try:
                if isinstance(th, discord.Thread):
                    await th.edit(name=f"{owner.display_name}의 농장"[:100])
            except discord.HTTPException:
                pass

    async def make_offer(self, interaction: discord.Interaction, user, fid: int, amount: int, panel, edit: bool = False):
        r = await self.db.offer_farm(user.id, fid, amount, int(time.time()))
        place = getattr(interaction.channel, "mention", "농장 스레드")
        if not r["ok"]:
            e = ui.card("인수 제안 실패", _offer_fail(r), ui.LOSE, user, SECTION)
        elif r["auto"]:
            e = ui.card("농장 인수 완료", (
                f"**{r['cost']:,}원**에 인수했어요 (적정 인수 금액 {r['fair']:,}원 이상이라 바로 인수) · 잔액 **{r['balance']:,}원**\n"
                f"전 주인 <@{r['prev_owner']}>님이 인수 금액을 받았어요.\n"
                "이 스레드의 농장이 이제 내 농장이에요. `/농사`로도 열 수 있어요."), ui.GOLD, user, SECTION)
            await self._dm(r["prev_owner"], ui.card("농장이 인수됐어요", (
                f"{user.mention}님이 적정 인수 금액 이상(**{r['cost']:,}원**)을 제안해 {place}의 농장을 바로 인수했어요.\n"
                f"인수 금액 **+{r['cost']:,}원**을 받았어요."), ui.INFO, None, SECTION))
        else:
            e = ui.card("인수 제안을 보냈어요", (
                f"제안 금액 **{r['amount']:,}원** (적정 인수 금액 {r['fair']:,}원)\n"
                f"주인 <@{r['owner_id']}>님이 수락하면 인수돼요. 제안은 <t:{r['expires']}:R> 만료돼요.\n"
                "-# 돈은 수락될 때 빠져나가요 · 그때 잔액이 모자라면 인수되지 않아요"), ui.INFO, user, SECTION)
            await self._dm(r["owner_id"], ui.card("농장 인수 제안", (
                f"{user.mention}님이 {place}의 농장을 **{r['amount']:,}원**에 인수하고 싶어 해요.\n"
                f"농장 화면의 **받은 인수 제안**에서 수락하거나 거절할 수 있어요 (<t:{r['expires']}:R> 만료)."),
                ui.INFO, None, SECTION))
        if edit:
            await interaction.response.edit_message(embed=e, view=None)
        else:
            await interaction.response.send_message(embed=e, ephemeral=True)
        if r["ok"] and r["auto"]:
            await self._moved(interaction, fid, user.id, panel, f"{user.mention}님이 이 농장을 인수했어요.")

    async def answer_offer(self, interaction: discord.Interaction, user, offer_id: Optional[int], accept: bool, panel):
        r = await self.db.answer_farm_offer(user.id, offer_id or 0, accept, int(time.time()))
        place = getattr(interaction.channel, "mention", "농장 스레드")
        if r["ok"] and r["accepted"]:
            e = ui.card("농장을 넘겼어요", (
                f"<@{r['buyer_id']}>님의 제안을 수락했어요. 받은 돈 **+{r['amount']:,}원** · 잔액 **{r['balance']:,}원**\n"
                "`/농사`로 새 농장을 열 수 있어요."), ui.WIN, user, SECTION)
            await self._dm(r["buyer_id"], ui.card("농장 인수 완료", (
                f"주인이 **{r['amount']:,}원** 제안을 수락했어요. {place}의 농장이 이제 내 농장이에요.\n"
                "`/농사`로도 열 수 있어요."), ui.GOLD, None, SECTION))
        elif r["ok"]:
            e = ui.card("인수 제안 거절", f"<@{r['buyer_id']}>님의 **{r['amount']:,}원** 제안을 거절했어요.", ui.EVEN, user, SECTION)
            await self._dm(r["buyer_id"], ui.card("농장 인수 제안 거절", (
                f"{place}의 농장 주인이 **{r['amount']:,}원** 제안을 거절했어요."), ui.EVEN, None, SECTION))
        else:
            msg = (_OFFER_FAIL["gone"] if r["reason"] == "gone" else
                   "제안한 사람이 지금 인수할 수 없어서 제안이 닫혔어요. (" + _offer_fail(r) + ")")
            e = ui.card("처리할 수 없어요", msg, ui.LOSE, user, SECTION)
        await interaction.response.edit_message(embed=e, view=None)
        if r["ok"] and r["accepted"]:
            await self._moved(interaction, r["id"], r["buyer_id"], panel, f"<@{r['buyer_id']}>님이 이 농장을 인수했어요.")

    @app_commands.command(name="농사", description="내 농장 — 땅을 골라 작물과 동물을 기르고, 합치고, 넓혀요")
    async def farm(self, interaction: discord.Interaction):
        if not release.preview_381(interaction.guild_id):
            return await interaction.response.send_message("/농사 는 아직 이 서버에서 쓸 수 없어요.", ephemeral=True)
        if interaction.guild is None:
            return await interaction.response.send_message("/농사 는 서버 채널에서 써 주세요.", ephemeral=True)
        user = interaction.user
        farms = await self.db.farms_of(user.id, int(time.time()))
        if not farms:
            return await interaction.response.send_message(embed=land_embed(user), view=LandPick(self, user), ephemeral=True)
        here = await self.db.thread_farm(interaction.channel_id)     # 내 농장 스레드 안이면 그 농장
        if len(farms) == 1 or here in [f["id"] for f in farms]:
            return await self.open(interaction, user, here if here in [f["id"] for f in farms] else farms[0]["id"])
        lines = "\n".join(f"{n}. {_farm_line(f)}" for n, f in enumerate(farms, 1))
        await interaction.response.send_message(
            embed=ui.card("내 농장", f"{lines}\n\n열 농장을 골라 주세요.", ui.INFO, user, SECTION),
            view=FarmPick(self, user, farms), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Farm(bot))
