# cogs/patch_notes.py
from __future__ import annotations

import asyncio
import datetime
import sqlite3
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from auth import owner_only
from patch_notes_data import PATCH_NOTES

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "economy.sqlite3"


class PatchNotesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._init_db()

    # ─── DB ───
    def _connect(self):
        con = sqlite3.connect(DB_PATH)
        con.execute("PRAGMA journal_mode=WAL;")
        return con

    def _init_db(self):
        con = self._connect()
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS patch_notes (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    version      TEXT    NOT NULL,
                    content      TEXT    NOT NULL DEFAULT '',
                    announced_at INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id            INTEGER PRIMARY KEY,
                    announce_channel_id INTEGER
                )
                """
            )
            con.commit()
        finally:
            con.close()

    def _save_patch(self, version: str, content: str, now_ts: int):
        con = self._connect()
        try:
            con.execute(
                """
                INSERT INTO patch_notes(version, content, announced_at)
                VALUES(?, ?, ?)
                """,
                (version, content, now_ts),
            )
            con.commit()
        finally:
            con.close()

    def _get_announce_channel(self, guild_id: int):
        con = self._connect()
        try:
            row = con.execute(
                "SELECT announce_channel_id FROM guild_settings WHERE guild_id=?",
                (int(guild_id),),
            ).fetchone()
            return int(row[0]) if row else None
        finally:
            con.close()

    def _set_announce_channel(self, guild_id: int, channel_id: int):
        con = self._connect()
        try:
            con.execute(
                """
                INSERT INTO guild_settings(guild_id, announce_channel_id)
                VALUES(?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET announce_channel_id=excluded.announce_channel_id
                """,
                (int(guild_id), int(channel_id)),
            )
            con.commit()
        finally:
            con.close()

    # ─── 유틸 ───
    def _make_embed(self, version: str, content: str, now_ts: int) -> discord.Embed:
        e = discord.Embed(
            title=f"📢 버전 {version} 업데이트",
            description=content,
            color=0x3498db,
        )
        e.set_footer(text=f"v{version} 패치노트")
        e.timestamp = datetime.datetime.fromtimestamp(now_ts, tz=datetime.timezone.utc)
        return e

    def _pick_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        """알림 채널 우선순위: 설정된 채널 → 시스템 채널 → 첫 번째 쓸 수 있는 텍스트 채널"""
        channel_id = self._get_announce_channel(guild.id)
        if channel_id:
            ch = guild.get_channel(channel_id)
            if ch and ch.permissions_for(guild.me).send_messages:
                return ch

        if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
            return guild.system_channel

        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                return ch

        return None

    # ─── 브로드캐스트 ───
    async def broadcast_patch(
        self, interaction: discord.Interaction, version: str, content: str
    ):
        now_ts = int(time.time())
        await asyncio.to_thread(self._save_patch, version, content, now_ts)
        embed = self._make_embed(version, content, now_ts)

        success, fail = 0, 0
        for guild in self.bot.guilds:
            channel = await asyncio.to_thread(self._pick_channel, guild)
            if channel:
                try:
                    await channel.send(embed=embed)
                    success += 1
                except Exception:
                    fail += 1
            else:
                fail += 1

        await interaction.followup.send(
            f"✅ **v{version}** 패치노트 전송 완료!\n"
            f"성공: {success}개 서버 / 실패: {fail}개 서버",
            ephemeral=True,
        )

    # ─── 커맨드 ───
    @app_commands.command(name="패치노트", description="[관리자] 패치노트를 전체 서버에 발송합니다.")
    @app_commands.describe(버전="버전 번호 (예: 1.02)")
    @app_commands.check(owner_only)
    async def patch_note(self, interaction: discord.Interaction, 버전: str):
        await interaction.response.defer(ephemeral=True)

        content = PATCH_NOTES.get(버전)
        if not content:
            available = ", ".join(PATCH_NOTES.keys()) or "없음"
            return await interaction.followup.send(
                f"❌ `{버전}` 버전의 패치노트가 없습니다.\n"
                f"등록된 버전: {available}",
                ephemeral=True,
            )

        await self.broadcast_patch(interaction, 버전, content)

    @patch_note.error
    async def patch_note_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                "❌ 봇 관리자만 사용할 수 있습니다.", ephemeral=True
            )

    @app_commands.command(name="알림채널", description="이 서버의 업데이트 알림을 받을 채널을 설정합니다.")
    @app_commands.describe(채널="알림을 받을 텍스트 채널")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_announce_channel(
        self, interaction: discord.Interaction, 채널: discord.TextChannel
    ):
        await asyncio.to_thread(
            self._set_announce_channel, interaction.guild.id, 채널.id
        )
        await interaction.response.send_message(
            f"✅ 업데이트 알림 채널이 {채널.mention}으로 설정됐습니다.",
            ephemeral=True,
        )

    @set_announce_channel.error
    async def set_announce_channel_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "❌ 서버 관리 권한이 필요합니다.", ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(PatchNotesCog(bot))
