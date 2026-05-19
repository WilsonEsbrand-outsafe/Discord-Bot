# cogs/tutorial.py
import discord
from discord import app_commands
from discord.ext import commands


TUTORIAL_STEPS = [
    {
        "title": "튜토리얼 1 / 4 · 구단 만들기",
        "content": (
            "이 서버에는 **구단 시스템**이 있습니다.\n\n"
            "먼저 구단을 만들어야 모든 콘텐츠를 이용할 수 있습니다.\n\n"
            "**지금 할 일**\n"
            "`/구단생성`\n\n"
            "입력하면 자동으로 **내 닉네임 FC** 구단이 생성되고\n"
            "🎁 **50,000원 보너스**를 받습니다."
        ),
    },
    {
        "title": "튜토리얼 2 / 4 · 돈 받기",
        "content": (
            "선수팩과 시장 거래에는 돈이 필요합니다.\n\n"
            "**하루 1번 무료 보상**을 받을 수 있습니다.\n\n"
            "**지금 할 일**\n"
            "`/출석`\n\n"
            "하루에 한 번 꼭 받아두세요."
        ),
    },
    {
        "title": "튜토리얼 3 / 4 · 선수 얻기",
        "content": (
            "선수는 **선수팩**으로 얻습니다.\n\n"
            "팩에는 여러 등급이 있으며,\n"
            "비쌀수록 좋은 선수가 나올 확률이 높습니다.\n\n"
            "**지금 할 일**\n"
            "`/선수팩 브론즈`\n\n"
            "처음에는 브론즈팩으로 충분합니다."
        ),
    },
    {
        "title": "튜토리얼 4 / 4 · 선수 시장",
        "content": (
            "이 서버의 핵심은 **선수 시장**입니다.\n\n"
            "- 선수 가격은 **10분마다 변동**됩니다\n"
            "- 시장 이용 시간: **09:00 ~ 23:00**\n\n"
            "**자주 쓰는 명령어**\n"
            "`/시장`\n"
            "`/선수검색`\n"
            "`/구매` `/판매`\n\n"
            "싸게 사서 비싸게 파는 것도 가능합니다."
        ),
    },
]


class TutorialView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.step = 0

    def make_embed(self, user: discord.User) -> discord.Embed:
        data = TUTORIAL_STEPS[self.step]
        e = discord.Embed(
            title=data["title"],
            description=data["content"],
            color=0x2ecc71,
        )
        e.set_author(name=user.display_name, icon_url=user.display_avatar.url)
        return e

    @discord.ui.button(label="◀ 이전", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.step > 0:
            self.step -= 1
        await interaction.response.edit_message(embed=self.make_embed(interaction.user), view=self)

    @discord.ui.button(label="다음 ▶", style=discord.ButtonStyle.primary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.step < len(TUTORIAL_STEPS) - 1:
            self.step += 1
            await interaction.response.edit_message(embed=self.make_embed(interaction.user), view=self)
        else:
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="튜토리얼 완료",
                    description=(
                        "튜토리얼이 끝났습니다.\n\n"
                        "이제 자유롭게 플레이하시면 됩니다.\n\n"
                        "추천 시작:\n"
                        "`/구단생성`\n"
                        "`/출석`\n"
                        "`/선수팩 브론즈`"
                    ),
                    color=0x95a5a6,
                ),
                view=None,
            )


class Tutorial(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="튜토리얼", description="신규 유저용 단계별 튜토리얼을 진행합니다.")
    async def tutorial(self, interaction: discord.Interaction):
        view = TutorialView()
        await interaction.response.send_message(
            embed=view.make_embed(interaction.user),
            view=view,
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Tutorial(bot))
