# cogs/notify.py
"""
/알림설정 — DM 알림 ON/OFF 토글 UI
"""
from __future__ import annotations
import discord
from discord import app_commands
from discord.ext import commands

from services.economy_db import EconomyDB
from services.notifier import NOTIFY_EVENTS


class NotifyToggleView(discord.ui.View):
    """알림 항목별 ON/OFF 토글 버튼 UI"""

    def __init__(self, user: discord.abc.User, settings: dict[str, bool], db: EconomyDB):
        super().__init__(timeout=120)
        self.user     = user
        self.settings = settings  # {event_key: bool}
        self.db       = db
        self._build()

    def _build(self):
        self.clear_items()
        for key, label in NOTIFY_EVENTS.items():
            enabled = self.settings.get(key, True)
            btn = discord.ui.Button(
                label=f"{'🔔' if enabled else '🔕'} {label}",
                style=discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary,
                custom_id=key,
                row=list(NOTIFY_EVENTS.keys()).index(key) // 3,
            )
            btn.callback = self._make_cb(key)
            self.add_item(btn)

    def _make_cb(self, key: str):
        async def cb(interaction: discord.Interaction):
            if interaction.user.id != self.user.id:
                return await interaction.response.send_message("본인만 설정할 수 있습니다.", ephemeral=True)
            new_val = not self.settings.get(key, True)
            self.settings[key] = new_val
            await self.db.set_notify(self.user.id, key, new_val)
            self._build()
            await interaction.response.edit_message(embed=self._make_embed(), view=self)
        return cb

    def _make_embed(self) -> discord.Embed:
        lines = []
        for key, label in NOTIFY_EVENTS.items():
            enabled = self.settings.get(key, True)
            lines.append(f"{'🔔' if enabled else '🔕'}  {label}")
        e = discord.Embed(
            title="🔔 DM 알림 설정",
            description="버튼을 눌러 각 알림을 ON/OFF하세요.\n\n" + "\n".join(lines),
            color=0x3498db,
        )
        e.set_footer(text="🔔 = 알림 ON  |  🔕 = 알림 OFF")
        return e


class Notify(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db  = EconomyDB()

    @app_commands.command(name="알림설정", description="DM 알림을 항목별로 ON/OFF 설정합니다.")
    async def notify_settings(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        settings = await self.db.get_all_notify(interaction.user.id)
        view  = NotifyToggleView(interaction.user, settings, self.db)
        await interaction.followup.send(embed=view._make_embed(), view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Notify(bot))
