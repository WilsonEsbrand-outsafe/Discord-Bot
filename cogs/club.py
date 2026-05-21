# cogs/club.py
import asyncio
import time
import discord
from discord import app_commands
from discord.ext import commands

from services.club_db import ClubDB
from services.economy_db import EconomyDB
from services.player_market_db import PlayerMarketDB


def _embed(title: str, desc: str, user: discord.abc.User) -> discord.Embed:
    e = discord.Embed(title=title, description=desc, color=0x2ecc71)
    e.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    return e


class Club(commands.Cog):
    BONUS = 50000  # ✅ 구단 생성 보너스(원하시면 숫자만 바꾸면 됨)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.clubs = ClubDB()
        self.money = EconomyDB()
        self.pm = PlayerMarketDB()

    @app_commands.command(name="구단생성", description="내 구단을 생성합니다. (구단명: 내 닉네임 FC)")
    async def create_club(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        now = int(time.time())
        club_name = f"{interaction.user.display_name} FC"

        ok, msg = await self.clubs.create_club(interaction.user.id, club_name, now)
        if not ok:
            return await interaction.followup.send(f"❌ {msg}", ephemeral=True)

        # 보너스 지급 + 아마추어 스쿼드 지급 (동시 실행)
        new_bal, squad_added = await asyncio.gather(
            self.money.add_balance(interaction.user.id, self.BONUS),
            self.pm.give_amateur_squad(interaction.user.id),
        )

        await interaction.followup.send(
            f"✅ {msg}\n"
            f"구단명: **{club_name}**\n"
            f"구단 생성 보너스: **+{self.BONUS:,}원** | 현재 잔액: **{new_bal:,}원**\n\n"
            f"⚽ **아마추어 스쿼드 {squad_added}명 지급 완료!**\n"
            f"GK 2 · DF 5 · MF 6 · FW 5\n"
            f"`/내선수`에서 확인하세요.",
            ephemeral=True,
        )

    @app_commands.command(name="내구단", description="내 구단 정보를 확인합니다.")
    async def my_club(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        row = await self.clubs.get_club(interaction.user.id)
        if not row:
            return await interaction.followup.send("❌ 아직 구단이 없습니다. `/구단생성`을 먼저 해주세요.", ephemeral=True)

        _uid, club_name, created_ts = row
        desc = (
            f"구단명: **{club_name}**\n"
            f"생성일: <t:{int(created_ts)}:f>"
        )
        await interaction.followup.send(embed=_embed("🏟️ 내 구단", desc, interaction.user), ephemeral=True)

    @app_commands.command(name="구단명변경", description="내 구단 이름을 변경합니다. (최대 30자)")
    @app_commands.describe(이름="새 구단 이름")
    async def rename_club(self, interaction: discord.Interaction, 이름: str):
        await interaction.response.defer(ephemeral=True)

        ok, msg = await self.clubs.rename_club(interaction.user.id, 이름.strip())
        if not ok:
            return await interaction.followup.send(f"❌ {msg}", ephemeral=True)

        await interaction.followup.send(
            f"✅ 구단명이 **{이름.strip()}**으로 변경됐습니다.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Club(bot))
