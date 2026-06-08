# bot.py
import os
import asyncio
import time
from pathlib import Path
import re
from typing import List, Optional
from datetime import datetime, timezone
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from auth import OWNER_ID, owner_only, owner_only_error

# ───────────────── 설정 ─────────────────
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN이 .env에 없습니다.")

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ───────────────── 공통 유틸 ─────────────────
MENTION_RE = re.compile(r"<@&?\d+>|<@!\d+>|<@\d+>")
FAN_ROLE_NAME = "축구 팬"

def _parse_hex_color(c: str) -> discord.Colour:
    if not c:
        return discord.Colour.default()
    c = c.strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", c):
        return discord.Colour.default()
    return discord.Colour(int(c, 16))

def resolve_mention_text(guild: Optional[discord.Guild], raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return s
    if MENTION_RE.fullmatch(s):
        return s
    name = s.lstrip("@").strip()
    if guild:
        role = discord.utils.find(lambda r: r.name.lower() == name.lower(), guild.roles)
        if role:
            return role.mention
        member = discord.utils.find(
            lambda m: (m.display_name and m.display_name.lower() == name.lower()) or
                      (m.name and m.name.lower() == name.lower()),
            guild.members,
        )
        if member:
            return member.mention
    return s

async def _send_transfer_embed(
    interaction: discord.Interaction,
    *,
    target: Optional[discord.TextChannel],
    player: str,
    from_team: str,
    to_team: str,
    image_url: str,
    details_pipe: str,
    source_url: str,
    title_prefix: str,
    color: int,
):
    dest = target or interaction.channel
    if not dest:
        return await interaction.response.send_message("채널을 찾을 수 없어요.", ephemeral=True)

    from_disp = resolve_mention_text(interaction.guild, from_team)
    to_disp   = resolve_mention_text(interaction.guild, to_team)

    fan_role = discord.utils.find(lambda r: r.name == FAN_ROLE_NAME, interaction.guild.roles) if interaction.guild else None
    fan_mention = fan_role.mention if fan_role else "@축구 팬"

    details = [seg.strip() for seg in (details_pipe or "").split("|") if seg.strip()]
    lines = [
        f"{from_disp} → {to_disp}",
        "",
        title_prefix + f" {fan_mention}",
        *(f"• {d}" for d in details),
        f"[출처]({source_url})",
    ]

    embed = discord.Embed(title=f"{player}", description="\n".join(lines), color=color)
    embed.set_image(url=image_url)

    try:
        await dest.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions(everyone=False, users=True, roles=True, replied_user=False),
        )
    except discord.Forbidden:
        return await interaction.followup.send(
            "❌ 전송 채널 권한 부족: `View Channel`, `Send Messages`, `Embed Links` 권한을 확인해 주세요.",
            ephemeral=True,
        )
    await interaction.followup.send("✅ 전송했습니다.", ephemeral=True)

# ───────────────── 슬래시: 청소 ─────────────────
@app_commands.guild_only()
@app_commands.check(owner_only)
@app_commands.default_permissions(manage_messages=True)
@bot.tree.command(name="청소", description="지정한 개수만큼 최근 메시지를 삭제합니다.")
@app_commands.describe(count="삭제할 메시지 개수 (1~100)")
async def clean_messages(interaction: discord.Interaction, count: int):
    await interaction.response.defer(ephemeral=True)

    if count < 1 or count > 100:
        return await interaction.followup.send("❌ 삭제 개수는 1~100 사이여야 합니다.", ephemeral=True)

    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        return await interaction.followup.send("❌ 텍스트 채널에서만 사용할 수 있습니다.", ephemeral=True)

    try:
        deleted = await channel.purge(limit=count)
    except discord.Forbidden:
        return await interaction.followup.send(
            "❌ 봇에게 **메시지 관리(Manage Messages)** 권한이 없습니다.",
            ephemeral=True
        )

    await interaction.followup.send(f"✅ 메시지 {len(deleted)}개를 삭제했습니다.", ephemeral=True)

# ───────────────── 슬래시: 임베드 3종 (무하이픈만 유지) ─────────────────
@app_commands.guild_only()
@app_commands.check(owner_only)
@bot.tree.command(name="임베드hwg", description="HWG 임베드를 전송합니다.")
@app_commands.describe(target="보낼 채널(선택). 비우면 현재 채널", player="선수", from_team="출발 팀",
                       to_team="도착 팀", image_url="이미지 URL", details="항목1|항목2|...", source_url="출처 URL")
async def embed_hwg(interaction: discord.Interaction, target: Optional[discord.TextChannel],
                    player: str, from_team: str, to_team: str, image_url: str, details: str, source_url: str):
    await interaction.response.defer(ephemeral=True)
    await _send_transfer_embed(
        interaction,
        target=target,
        player=player,
        from_team=from_team,
        to_team=to_team,
        image_url=image_url,
        details_pipe=details,
        source_url=source_url,
        title_prefix="**Here We Go!**",
        color=0x1E90FF,
    )

@app_commands.guild_only()
@app_commands.check(owner_only)
@bot.tree.command(name="임베드오피셜", description="완료된 이적 임베드를 전송합니다.")
@app_commands.describe(target="보낼 채널(선택). 비우면 현재 채널", player="선수", from_team="출발 팀",
                       to_team="도착 팀", image_url="이미지 URL", details="항목1|항목2|...", source_url="출처 URL")
async def embed_official(interaction: discord.Interaction, target: Optional[discord.TextChannel],
                         player: str, from_team: str, to_team: str, image_url: str, details: str, source_url: str):
    await interaction.response.defer(ephemeral=True)
    await _send_transfer_embed(
        interaction,
        target=target,
        player=player,
        from_team=from_team,
        to_team=to_team,
        image_url=image_url,
        details_pipe=details,
        source_url=source_url,
        title_prefix="**🤝 OFFICIAL!**",
        color=0xFFD700,
    )

@app_commands.guild_only()
@app_commands.check(owner_only)
@bot.tree.command(name="임베드속보", description="속보(브레이킹) 스타일 이적 임베드를 전송합니다.")
@app_commands.describe(target="보낼 채널(선택). 비우면 현재 채널", player="선수", from_team="출발 팀",
                       to_team="도착 팀", image_url="이미지 URL", details="항목1|항목2|...", source_url="출처 URL")
async def embed_breaking(interaction: discord.Interaction, target: Optional[discord.TextChannel],
                         player: str, from_team: str, to_team: str, image_url: str, details: str, source_url: str):
    await interaction.response.defer(ephemeral=True)
    await _send_transfer_embed(
        interaction,
        target=target,
        player=player,
        from_team=from_team,
        to_team=to_team,
        image_url=image_url,
        details_pipe=details,
        source_url=source_url,
        title_prefix="**🚨 속보**",
        color=0xFF3B30,
    )

# ───────────────── 글로벌 중복 제거 (디스코드에 남은 글로벌 명령어 삭제) ─────────────────
@app_commands.default_permissions(administrator=True)
@app_commands.check(owner_only)
@bot.tree.command(name="글로벌초기화", description="디스코드에 남아있는 글로벌(전체) 슬래시 명령어를 전부 삭제합니다.")
async def purge_global(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    # 글로벌(전체) 명령어를 빈 목록으로 동기화 → 디스코드에서 삭제됨
    bot.tree.clear_commands(guild=None)
    await bot.tree.sync()

    await interaction.followup.send(
        "✅ 글로벌(전체) 슬래시 명령어 삭제 완료.\n"
        "이제 봇을 재시작한 뒤, /동기화 를 한 번 실행하세요.",
        ephemeral=True,
    )

# ───────────────── 모든 길드 자동 동기화 + 수동 동기화(리로드 포함) ─────────────────
async def sync_guild(guild: discord.abc.Snowflake):
    gid = getattr(guild, "id", guild)
    try:
        bot.tree.clear_commands(guild=guild)
        bot.tree.copy_global_to(guild=guild)

        # ✅ 타임아웃(예: 25초) 걸어서 무한 대기 방지
        synced = await asyncio.wait_for(bot.tree.sync(guild=guild), timeout=60)
        print(f"🔗 Synced {len(synced)} cmds to guild {gid}")

    except asyncio.TimeoutError:
        print(f"⚠️ Sync timeout for guild {gid} (60s). 잠시 후 /동기화 로 재시도 권장")

    except Exception as e:
        print(f"❌ Sync failed for guild {gid}:", repr(e))

@bot.event
async def on_ready():
    print(f"🤖 로그인 성공: {bot.user} (ID: {bot.user.id})")

    # 모든 코그를 순회하며 로드하도록 수정
    EXTENSIONS = ("cogs.fixtures", "cogs.economy", "cogs.toto", "cogs.players_market", "cogs.club", "cogs.tutorial", "cogs.patch_notes", "cogs.trade", "cogs.notify")
    for ext in EXTENSIONS:
        try:
            await bot.load_extension(ext)
            print(f"🧩 {ext} 로드 완료")
        except Exception as e:
            print(f"⚠️ {ext} 로드 실패:", repr(e))

    # ✅ 길드 동기화 순서 고정 (1 → 4) : ID 리스트 그대로 순회
    ORDERED_GUILD_IDS = [
        1374213619793006704,  # 1
        757761125403066419,   # 2
        1408836309971243251,  # 3
    ]

    for gid in ORDERED_GUILD_IDS:
        await sync_guild(discord.Object(id=gid))
        await asyncio.sleep(1.5)
    
    # ───────────────── 시장/월 처리 루프 시작 ─────────────────
    # 중복 실행 방지
    if not hasattr(bot, "_pm_loops_started"):
        bot._pm_loops_started = True

        from services.player_market_db import PlayerMarketDB

        pm = PlayerMarketDB()

        async def tick_loop():
            while True:
                try:
                    now = int(time.time())
                    await pm.ensure_bootstrap(now)
                    await pm.run_tick_if_due(now)
                except Exception as e:
                    print("❌ [PM] tick_loop error:", repr(e))
                await asyncio.sleep(5)

        async def month_loop():
            from services.economy_db import EconomyDB
            from services.notifier import send_notify
            _db = EconomyDB()
            while True:
                try:
                    now = int(time.time())
                    await pm.ensure_bootstrap(now)
                    due, user_events = await pm.run_month_if_due(now)
                    if due > 0:
                        print(f"🗓️ [PM] 월 진행: +{due}개월")
                    # 성장/은퇴 DM 발송
                    for uid, evts in user_events.items():
                        for evt in evts:
                            if evt["event"] == "growth":
                                event_key = "선수_성장"
                                em = discord.Embed(
                                    title="📈 선수 OVR 성장",
                                    description=(
                                        f"**{evt['name']}**의 능력치가 성장했습니다!\n"
                                        f"OVR **{evt['ovr_before']}** → **{evt['ovr_after']}**"
                                    ),
                                    color=0x3498db,
                                )
                            else:  # retire
                                event_key = "선수_은퇴"
                                em = discord.Embed(
                                    title="💀 선수 은퇴",
                                    description=f"**{evt['name']}**이(가) 은퇴했습니다.",
                                    color=0x95a5a6,
                                )
                            await send_notify(bot, _db, uid, event_key, em)
                except Exception as e:
                    print("❌ [PM] month_loop error:", repr(e))
                await asyncio.sleep(10)

        asyncio.create_task(tick_loop())
        asyncio.create_task(month_loop())
        print("✅ [PM] 시장 틱/월 진행 루프 시작")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: Exception):
    # 원래 에러는 로그로 남기기
    import traceback
    traceback.print_exception(type(error), error, error.__traceback__)

    msg = f"❌ 오류가 발생했습니다: {type(error).__name__}"

    try:
        # 이미 defer/응답이 끝난 상태면 followup으로 보내야 함
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        # interaction 자체가 만료(Unknown interaction)된 경우 등은 조용히 종료
        pass

@bot.event
async def on_guild_join(guild: discord.Guild):
    await sync_guild(guild)

@app_commands.default_permissions(administrator=True)
@app_commands.check(owner_only)
@bot.tree.command(name="동기화", description="(관리자) 코그 리로드 + 이 서버 슬래시 명령어 동기화를 한 번에 수행합니다.")
async def sync_and_reload(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("서버에서만 사용 가능합니다.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    # 리로드 대상 목록에 전체 추가
    EXTENSIONS = ("cogs.fixtures", "cogs.economy", "cogs.toto", "cogs.players_market", "cogs.club", "cogs.tutorial", "cogs.patch_notes", "cogs.trade", "cogs.notify")
    for ext in EXTENSIONS:
        try:
            await bot.reload_extension(ext)
        except commands.ExtensionNotLoaded:
            try:
                await bot.load_extension(ext)
            except Exception as e:
                print(f"⚠️ {ext} 로드 실패:", repr(e))
        except Exception as e:
            print(f"⚠️ {ext} 리로드 실패:", repr(e))

    # ✅ 슬래시 동기화(이 서버)
    await sync_guild(discord.Object(id=interaction.guild_id))

    await interaction.followup.send("🔄 코그 리로드 + 동기화 완료", ephemeral=True)

# ───────────────── 슬래시: 올인원 임베드 ─────────────────
@app_commands.guild_only()
@app_commands.check(owner_only)
@bot.tree.command(name="임베드전체", description="임베드")
@app_commands.describe(
    title="임베드 제목",
    description="임베드 내용(줄바꿈 가능)",
    url="임베드 제목에 하이퍼링크를 걸 URL",
    color="임베드 색상 (예: #3498db 또는 3498db)",
    author_name="작성자 이름",
    author_icon="작성자 아이콘 URL",
    thumbnail="썸네일 이미지 URL",
    image="본문 이미지 URL",
    footer="푸터(맨 아래 작은 글씨)",
    footer_icon="푸터 아이콘 URL",
    fields="필드들: 제목||내용|제목||내용 … 형식 (예: 이름||홍길동|나이||20)",
)
async def embed_full(
    interaction: discord.Interaction,
    title: str,
    description: str = "",
    url: str = "",
    color: str = "",
    author_name: str = "",
    author_icon: str = "",
    thumbnail: str = "",
    image: str = "",
    footer: str = "",
    footer_icon: str = "",
    fields: str = "",
):
    try:
        await interaction.response.defer(ephemeral=True)

        c = (color or "").lstrip("#").strip()
        if len(c) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in c):
            color_value = int(c, 16)
        else:
            color_value = 0x2ecc71

        embed = discord.Embed(title=title, description=description or None, color=color_value)
        if url:
            embed.url = url

        if author_name:
            if author_icon:
                embed.set_author(name=author_name, icon_url=author_icon)
            else:
                embed.set_author(name=author_name)

        if thumbnail:
            embed.set_thumbnail(url=thumbnail)

        if image:
            embed.set_image(url=image)

        if footer:
            if footer_icon:
                embed.set_footer(text=footer, icon_url=footer_icon)
            else:
                embed.set_footer(text=footer)

        if fields:
            try:
                raw_segments = [seg.strip() for seg in fields.split("|") if seg.strip()]
                for i in range(0, len(raw_segments), 2):
                    name = raw_segments[i]
                    value = raw_segments[i + 1] if i + 1 < len(raw_segments) else ""
                    if not name and not value:
                        continue
                    embed.add_field(name=name or "제목 없음", value=value or "\u200b", inline=False)
            except Exception as fe:
                print("fields 파싱 오류:", repr(fe))

        dest = interaction.channel
        if dest is None:
            return await interaction.followup.send("채널을 찾을 수 없습니다.", ephemeral=True)

        await dest.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions(everyone=False, users=True, roles=True, replied_user=False),
        )
        await interaction.followup.send("✅ 임베드를 전송했습니다.", ephemeral=True)

    except Exception as e:
        print("embed_full 오류:", repr(e))
        try:
            await interaction.followup.send("임베드를 만드는 중 오류가 발생했습니다.", ephemeral=True)
        except Exception:
            pass

# ───────────────── 슬래시: 겨울결산 (페이지 넘김) ─────────────────
RECAP_TXT = BASE_DIR / "transfer_recap_2026w.txt"

def _chunk_for_embed(lines: List[str], limit: int = 3200) -> List[str]:
    pages: List[str] = []
    buf: List[str] = []
    size = 0
    for line in lines:
        if not line.strip():
            continue
        add_len = len(line) + 1
        if buf and size + add_len > limit:
            pages.append("\n".join(buf))
            buf = [line]
            size = add_len
        else:
            buf.append(line)
            size += add_len
    if buf:
        pages.append("\n".join(buf))
    return pages or ["(내용이 없습니다.)"]

def _load_recap_lines() -> List[str]:
    if not RECAP_TXT.exists():
        return []
    text = RECAP_TXT.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in text if ln.strip() and not ln.strip().startswith("#")]

class RecapPager(discord.ui.View):
    def __init__(self, pages: List[str]):
        super().__init__(timeout=None)
        self.pages = pages
        self.i = 0
        self._sync_buttons()

    def _sync_buttons(self):
        self.prev_btn.disabled = self.i <= 0
        self.next_btn.disabled = self.i >= len(self.pages) - 1

    def _embed(self) -> discord.Embed:
        return discord.Embed(
            title="2026년 1월 프리미어리그(EPL) 관련 겨울 이적시장 결산",
            description=f"{self.pages[self.i]}\n\n페이지 {self.i+1}/{len(self.pages)}",
            color=0x2ecc71,
        )

    async def _update(self, interaction: discord.Interaction):
        self._sync_buttons()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="◀ 이전", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.i > 0:
            self.i -= 1
        await self._update(interaction)

    @discord.ui.button(label="다음 ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.i < len(self.pages) - 1:
            self.i += 1
        await self._update(interaction)

async def _send_winter_recap(interaction: discord.Interaction):
    lines = _load_recap_lines()
    if not lines:
        return await interaction.response.send_message(
            "❌ transfer_recap_2026w.txt 파일이 없거나 비어 있습니다.",
            ephemeral=True
        )

    pages = _chunk_for_embed(lines)
    view = RecapPager(pages)
    await interaction.response.send_message(embed=view._embed(), view=view)

@app_commands.guild_only()
@app_commands.check(owner_only)
@bot.tree.command(name="winterrecap", description="2026년 1월 겨울 이적시장 결산")
async def winter_recap_en(interaction: discord.Interaction):
    await _send_winter_recap(interaction)

@winter_recap_en.error
async def winter_recap_en_error(interaction: discord.Interaction, error: Exception):
    await owner_only_error(interaction, error)

@app_commands.guild_only()
@app_commands.check(owner_only)
@bot.tree.command(name="겨울결산", description="2026년 1월 겨울 이적시장 결산")
async def winter_recap_kr(interaction: discord.Interaction):
    await _send_winter_recap(interaction)

@winter_recap_kr.error
async def winter_recap_kr_error(interaction: discord.Interaction, error: Exception):
    await owner_only_error(interaction, error)

# ───────────────── 슬래시: 공지 임베드 ─────────────────
@app_commands.guild_only()
@app_commands.check(owner_only)
@bot.tree.command(name="임베드공지", description="임베드 공지를 전송합니다.")
@app_commands.describe(
    target="보낼 채널(선택). 비우면 현재 채널",
    mention="멘션(선택): 역할/유저/텍스트 (예: @축구 팬, @홍길동, <@123...>)",
    title="공지 제목",
    content="공지 내용(줄바꿈 가능)",
    color="색상(선택): #3498db 또는 3498db",
    thumbnail="썸네일 이미지 URL(선택)",
    image="본문 이미지 URL(선택)",
    footer="푸터(선택)",
)
async def announce_embed(
    interaction: discord.Interaction,
    target: Optional[discord.TextChannel] = None,
    mention: str = "",
    title: str = "📢 공지",
    content: str = "",
    color: str = "",
    thumbnail: str = "",
    image: str = "",
    footer: str = "",
):
    await interaction.response.defer(ephemeral=True)

    dest = target or interaction.channel
    if not isinstance(dest, discord.TextChannel):
        return await interaction.followup.send("❌ 텍스트 채널에서만 전송할 수 있습니다.", ephemeral=True)

    # 색상 파싱
    c = (color or "").strip().lstrip("#")
    if len(c) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in c):
        color_value = int(c, 16)
    else:
        color_value = 0x2ecc71

    content = content.replace("\\n", "\n")

    # 임베드 구성
    embed = discord.Embed(
        title=title or "공지",
        description=content or None,
        color=color_value,
        timestamp=datetime.now(timezone.utc),
    )
    # 서버 아이콘 있으면 넣고, 없으면 icon_url 자체를 안 넣음(버전 호환)
    author_kwargs = {"name": "공지"}
    if interaction.guild and interaction.guild.icon:
        author_kwargs["icon_url"] = interaction.guild.icon.url
    embed.set_author(**author_kwargs)


    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if image:
        embed.set_image(url=image)

    if footer:
        embed.set_footer(text=footer)
    else:
        embed.set_footer(text=f"작성: {interaction.user.display_name}")

    # 멘션 처리(역할/유저 이름도 자동 변환)
    mention_text = resolve_mention_text(interaction.guild, mention) if mention else ""

    try:
        await dest.send(
            content=mention_text if mention_text else None,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, users=True, roles=True, replied_user=False
            ),
        )
    except discord.Forbidden:
        return await interaction.followup.send(
            "❌ 전송 채널 권한 부족: `View Channel`, `Send Messages`, `Embed Links` 권한을 확인해 주세요.",
            ephemeral=True,
        )

    await interaction.followup.send("✅ 공지를 전송했습니다.", ephemeral=True)


@announce_embed.error
async def announce_embed_error(interaction: discord.Interaction, error: Exception):
    await owner_only_error(interaction, error)

# ───────────────── 슬래시: 선수 풀 초기화 ─────────────────
@app_commands.guild_only()
@app_commands.check(owner_only)
@bot.tree.command(name="선수풀초기화", description="[관리자] 시스템 선수 풀을 초기화하고 새로 스폰합니다. (유저 보유 선수 유지)")
async def reset_player_pool(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    from services.player_market_db import PlayerMarketDB
    pm = PlayerMarketDB()
    now = int(time.time())
    deleted, spawned = await pm.reset_system_pool(now)
    pool_counts = await pm.count_pack_pool()

    count_lines = "\n".join(
        f"  • {name}팩: **{cnt}명**" for name, cnt in pool_counts.items()
    )
    await interaction.followup.send(
        f"✅ **선수 풀 초기화 완료**\n"
        f"- 삭제된 선수: **{deleted:,}명**\n"
        f"- 새로 활성화된 선수: **{spawned:,}명**\n"
        f"(유저 보유 선수 및 아마추어 더미는 유지됩니다)\n\n"
        f"**팩별 선수 수**\n{count_lines}",
        ephemeral=True,
    )

@reset_player_pool.error
async def reset_player_pool_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    await owner_only_error(interaction, error)

# ───────────────── 슬래시: 가격 범위 재계산 ─────────────────
@app_commands.guild_only()
@app_commands.check(owner_only)
@bot.tree.command(name="가격범위재계산", description="[관리자] 모든 선수의 floor/ceil을 현재 공식으로 즉시 재계산합니다.")
async def recalc_price_ranges(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    from services.player_market_db import PlayerMarketDB
    pm = PlayerMarketDB()
    count = await pm.recalculate_price_ranges(int(time.time()))

    await interaction.followup.send(
        f"✅ **가격 범위 재계산 완료**\n"
        f"- 업데이트된 선수: **{count:,}명**\n"
        f"- 새 범위: 기준가 × 0.70 ~ × 1.55",
        ephemeral=True,
    )

@recalc_price_ranges.error
async def recalc_price_ranges_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    await owner_only_error(interaction, error)

# ───────────────── /명령어 자동 생성(드롭다운) ─────────────────
def _is_owner_only_command(cmd: app_commands.Command) -> bool:
    # owner_only 체크가 걸린 명령어는 제외
    for chk in getattr(cmd, "checks", []):
        if getattr(chk, "__name__", "") == "owner_only":
            return True
    return False

HIDDEN_COMMANDS: set[str] = {"명령어", "겨울결산", "winterrecap"}

CATEGORY_ORDER: list[str] = [
    "📅 경기 정보",
    "💰 경제",
    "🎰 토토",
    "⚽ 선수 & 이적시장",
    "🤝 트레이드",
    "🏟️ 클럽",
    "🧩 기타",
]

def _guess_category_from_module(cmd: app_commands.Command) -> str:
    cb = getattr(cmd, "callback", None)
    mod = getattr(cb, "__module__", "") if cb else ""

    if "fixtures" in mod:
        return "📅 경기 정보"
    if "economy" in mod:
        return "💰 경제"
    if "toto" in mod:
        return "🎰 토토"
    if "players_market" in mod:
        return "⚽ 선수 & 이적시장"
    if "trade" in mod:
        return "🤝 트레이드"
    if "club" in mod:
        return "🏟️ 클럽"
    return "🧩 기타"

def _flatten_commands(cmds: list[app_commands.Command]) -> list[tuple[str, str, app_commands.Command]]:
    out: list[tuple[str, str, app_commands.Command]] = []

    for c in cmds:
        if isinstance(c, app_commands.Group):
            group_name = c.name
            for sc in c.commands:
                if isinstance(sc, app_commands.Group):
                    for ssc in sc.commands:
                        out.append((f"/{group_name} {sc.name} {ssc.name}", (ssc.description or ""), ssc))
                else:
                    out.append((f"/{group_name} {sc.name}", (sc.description or ""), sc))
        else:
            out.append((f"/{c.name}", (c.description or ""), c))

    return out

def build_user_help_embeds(bot: commands.Bot) -> list[discord.Embed]:
    raw_cmds = bot.tree.get_commands()
    flat = _flatten_commands(raw_cmds)

    filtered = [
        (n, d, c) for (n, d, c) in flat
        if not _is_owner_only_command(c) and c.name not in HIDDEN_COMMANDS
    ]

    buckets: dict[str, list[tuple[str, str]]] = {}
    for name, desc, cmd in filtered:
        cat = _guess_category_from_module(cmd)
        buckets.setdefault(cat, []).append((name, desc))

    for cat in buckets:
        buckets[cat].sort(key=lambda x: x[0])

    embeds: list[discord.Embed] = []
    for cat in CATEGORY_ORDER:
        if cat not in buckets:
            continue

        lines = []
        for name, desc in buckets[cat]:
            if desc:
                lines.append(f"`{name}` — {desc}")
            else:
                lines.append(f"`{name}`")

        e = discord.Embed(
            title=cat,
            description="\n".join(lines) if lines else "표시할 명령어가 없습니다.",
            color=0x2ecc71,
        )
        embeds.append(e)

    if not embeds:
        embeds.append(discord.Embed(title="명령어", description="표시할 명령어가 없습니다.", color=0x2ecc71))

    return embeds

class UserHelpDropdown(discord.ui.Select):
    def __init__(self, embeds: list[discord.Embed]):
        self.embeds = embeds
        options = []
        for i, e in enumerate(embeds):
            options.append(
                discord.SelectOption(
                    label=e.title[:100] if e.title else f"페이지 {i+1}",
                    value=str(i),
                    description="이 페이지로 이동",
                )
            )
        super().__init__(placeholder="페이지 선택", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        idx = int(self.values[0])
        await interaction.response.edit_message(embed=self.embeds[idx], view=self.view)

class UserHelpView(discord.ui.View):
    def __init__(self, embeds: list[discord.Embed]):
        super().__init__(timeout=300)
        self.embeds = embeds
        self.add_item(UserHelpDropdown(embeds))

@bot.tree.command(name="명령어", description="일반 유저용 축구 봇 명령어 안내(드롭다운)")
async def user_help(interaction: discord.Interaction):
    embeds = build_user_help_embeds(bot)
    view = UserHelpView(embeds)

    if not interaction.channel or not isinstance(interaction.channel, discord.abc.Messageable):
        return await interaction.response.send_message("❌ 이 채널에서는 사용할 수 없습니다.", ephemeral=True)

    # 채널에 안내 1개만 전송
    await interaction.channel.send(embed=embeds[0], view=view)

    # 실행한 사람에게만 확인 메시지
    await interaction.response.send_message("✅ 안내 메시지를 채널에 전송했습니다.", ephemeral=True)

# ───────────────── 실행부 ─────────────────
async def main():
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 종료: Ctrl+C")
