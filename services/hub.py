# services/hub.py — 2.8 허브 화면 공통 틀: 한 메시지를 계속 고쳐 쓰고, 맨 아래 줄은 언제나 ◀ 🏠 ▶.
# 🏠 = 목차(드롭다운으로 바로 이동). 페이지는 Page(이모지, 제목, render) — render(view) 가 임베드를 돌려주고
# 필요하면 view.add_item(...) 으로 그 페이지 전용 버튼 · 메뉴를 0~3번 줄에 붙인다.
# 버튼이 만료되면(10분) 화면 아래에 "다시 열어 주세요" 안내를 남긴다.
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import discord

from services import ui

NAV_ROW = 4
TIMEOUT = 600


@dataclass
class Page:
    emoji: str
    title: str
    render: Callable[["HubView"], Awaitable[discord.Embed]]
    hint: str = ""          # 목차에 보이는 한 줄 설명


class HubView(discord.ui.View):
    """여러 화면을 ◀ ▶ 로 넘기고 🏠 로 목차에 돌아가는 화면. 연 사람만 누를 수 있다."""

    def __init__(self, user, title: str, section: str, pages: list[Page], command: str, page: int = -1,
                 home_note: str = "", ctx: Optional[dict] = None):
        super().__init__(timeout=TIMEOUT)
        self.user, self.title, self.section, self.pages, self.command = user, title, section, pages, command
        self.page = page                  # -1 = 목차
        self.home_note = home_note
        self.ctx = ctx if ctx is not None else {}   # 페이지끼리 나눠 쓰는 값 (cog · db 등)
        self.message: Optional[discord.Message] = None
        self._inter: Optional[discord.Interaction] = None

    # ── 그리기 ──
    async def build(self) -> discord.Embed:
        self.clear_items()
        if self.page < 0:
            e = self._home()
        else:
            p = self.pages[self.page]
            e = await p.render(self)
            e.set_footer(text=f"{self.title} · {self.page + 1}/{len(self.pages)} {p.emoji} {p.title} · ◀ ▶ 넘기기 · 🏠 목차")
        self._nav()
        return e

    def _home(self) -> discord.Embed:
        lines = [f"{p.emoji} **{p.title}**" + (f" — {p.hint}" if p.hint else "") for p in self.pages]
        e = ui.card(f"🏠 {self.title}", (self.home_note + "\n\n" if self.home_note else "") + "\n".join(lines),
                    ui.INFO, self.user, self.section)
        e.set_footer(text="아래 메뉴에서 바로 이동하거나 ▶ 로 처음부터 넘겨 보세요")
        menu = discord.ui.Select(placeholder="📚 목차 — 원하는 화면으로 바로 이동", row=0, options=[
            discord.SelectOption(label=p.title, value=str(i), emoji=p.emoji, description=p.hint[:100] or None)
            for i, p in enumerate(self.pages[:25])])

        async def jump(interaction: discord.Interaction):
            await self.go(interaction, int(menu.values[0]))
        menu.callback = jump
        self.add_item(menu)
        return e

    def _nav(self):
        n = len(self.pages)
        for emoji, step, label in (("◀", -1, "이전"), ("🏠", None, "목차"), ("▶", 1, "다음")):
            b = discord.ui.Button(emoji=emoji, label=label, row=NAV_ROW,
                                  style=discord.ButtonStyle.primary if step is None else discord.ButtonStyle.secondary)
            if step is None:
                b.disabled = self.page < 0
                target = -1
            else:
                target = (self.page + step) % n if self.page >= 0 else (0 if step > 0 else n - 1)
            b.callback = self._goto(target)
            self.add_item(b)

    def _goto(self, target: int):
        async def cb(interaction: discord.Interaction):
            await self.go(interaction, target)
        return cb

    async def go(self, interaction: discord.Interaction, page: int, note: Optional[discord.Embed] = None):
        """그 페이지로 이 메시지를 고쳐 쓴다. note 를 주면 페이지 위에 결과 카드를 하나 더 붙인다."""
        self.page = page
        self._inter = interaction
        e = await self.build()
        embeds = [note, e] if note else [e]
        if interaction.response.is_done():
            await interaction.edit_original_response(embeds=embeds, view=self)
        else:
            await interaction.response.edit_message(embeds=embeds, view=self)

    async def refresh(self, interaction: discord.Interaction, note: Optional[discord.Embed] = None):
        await self.go(interaction, self.page, note)

    async def send(self, interaction: discord.Interaction, ephemeral: bool = True):
        self._inter = interaction
        e = await self.build()
        await interaction.response.send_message(embed=e, view=self, ephemeral=ephemeral)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(f"🙅 화면을 연 사람만 누를 수 있어요. `{self.command}`을 직접 열어 주세요.",
                                                    ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self._inter is None:
            return
        try:
            msg = await self._inter.original_response()
            embeds = list(msg.embeds)
            if embeds:
                embeds[-1].set_footer(text=f"⌛ 버튼이 만료됐어요. {self.command}을 다시 열어 주세요.")
            await self._inter.edit_original_response(embeds=embeds, view=None)
        except discord.HTTPException:
            pass
