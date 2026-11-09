# cogs/farm.py — 🌱 /농사 (3.8.1, 테스트 서버 먼저): 씨앗 심기 → 시간이 지나면 [수확] → 돈
import time

import discord
from discord import app_commands
from discord.ext import commands

import release
from services import ui
from services.farm import CROPS, PLOTS, FarmDB

SECTION = "🌱 농사"


def _left(sec: int) -> str:
    sec = max(0, int(sec))
    h, m = divmod((sec + 59) // 60, 60)
    return f"{h}시간 {m}분" if h else f"{m}분"


def field_card(user, f: dict, now: int, note: str = "") -> discord.Embed:
    rows = []
    for i, p in enumerate(f["plots"]):
        if p is None:
            rows.append(f"`{i + 1}` 🟫 빈 밭")
            continue
        emoji, name = CROPS[p["crop"]][:2]
        rows.append(f"`{i + 1}` {emoji} **{name}** — 수확할 수 있어요!" if p["ready"]
                    else f"`{i + 1}` 🌱 {name} — {_left(p['ready_ts'] - now)} 남음 (<t:{p['ready_ts']}:t>)")
    crops = "\n".join(f"{e} {n} · 씨앗 {price:,}원 · {_left(t)} · 수확 **{pay:,}원**"
                      for e, n, price, t, pay in CROPS.values())
    e = ui.card("🌾 내 밭", (f"{note}\n\n" if note else "") + "\n".join(rows)
                + f"\n\n**작물**\n{crops}\n\n`잔액` **{f['balance']:,}원**", ui.WIN, user, SECTION)
    e.set_footer(text="아래에서 작물을 고르면 빈 밭에 심어요 · 다 자라면 [수확]")
    return e


class FarmView(discord.ui.View):
    def __init__(self, cog: "Farm", user, f: dict):
        super().__init__(timeout=300)
        self.cog, self.user, self.all = cog, user, False
        self._draw(f)

    def _draw(self, f: dict):
        self.clear_items()
        empty = sum(p is None for p in f["plots"])
        ready = sum(bool(p and p["ready"]) for p in f["plots"])
        sel = discord.ui.Select(placeholder=f"🌱 심을 작물 고르기 ({'빈 밭 모두' if self.all else '한 칸'}에)",
                                disabled=not empty, row=0, options=[
                                    discord.SelectOption(label=f"{n} · 씨앗 {price:,}원", value=k, emoji=e,
                                                         description=f"{_left(t)} 뒤 {pay:,}원")
                                    for k, (e, n, price, t, pay) in CROPS.items()])
        sel.callback = self._plant
        self.add_item(sel)
        mode = discord.ui.Button(label="빈 밭 모두에 심기" if not self.all else "한 칸만 심기", emoji="🌾",
                                 style=discord.ButtonStyle.secondary, row=1)
        mode.callback = self._mode
        self.add_item(mode)
        hv = discord.ui.Button(label=f"수확 ({ready})", emoji="🧺", style=discord.ButtonStyle.success,
                               disabled=not ready, row=1)
        hv.callback = self._harvest
        self.add_item(hv)
        rf = discord.ui.Button(label="새로고침", emoji="🔄", style=discord.ButtonStyle.secondary, row=1)
        rf.callback = self._refresh
        self.add_item(rf)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("🙅 명령어를 쓴 사람만 누를 수 있어요.", ephemeral=True)
            return False
        return True

    async def _show(self, interaction: discord.Interaction, note: str = ""):
        now = int(time.time())
        f = await self.cog.db.field(self.user.id, now)
        self._draw(f)
        await interaction.response.edit_message(embed=field_card(self.user, f, now, note), view=self)

    async def _plant(self, interaction: discord.Interaction):
        crop = interaction.data["values"][0]
        r = await self.cog.db.plant(self.user.id, crop, int(time.time()), all_empty=self.all)
        e, n = CROPS[crop][:2]
        note = (f"🌱 {e} **{n}** {r['count']}칸을 심었어요 (-{r['cost']:,}원)" if r["ok"] else
                {"full": "🙅 빈 밭이 없어요.", "balance": f"🙅 씨앗 값 {CROPS[crop][2]:,}원이 부족해요.",
                 "crop": "🙅 없는 작물이에요."}[r["reason"]])
        await self._show(interaction, note)

    async def _mode(self, interaction: discord.Interaction):
        self.all = not self.all
        await self._show(interaction)

    async def _harvest(self, interaction: discord.Interaction):
        r = await self.cog.db.harvest(self.user.id, int(time.time()))
        if r["ok"]:
            got = " · ".join(f"{CROPS[c][0]} {pay:,}원" for c, pay in r["crops"])
            note = f"🧺 수확해서 **+{r['total']:,}원**을 받았어요!\n{got}"
        else:
            note = "🙅 아직 다 자란 작물이 없어요."
        await self._show(interaction, note)

    async def _refresh(self, interaction: discord.Interaction):
        await self._show(interaction)


class Farm(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = FarmDB()

    @app_commands.command(name="농사", description=f"🌱 밭 {PLOTS}칸에 씨앗을 심고, 다 자라면 수확해서 팔아요")
    async def farm(self, interaction: discord.Interaction):
        if not release.preview_381(interaction.guild_id):
            return await interaction.response.send_message("🌱 /농사 는 아직 이 서버에서 쓸 수 없어요.", ephemeral=True)
        now = int(time.time())
        f = await self.db.field(interaction.user.id, now)
        await interaction.response.send_message(embed=field_card(interaction.user, f, now),
                                                view=FarmView(self, interaction.user, f))


async def setup(bot: commands.Bot):
    await bot.add_cog(Farm(bot))
