# cogs/admin.py — /관리자명령어: 드롭다운에서 관리자 명령어를 골라 실행한다.
# 관리자(owner_only) 명령어는 동기화 때 / 목록에서 빠져 auth.ADMIN_COMMANDS 로 옮겨진다 (auth.collect_owner_commands).
# 입력값은 모달로 받는다 — 한 장에 5칸까지라 넘치면 여러 장으로 나눈다.
import inspect

import discord
from discord import app_commands
from discord.ext import commands

from auth import ADMIN_COMMANDS, owner_only
from services import ui

SECTION = "🛠️ 관리자"
PAGE = 5                                                  # 모달 한 장의 최대 입력 칸
LONG = {"description", "content", "details", "fields"}   # 여러 줄로 받을 입력
_T = discord.AppCommandOptionType
_SKIP = object()                                          # 비워 둔 선택 입력 → 기본값 사용


def _emoji(cmd) -> str:
    if "임베드" in cmd.name:
        return "📰"
    return {"cogs.economy": "💰", "cogs.toto": "🎰", "cogs.patch_notes": "📢",
            "cogs.players_market": "⚽"}.get(cmd.callback.__module__, "⚙️")


def _field(p) -> discord.ui.Label:
    """명령어 입력값 하나 → 모달 칸. 유저 · 채널 · 선택지는 드롭다운, 나머지는 글자 입력."""
    if p.type is _T.user:
        comp = discord.ui.UserSelect(placeholder="유저 선택")
    elif p.type is _T.channel:
        comp = discord.ui.ChannelSelect(placeholder="채널 선택 (비우면 지금 채널)",
                                        channel_types=[discord.ChannelType.text, discord.ChannelType.news])
    elif p.choices:
        comp = discord.ui.Select(placeholder="선택", options=[
            discord.SelectOption(label=c.name[:100], value=str(c.value)) for c in p.choices[:25]])
    else:
        comp = discord.ui.TextInput(style=discord.TextStyle.paragraph if p.name in LONG else discord.TextStyle.short,
                                    placeholder="숫자" if p.type in (_T.integer, _T.number) else None)
    comp.required = p.required
    hint = p.description if p.description and p.description != "…" else ""
    if not p.required:
        hint = f"(선택{'' if p.default is None else f' · 기본 {p.default}'}) {hint}"
    return discord.ui.Label(text=p.display_name[:45], description=hint.strip()[:100] or None, component=comp)


def _value(p, comp, interaction):
    """모달 칸 → 명령어 인자. 숫자가 아니면 ValueError."""
    if isinstance(comp, discord.ui.TextInput):
        s = comp.value.strip()
        if not s:
            return _SKIP
        if p.type is _T.integer:
            return int(s.replace(",", ""))
        if p.type is _T.number:
            return float(s.replace(",", ""))
        return s
    if not comp.values:
        return _SKIP
    v = comp.values[0]
    if p.type is _T.channel:
        return interaction.guild.get_channel(v.id) or v
    if p.choices:
        choice = next(c for c in p.choices if str(c.value) == v)
        wants_choice = "Choice" in str(inspect.signature(p.command.callback).parameters[p.name].annotation)
        return choice if wants_choice else choice.value
    return v


class Runner:
    """명령어 하나를 실행하기까지: 입력 페이지 → (필요하면 다음 페이지) → 실행."""

    def __init__(self, cmd):
        self.cmd, self.kwargs, self.page = cmd, {}, 0
        self.params = sorted(cmd.parameters, key=lambda p: not p.required)   # 필수 입력이 앞 페이지로
        self.pages = -(-len(self.params) // PAGE)

    def rest(self) -> list:
        return self.params[self.page * PAGE:]

    async def next(self, interaction: discord.Interaction):
        if not self.params:   # 입력이 없는 명령어(초기화 · 동기화 등)는 한 번 더 확인
            e = ui.card(f"⚠️ /{self.cmd.name} 실행할까요?", self.cmd.description, ui.DOOM, interaction.user, SECTION)
            return await interaction.response.send_message(embed=e, view=ConfirmView(self), ephemeral=True)
        await interaction.response.send_modal(ParamModal(self))

    async def run(self, interaction: discord.Interaction):
        try:
            if self.cmd.binding is not None:
                await self.cmd.callback(self.cmd.binding, interaction, **self.kwargs)
            else:
                await self.cmd.callback(interaction, **self.kwargs)
        except Exception as ex:
            msg = f"❌ `/{self.cmd.name}` 실행 중 오류: {type(ex).__name__}: {ex}"[:1900]
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)


class _OwnerView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=600)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await owner_only(interaction)


class ParamModal(discord.ui.Modal):
    def __init__(self, run: Runner):
        page = f" ({run.page + 1}/{run.pages})" if run.pages > 1 else ""
        super().__init__(title=f"/{run.cmd.name}{page}"[:45], timeout=600)
        self.run = run
        self.fields = [(p, _field(p)) for p in run.rest()[:PAGE]]
        for _, label in self.fields:
            self.add_item(label)

    async def on_submit(self, interaction: discord.Interaction):
        for p, label in self.fields:
            try:
                v = _value(p, label.component, interaction)
            except ValueError:
                return await interaction.response.send_message(
                    f"❌ `{p.display_name}` 에는 숫자를 넣어 주세요. `/관리자명령어`에서 다시 골라 주세요.", ephemeral=True)
            if v is not _SKIP:
                self.run.kwargs[p.name] = v
        self.run.page += 1
        rest = self.run.rest()
        if not rest:
            return await self.run.run(interaction)
        # 모달 다음에 바로 모달을 띄울 수 없어서 버튼을 한 번 거친다
        optional = all(not p.required for p in rest)
        e = ui.card(f"📝 /{self.run.cmd.name} — 입력 {self.run.page}/{self.run.pages}",
                    "남은 입력: " + " · ".join(f"`{p.display_name}`" for p in rest)
                    + ("\n남은 칸은 모두 선택이라 바로 실행해도 돼요." if optional else ""), ui.INFO, interaction.user, SECTION)
        await interaction.response.send_message(embed=e, view=NextView(self.run, optional), ephemeral=True)


class NextView(_OwnerView):
    def __init__(self, run: Runner, can_run: bool):
        super().__init__()
        self.run = run
        if not can_run:
            self.remove_item(self.run_now)

    @discord.ui.button(label="다음 입력", emoji="▶️", style=discord.ButtonStyle.primary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.run.next(interaction)

    @discord.ui.button(label="바로 실행", emoji="✅", style=discord.ButtonStyle.success)
    async def run_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await self.run.run(interaction)


class ConfirmView(_OwnerView):
    def __init__(self, run: Runner):
        super().__init__()
        self.run = run

    @discord.ui.button(label="실행", emoji="▶️", style=discord.ButtonStyle.danger)
    async def go(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await self.run.run(interaction)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            embed=ui.card("취소했어요", "아무것도 실행하지 않았어요.", ui.EVEN, interaction.user, SECTION), view=None)


class AdminMenu(_OwnerView):
    """/관리자명령어 — 관리자 명령어 드롭다운."""

    def __init__(self):
        super().__init__()
        cmds = sorted(ADMIN_COMMANDS.values(), key=lambda c: (_emoji(c), c.name))[:25]
        self.menu = discord.ui.Select(placeholder="실행할 관리자 명령어를 고르세요", options=[
            discord.SelectOption(label=f"/{c.name}", value=c.name, emoji=_emoji(c),
                                 description=(c.description or "")[:100] or None) for c in cmds])
        self.menu.callback = self._pick
        self.add_item(self.menu)

    async def _pick(self, interaction: discord.Interaction):
        cmd = ADMIN_COMMANDS.get(self.menu.values[0])
        if cmd is None:
            return await interaction.response.send_message("❌ 명령어를 찾을 수 없어요. `/관리자명령어`를 다시 열어 주세요.",
                                                           ephemeral=True)
        await Runner(cmd).next(interaction)


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # owner_only 체크를 달면 이 명령어까지 메뉴로 옮겨지므로, 확인은 안에서 한다.
    @app_commands.command(name="관리자명령어", description="관리자 명령어를 드롭다운에서 골라 실행합니다 (봇 주인 전용)")
    async def admin(self, interaction: discord.Interaction):
        user = interaction.user
        if not await owner_only(interaction):
            return await interaction.response.send_message(embed=ui.card(
                "🙅 봇 주인만 쓸 수 있어요", "관리자 명령어는 봇 주인만 실행할 수 있어요.", ui.LOSE, user, SECTION), ephemeral=True)
        if not ADMIN_COMMANDS:
            return await interaction.response.send_message("아직 관리자 명령어가 등록되지 않았어요. 봇이 동기화된 뒤 다시 열어 주세요.",
                                                           ephemeral=True)
        e = ui.card("🛠️ 관리자 명령어",
                    "아래 메뉴에서 실행할 명령어를 고르세요.\n"
                    "• 입력이 필요하면 입력창이 떠요 (5칸이 넘으면 여러 장)\n"
                    "• 입력이 없는 명령어(초기화 · 동기화 등)는 한 번 더 확인해요\n\n"
                    f"`등록된 명령어` **{len(ADMIN_COMMANDS)}개**", ui.INFO, user, SECTION)
        await interaction.response.send_message(embed=e, view=AdminMenu(), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
