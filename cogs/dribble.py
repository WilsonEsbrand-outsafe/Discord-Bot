# cogs/dribble.py — ⚽ /드리블 베팅액 (3.1.0, 테스트 서버 먼저)
# 키코가 하프라인부터 솔로 드리블: 수비수마다 [왼쪽] · [오른쪽] 중 하나. 제칠수록 성공 확률(65% → 35%)은 내려가고 배당은 오른다.
# 언제든 [슛!]으로 지금 배당을 받고 끝낼 수 있고, 골키퍼까지 제치면 골 — 140배. 막히면 베팅금을 잃는다.
# 베팅금은 시작할 때 먼저 빼고, 끝날 때 받는 돈(원금 포함)을 넣는다.
import random

import discord
from discord import app_commands
from discord.ext import commands

import release
from services import dribble as D
from services import ui
from services.economy_db import EconomyDB

SECTION = "🎙️ 솔로 드리블 중계"
TIMEOUT = 120   # 이 시간 동안 아무것도 안 누르면 지금 배당으로 자동 슛


def ladder_text(passed: int) -> str:
    """배당 사다리: 제친 곳 ✅ · 지금 앞 ⚽ · 남은 곳."""
    rows = []
    for i, (emoji, name, _) in enumerate(D.STAGES):
        mark = "✅" if i < passed else ("⚽" if i == passed else "▫️")
        cur = " ◀ 지금" if i == passed else ""
        rows.append(f"{mark} {emoji} {name} `{D.ODDS[i]:.0%}` → **{D.MULTS[i]:g}배**{cur}")
    return "\n".join(rows)


def field(passed: int) -> str:
    """하프라인 → 골문 한 줄 그림."""
    cells = ["✅" if i < passed else ("⚽" if i == passed else D.STAGES[i][0]) for i in range(len(D.STAGES))]
    return "⬜ " + " ".join(cells) + " 🥅"


def play_card(user, amount: int, passed: int, caster: str) -> discord.Embed:
    emoji, name, where = D.STAGES[passed]
    have = D.payout(amount, passed) if passed else 0
    e = ui.card(f"⚽ {where} — {emoji} {name}이(가) 막아섭니다!",
                f'> 🎙️ *"{caster}"*\n\n{field(passed)}\n\n'
                f"⬅️ **왼쪽** · **오른쪽** ➡️ — 수비수가 막는 쪽을 피하면 제쳐요 (이번 성공 확률 **{D.ODDS[passed]:.0%}**)\n"
                + (f"🥅 지금 [슛!] 하면 **{have:,}원**을 받아요 ({D.MULTS[passed - 1]:g}배)\n" if passed else "")
                + f"\n{ladder_text(passed)}\n\n`베팅` **{amount:,}원**",
                ui.KIKO, user, SECTION)
    e.set_footer(text=f"{TIMEOUT // 60}분 동안 아무것도 안 누르면 지금 배당으로 자동 슛 · 어디서 멈춰도 기대값은 같아요")
    return e


class DribbleView(discord.ui.View):
    def __init__(self, cog: "Dribble", user, amount: int):
        super().__init__(timeout=TIMEOUT)
        self.cog, self.user, self.amount = cog, user, amount
        self.passed, self.done, self.message = 0, False, None
        self._draw()

    def _draw(self):
        self.clear_items()
        for i, (label, emoji) in enumerate((("왼쪽", "⬅️"), ("오른쪽", "➡️"))):
            b = discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.primary, row=0)
            b.callback = self._go(i)
            self.add_item(b)
        have = D.payout(self.amount, self.passed)
        shot = discord.ui.Button(label=f"슛! · {have:,}원 받기" if self.passed else "슛! (한 명은 제쳐야 해요)",
                                 emoji="🥅", style=discord.ButtonStyle.success, disabled=not self.passed, row=1)
        shot.callback = self._shoot
        self.add_item(shot)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("🙅 공을 가진 사람만 누를 수 있어요.", ephemeral=True)
            return False
        return True

    def _go(self, pick: int):
        async def cb(interaction: discord.Interaction):
            if self.done:
                return await interaction.response.defer()
            emoji, name, _ = D.STAGES[self.passed]
            if D.tackled(self.passed, random):   # 막는 쪽으로 들어갔다 — 태클
                self.done = True
                self.stop()
                e = await self.cog.settle(self.user, self.amount, self.passed, tackled=True,
                                          caster=f"{D.SIDES[pick]}으로 파고들었지만 {name}이(가) 그쪽을 읽었어요! 태클에 걸립니다!")
                return await interaction.response.edit_message(embed=e, view=None)
            self.passed += 1
            if self.passed == len(D.STAGES):   # 골키퍼까지 제쳤다 — 골
                self.done = True
                self.stop()
                e = await self.cog.settle(self.user, self.amount, self.passed,
                                          caster="골키퍼까지 제쳤습니다!! 빈 골문에 밀어 넣습니다 — 골!!! 70m 솔로 골!!!")
                return await interaction.response.edit_message(embed=e, view=None)
            nxt = D.STAGES[self.passed]
            caster = f"{D.SIDES[pick]}으로 치고 나갑니다! {name} 제쳤어요! 이번엔 {nxt[0]} {nxt[1]}!"
            self._draw()
            await interaction.response.edit_message(embed=play_card(self.user, self.amount, self.passed, caster), view=self)
        return cb

    async def _shoot(self, interaction: discord.Interaction):
        if self.done or not self.passed:
            return await interaction.response.defer()
        self.done = True
        self.stop()
        where = D.STAGES[self.passed - 1][1]
        e = await self.cog.settle(self.user, self.amount, self.passed,
                                  caster=f"{where}을(를) 제치고 바로 슈팅!! 골망을 흔듭니다!")
        await interaction.response.edit_message(embed=e, view=None)

    async def on_timeout(self):
        if self.done:
            return
        self.done = True
        caster = ("공을 오래 끌다가 결국 슈팅! 들어갑니다!" if self.passed
                  else "공을 잡고 한참 서 있다가… 경기가 끝났어요. 베팅금은 돌려드려요.")
        e = await self.cog.settle(self.user, self.amount, self.passed, caster=caster)
        if self.message:
            try:
                await self.message.edit(embed=e, view=None)
            except discord.HTTPException:
                pass


class Dribble(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = EconomyDB()

    async def settle(self, user, amount: int, passed: int, caster: str, tackled: bool = False) -> discord.Embed:
        """끝 — 받는 돈을 넣고 결과 화면. tackled 면 0원."""
        got = 0 if tackled else D.payout(amount, passed)
        bal = await self.db.add_balance(user.id, got) if got else await self.db.get_balance(user.id)
        delta = got - amount
        goal = passed == len(D.STAGES)
        if tackled:
            title, color, emoji = f"💥 태클! {passed}명 제치고 멈췄어요", ui.LOSE, "💥"
        elif goal:
            title, color, emoji = "🏆 70m 솔로 골!!!", ui.GOLD, "🏆"
        elif passed:
            title, color, emoji = f"⚽ {passed}명 제치고 슛 — 골!", ui.GOLD if passed >= 4 else ui.WIN, "⚽"
        else:
            title, color, emoji = "😐 드리블을 시작하지 않았어요", ui.EVEN, "😐"
        mult = f"{D.MULTS[passed - 1]:g}배" if passed and not tackled else ("0배" if tackled else "원금")
        e = ui.card(title, f'> 🎙️ *"{caster}"*\n\n{field(passed) if not goal else "✅ " * len(D.STAGES) + "⚽🥅"}\n\n'
                    f"`정산` **{ui.won(delta)}** · 받은 돈 {got:,}원 ({mult})\n`잔액` **{bal:,}원**",
                    color, user, SECTION)
        e.set_thumbnail(url=ui.result_thumb(emoji, color, user))
        return e

    @app_commands.command(name="드리블", description="키코의 솔로 드리블! 수비수마다 왼쪽 · 오른쪽 — 제칠수록 어렵고 배당 ↑ 언제든 슛, 골이면 140배 (최소 1,000원)")
    @app_commands.rename(amount="베팅액")
    @app_commands.describe(amount="베팅 금액 (최소 1,000원)")
    async def dribble(self, interaction: discord.Interaction, amount: app_commands.Range[int, D.MIN_BET]):
        user, amount = interaction.user, int(amount)
        if not release.preview_310(interaction.guild_id):
            return await interaction.response.send_message("⚽ /드리블 은 아직 이 서버에서 쓸 수 없어요.", ephemeral=True)
        await interaction.response.defer()
        bal = await self.db.get_balance(user.id)
        if bal < amount:
            return await interaction.followup.send(embed=ui.card(
                "🙅 경기장에 들어갈 수 없어요", f'> 🎙️ *"잔액이 부족합니다!"*\n\n`베팅` **{amount:,}원**\n`잔액` **{bal:,}원**',
                ui.LOSE, user, SECTION))
        await self.db.add_balance(user.id, -amount)
        view = DribbleView(self, user, amount)
        caster = f"{amount:,}원이 걸린 솔로 드리블! 키코가 하프라인에서 공을 잡습니다. 첫 수비수가 다가와요!"
        try:
            view.message = await interaction.followup.send(embed=play_card(user, amount, 0, caster), view=view, wait=True)
        except discord.HTTPException:
            view.done = True
            await self.db.add_balance(user.id, amount)
            raise


async def setup(bot: commands.Bot):
    await bot.add_cog(Dribble(bot))
