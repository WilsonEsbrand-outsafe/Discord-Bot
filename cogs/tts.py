# cogs/tts.py — 🔊 /tts 문장: 쓴 사람이 들어가 있는 음성 채널에 봇이 들어와 문장을 읽어 준다 (패치노트에 넣지 않는 기능)
# 쓸 수 있는 서버는 release.TTS_GUILDS (지금은 테스트 서버만). 서버마다 한 번에 한 문장씩 차례대로 읽고,
# IDLE 초 동안 아무도 안 쓰거나 채널에 사람이 없으면 나간다. 서버에 ffmpeg 가 있어야 소리가 난다.
import asyncio
import os
import tempfile

import discord
from discord import app_commands
from discord.ext import commands

import release
from services import tts as T

IDLE = 120   # 마지막으로 읽고 이 시간이 지나면 음성 채널에서 나간다


class TTS(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.locks: dict[int, asyncio.Lock] = {}
        self.idle: dict[int, asyncio.Task] = {}

    def _lock(self, gid: int) -> asyncio.Lock:
        return self.locks.setdefault(gid, asyncio.Lock())

    @app_commands.command(name="tts", description="🔊 내가 들어가 있는 음성 채널에서 봇이 문장을 읽어 줘요")
    @app_commands.describe(문장=f"읽을 문장 (최대 {T.MAX_LEN}자)", 목소리="기본은 선희")
    @app_commands.choices(목소리=[app_commands.Choice(name=n, value=n) for n in T.VOICES])
    async def tts(self, interaction: discord.Interaction, 문장: app_commands.Range[str, 1, T.MAX_LEN],
                  목소리: str = T.DEFAULT_VOICE):
        guild, user = interaction.guild, interaction.user
        if guild is None or guild.id not in release.TTS_GUILDS:
            return await interaction.response.send_message("이 서버에서는 아직 쓸 수 없어요.", ephemeral=True)
        channel = getattr(getattr(user, "voice", None), "channel", None)
        if channel is None:
            return await interaction.response.send_message("🔇 먼저 음성 채널에 들어가 주세요.", ephemeral=True)
        text = T.clean(문장, {m.id: m.display_name for m in guild.members if str(m.id) in 문장})
        if not text:
            return await interaction.response.send_message("읽을 글자가 없어요.", ephemeral=True)
        vc = guild.voice_client
        lock = self._lock(guild.id)
        if vc and vc.is_connected() and vc.channel != channel and lock.locked():
            return await interaction.response.send_message(
                f"🔊 지금 {vc.channel.mention}에서 읽는 중이에요. 잠시 뒤에 다시 써 주세요.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)

        async with lock:
            fd, path = tempfile.mkstemp(suffix=".mp3")
            os.close(fd)
            try:
                try:
                    await T.synthesize(text, 목소리, path)
                except RuntimeError as e:
                    print("⚠️", e)
                    return await interaction.followup.send("😵 목소리를 만들지 못했어요. 잠시 뒤에 다시 써 주세요.", ephemeral=True)
                try:
                    vc = await self._join(guild, channel)
                except Exception as e:
                    print("⚠️ TTS 음성 채널 접속 실패:", repr(e))
                    return await interaction.followup.send("😵 음성 채널에 들어가지 못했어요. 봇 권한(연결 · 말하기)을 확인해 주세요.",
                                                           ephemeral=True)
                await interaction.followup.send(f"🔊 {channel.mention}에서 읽는 중이에요: {text}", ephemeral=True)
                await self._play(vc, path)
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
        self._idle_later(guild)

    async def _join(self, guild: discord.Guild, channel) -> discord.VoiceClient:
        vc = guild.voice_client
        if vc and vc.is_connected():
            if vc.channel != channel:
                await vc.move_to(channel)
            return vc
        if vc:   # 끊긴 채로 남은 연결
            await vc.disconnect(force=True)
        return await channel.connect(timeout=15, self_deaf=True)

    async def _play(self, vc: discord.VoiceClient, path: str) -> None:
        done = asyncio.Event()
        loop = asyncio.get_running_loop()

        def after(err):
            if err:
                print("⚠️ TTS 재생 오류:", repr(err))
            loop.call_soon_threadsafe(done.set)

        vc.play(discord.FFmpegPCMAudio(path), after=after)
        try:
            await asyncio.wait_for(done.wait(), 60)
        except asyncio.TimeoutError:
            vc.stop()

    def _idle_later(self, guild: discord.Guild) -> None:
        old = self.idle.pop(guild.id, None)
        if old:
            old.cancel()

        async def leave():
            await asyncio.sleep(IDLE)
            vc = guild.voice_client
            if vc and not vc.is_playing() and not self._lock(guild.id).locked():
                await vc.disconnect()

        self.idle[guild.id] = asyncio.create_task(leave())

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after):
        """봇만 남으면 바로 나간다."""
        vc = member.guild.voice_client
        if vc and vc.channel and before.channel == vc.channel and not any(not m.bot for m in vc.channel.members):
            await vc.disconnect()

    async def cog_unload(self):
        for t in self.idle.values():
            t.cancel()
        for vc in list(self.bot.voice_clients):
            await vc.disconnect(force=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(TTS(bot))
