# cogs/burning.py — 3.9.1 /버닝: 수능 기념 버닝 이벤트 효과 + 버닝 미션 (플레이 시간 · 출석 · 낚시 · 농사) 보상 받기.
# 규칙 · 미션은 services/burning.py. 플레이 시간은 bot.py 가 상호작용마다 burning.touch 하고 여기서 1분마다 저장한다.
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from services import burning as B
from services import ui
from services.economy_db import ITEMS, EconomyDB

SECTION = "수능 버닝"


def _kst(ts: int) -> str:
    t = time.gmtime(int(ts) + 9 * 3600)
    return f"{t.tm_mon}월 {t.tm_mday}일 {t.tm_hour:02d}:{t.tm_min:02d}"


def _items(items: dict) -> str:
    """{'lawyer': 1} → '변호사 선임권' (2개 이상이면 '변호사 선임권 2개')."""
    return ", ".join(ITEMS[k][1] + (f" {q}개" if q > 1 else "") for k, q in items.items())


def burning_embed(user, rows: list[dict], now: float, note: str = "") -> discord.Embed:
    if now < B.START:
        when = f"{_kst(B.START)}에 시작해요"
    elif now < B.END:
        when = f"{_kst(B.END)}까지 (<t:{B.END}:R> 끝)"
    else:
        when = "이벤트가 끝났어요. 다 채운 미션 보상은 사흘 동안 받을 수 있어요"
    e = ui.card(B.NAME, when + (f"\n\n{note}" if note else ""), ui.GOLD, user, SECTION)
    e.add_field(name="버닝 효과", value="\n".join(B.TEXT), inline=False)
    lines = []
    for m in rows:
        state = "받음" if m["claimed"] else ("받을 수 있어요" if m["done"] else f"{m['value']:,}/{m['goal']:,}{B.UNIT[m['what']]}")
        lines.append(f"**{m['name']}** {_items(m['items'])}\n`{ui.bar(m['value'], m['goal'])}` {state}")
    e.add_field(name="버닝 미션", value="\n".join(lines), inline=False)
    e.set_footer(text="플레이 시간은 봇을 쓴 시간이에요 (1분 안에 한 번이라도 쓰면 1분). 물고기는 자동 낚시도 세요.")
    return e


class BurningView(discord.ui.View):
    def __init__(self, cog: "Burning", user, rows: list[dict]):
        super().__init__(timeout=600)
        self.cog, self.user = cog, user
        ready = sum(1 for m in rows if m["done"] and not m["claimed"])
        b = discord.ui.Button(label=f"보상 받기 ({ready}개)" if ready else "받을 보상 없음",
                              style=discord.ButtonStyle.success, disabled=not ready)
        b.callback = self._claim
        self.add_item(b)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("`/버닝` 으로 내 화면을 열어 주세요.", ephemeral=True)
            return False
        return True

    async def _claim(self, interaction: discord.Interaction):
        now = time.time()
        r = await self.cog.db._tx(lambda con: B.claim(con, self.user.id, now))
        rows = await self.cog.rows(self.user.id)
        note = (f"미션 {len(r['claimed'])}개 보상으로 **{_items(r['items'])}** 을(를) 받았어요. `/가방` 에 넣었어요."
                if r["claimed"] else "지금 받을 보상이 없어요.")
        await interaction.response.edit_message(embed=burning_embed(self.user, rows, now, note),
                                                view=BurningView(self.cog, self.user, rows))


class Burning(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = EconomyDB()
        self.flush.start()

    def cog_unload(self):
        self.flush.cancel()

    async def rows(self, uid: int) -> list[dict]:
        return await self.db._tx(lambda con: B.status(con, uid))

    @tasks.loop(seconds=60)
    async def flush(self):
        pending = B.take_pending()
        if pending:
            now = time.time()
            await self.db._tx(lambda con: B.add_play(con, pending, now))

    @flush.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="버닝", description="수능 기념 버닝 이벤트: 효과와 미션 보상")
    async def burning(self, interaction: discord.Interaction):
        now = time.time()
        rows = await self.rows(interaction.user.id)
        note = "" if B.can_claim(interaction.user.id, now) or now >= B.END + B.CLAIM_GRACE else "보상은 이벤트가 열리면 받을 수 있어요."
        await interaction.response.send_message(embed=burning_embed(interaction.user, rows, now, note),
                                                view=BurningView(self, interaction.user, rows))


async def setup(bot: commands.Bot):
    await bot.add_cog(Burning(bot))
