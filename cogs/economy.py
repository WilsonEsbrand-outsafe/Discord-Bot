# cogs/economy.py
import os
import time
import random
import discord
from discord import app_commands
from discord.ext import commands
from auth import owner_only

from services.economy_db import EconomyDB
from services.notifier import send_notify

def _format_time_left(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}시간 {m}분 {s}초"
    if m > 0:
        return f"{m}분 {s}초"
    return f"{s}초"


def _dir_name(code: str) -> str:
    return {"L": "왼쪽", "C": "가운데", "R": "오른쪽"}.get(code, code)


def _embed(title: str, desc: str, user: discord.abc.User) -> discord.Embed:
    e = discord.Embed(title=title, description=desc)
    e.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    return e


class Economy(commands.Cog):
    TRAIN_COOLDOWN = 30
    PENALTY_COOLDOWN = 30

    # ✅ 훈련 이벤트(고정 범위 내에서 수익/손실)
    TRAIN_EVENTS = [
        {
            "name": "지구력 훈련",
            "emoji": "🏃",
            "success_rate": 0.80,
            "win": (2500, 12000),
            "lose": (-3500, -1000),
            "success_text": "호흡이 안정적으로 잡혔습니다.",
            "fail_text": "무리해서 컨디션이 떨어졌습니다.",
        },
        {
            "name": "드리블 훈련",
            "emoji": "🧠",
            "success_rate": 0.80,
            "win": (2500, 12000),
            "lose": (-3500, -1000),
            "success_text": "수비를 깔끔하게 벗겨냈습니다.",
            "fail_text": "볼을 빼앗겼습니다.",
        },
        {
            "name": "페널티킥 훈련",
            "emoji": "🥅",
            "success_rate": 2/3,
            "win": (5000, 15000),
            "lose": (-5000, -2000),
            "success_text": "연습이지만 아주 깔끔한 골입니다.",
            "fail_text": "골키퍼가 읽었습니다.",
        },
        {
            "name": "야구 타격 훈련",
            "emoji": "⚾",
            "success_rate": 2/3,
            "win": (5000, 15000),
            "lose": (-5000, -2000),
            "success_text": "정타! 타이밍이 맞았습니다.",
            "fail_text": "헛스윙… 타이밍이 늦었습니다.",
        },
        {
            "name": "프리킥 훈련",
            "emoji": "🎯",
            "success_rate": 0.40,
            "win": (8000, 20000),
            "lose": (-7500, -3000),
            "success_text": "환상적인 궤적입니다.",
            "fail_text": "벽에 걸렸습니다.",
        },
        {
            "name": "자유투 훈련",
            "emoji": "🏀",
            "success_rate": 2/3,
            "win": (5000, 15000),
            "lose": (-5000, -2000),
            "success_text": "클린! 림에도 안걸렸습니다.",
            "fail_text": "백보드에 맞고 튕겨져 나옵니다.",
        },
        {
            "name": "샌드백 훈련",
            "emoji": "🥊",
            "success_rate": 0.75,
            "win": (3000, 12000),
            "lose": (-4000, -1500),
            "success_text": "묵직한 타격감! 폼이 완벽합니다.",
            "fail_text": "타이밍이 어긋나 손목을 삐끗했습니다.",
        },
        {
            "name": "스파이크 훈련",
            "emoji": "🏐",
            "success_rate": 2/3,
            "win": (5000, 15000),
            "lose": (-5000, -2000),
            "success_text": "인! 깔끔한 스파이크!",
            "fail_text": "아웃! 실력이 그게 뭔가요?",
        },
    ]

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = EconomyDB()

    # ───────────── 유저 명령어 ─────────────

    @app_commands.command(name="지갑", description="내 잔액을 확인합니다.")
    async def wallet(self, interaction: discord.Interaction):
        bal = await self.db.get_balance(interaction.user.id)
        e = _embed("💰 지갑", f"{interaction.user.mention} 잔액: **{bal:,}**", interaction.user)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="출석", description="하루 1번 출석 보상을 받습니다.")
    async def daily(self, interaction: discord.Interaction):
        await interaction.response.defer()

        reward = 30000
        now_ts = int(time.time())

        try:
            ok, new_bal, remaining, streak, streak_bonus = await self.db.claim_daily(interaction.user.id, reward, now_ts)
        except Exception as ex:
            return await interaction.followup.send(f"❌ DB 오류: {type(ex).__name__}")

        if not ok:
            cur = await self.db.get_balance(interaction.user.id)
            e = _embed(
                "⏳ 출석 보상",
                f"{interaction.user.mention}\n이미 출석 보상을 받았습니다.\n남은 시간: **{_format_time_left(remaining)}**\n현재 잔액: **{cur:,}**",
                interaction.user,
            )
            return await interaction.followup.send(embed=e)

        # 스트릭 표시 구성
        _NEXT_MILESTONE = {v: v for v in (7, 14, 30)}
        next_ms = next((m for m in (7, 14, 30) if m > streak), None)
        streak_line = f"🔥 연속 출석 **{streak}일**"
        if next_ms:
            streak_line += f"  (다음 보너스까지 **{next_ms - streak}일**)"
        bonus_line = f"\n🎁 **스트릭 보너스 +{streak_bonus:,}원** ({streak}일 달성!)" if streak_bonus else ""

        e = _embed(
            "✅ 출석 완료",
            f"{interaction.user.mention}\n"
            f"기본 보상: **{reward:,}원**{bonus_line}\n"
            f"현재 잔액: **{new_bal:,}**\n\n"
            f"{streak_line}",
            interaction.user,
        )
        await interaction.followup.send(embed=e)

    @app_commands.command(name="송금", description="다른 유저에게 돈을 보냅니다.")
    @app_commands.describe(to_user="받을 유저", amount="보낼 금액(1 이상)")
    async def transfer(self, interaction: discord.Interaction, to_user: discord.Member, amount: int):
        await interaction.response.defer()

        amount = int(amount)
        err = await self.db.transfer(interaction.user.id, to_user.id, amount)
        if err:
            e = _embed("❌ 송금 실패", f"{interaction.user.mention}\n사유: **{err}**", interaction.user)
            return await interaction.followup.send(embed=e)

        my_bal = await self.db.get_balance(interaction.user.id)
        to_bal = await self.db.get_balance(to_user.id)

        e = _embed(
            "✅ 송금 완료",
            f"{interaction.user.mention} → {to_user.mention}\n금액: **{amount:,}원**\n\n"
            f"보낸 사람 잔액: **{my_bal:,}**\n받는 사람 잔액: **{to_bal:,}**",
            interaction.user,
        )
        await interaction.followup.send(embed=e)

        dm_embed = discord.Embed(
            title="💸 송금 수신",
            description=(
                f"**{interaction.user.display_name}**님에게서 **{amount:,}원**을 받았습니다.\n"
                f"현재 잔액: **{to_bal:,}원**"
            ),
            color=0x2ecc71,
        )
        await send_notify(self.bot, self.db, to_user.id, "송금_수신", dm_embed)

    # ✅ 훈련: /훈련 만 치면 랜덤 상황 발생
    @app_commands.command(name="훈련", description="랜덤 훈련을 진행합니다. (쿨타임 30초)")
    async def training(self, interaction: discord.Interaction):
        await interaction.response.defer()

        ev = random.choice(self.TRAIN_EVENTS)
        success = (random.random() < ev["success_rate"])

        if success:
            delta = random.randint(ev["win"][0], ev["win"][1])
            result = "성공 ✅"
            line = ev["success_text"]
        else:
            delta = random.randint(ev["lose"][0], ev["lose"][1])  # 음수 범위
            result = "실패 ❌"
            line = ev["fail_text"]

        now_ts = int(time.time())

        try:
            ok, new_bal, remaining = await self.db.play_training(
                interaction.user.id, delta, now_ts, cooldown_sec=self.TRAIN_COOLDOWN
            )
        except Exception as e:
            return await interaction.followup.send(f"❌ DB 오류: {type(e).__name__}")

        if not ok:
            cur = await self.db.get_balance(interaction.user.id)
            e = _embed(
                "⏳ 훈련 쿨타임",
                f"{interaction.user.mention}\n남은 시간: **{_format_time_left(remaining)}**\n현재 잔액: **{cur:,}**",
                interaction.user,
            )
            return await interaction.followup.send(embed=e)

        title = f"{ev['emoji']} {ev['name']}"
        e = _embed(title, f"{interaction.user.mention}\n{line}", interaction.user)
        e.add_field(name="결과", value=result, inline=True)
        e.add_field(name="변동", value=f"**{delta:+,}원**", inline=True)
        e.add_field(name="현재 잔액", value=f"**{new_bal:,}**", inline=False)
        await interaction.followup.send(embed=e)

    # ✅ 페널티킥: 베팅형 + 보기 편한 출력
    @app_commands.command(name="페널티킥", description="돈을 베팅해서 승부합니다. (쿨타임 0초)")
    @app_commands.describe(direction="슛 방향", amount="베팅 금액(1 이상)")
    @app_commands.choices(direction=[
        app_commands.Choice(name="왼쪽", value="L"),
        app_commands.Choice(name="가운데", value="C"),
        app_commands.Choice(name="오른쪽", value="R"),
    ])
    async def penalty_kick(self, interaction: discord.Interaction, direction: app_commands.Choice[str], amount: int):
        await interaction.response.defer()

        amount = int(amount)
        if amount <= 0:
            return await interaction.followup.send("❌ 베팅 금액은 1 이상이어야 합니다.")

        cur_bal = await self.db.get_balance(interaction.user.id)
        if cur_bal < amount:
            e = _embed(
                "❌ 베팅 실패",
                f"{interaction.user.mention}\n잔액이 부족합니다.",
                interaction.user,
            )
            e.add_field(name="베팅", value=f"{amount:,}원", inline=True)
            e.add_field(name="현재 잔액", value=f"{cur_bal:,}", inline=True)
            return await interaction.followup.send(embed=e)

        keeper = random.choice(["L", "C", "R"])
        scored = (direction.value != keeper)

        # 배당 1.5배 → 순이익 +50%
        profit = amount // 2  # 정수 처리
        total_return = amount + profit

        if scored:
            delta = profit
            title = "⚽ 페널티킥 성공"
            outcome = "골 ✅"
            payout_text = f"+{profit:,}원"
            return_text = f"{total_return:,}원"
            odds_text = "1.5배"
        else:
            delta = -amount
            title = "🧤 페널티킥 실패"
            outcome = "선방 ❌"
            payout_text = f"-{amount:,}원"
            return_text = "0원"
            odds_text = "-"

        now_ts = int(time.time())

        try:
            ok, new_bal, remaining = await self.db.play_penalty_kick(
                interaction.user.id, delta, now_ts, cooldown_sec=self.PENALTY_COOLDOWN
            )
        except Exception as e:
            return await interaction.followup.send(f"❌ DB 오류: {type(e).__name__}")

        if not ok:
            e = _embed(
                "⏳ 페널티킥 쿨타임",
                f"{interaction.user.mention}\n남은 시간: **{_format_time_left(remaining)}**\n현재 잔액: **{cur_bal:,}**",
                interaction.user,
            )
            return await interaction.followup.send(embed=e)

        my_shot = _dir_name(direction.value)
        gk = _dir_name(keeper)

        e = _embed(title, f"{interaction.user.mention}\n내 슛: **{my_shot}** / 골키퍼: **{gk}**", interaction.user)
        e.add_field(name="베팅", value=f"{amount:,}원", inline=True)
        e.add_field(name="결과", value=outcome, inline=True)
        e.add_field(name="배당", value=odds_text, inline=True)
        e.add_field(name="순이익(변동)", value=payout_text, inline=True)
        e.add_field(name="총 반환(성공 시)", value=return_text, inline=True)
        e.add_field(name="현재 잔액", value=f"**{new_bal:,}**", inline=False)
        await interaction.followup.send(embed=e)

    # ───────────── 본인 전용(관리자) ─────────────

    @app_commands.command(name="돈지급", description="(본인전용) 유저에게 돈을 지급/회수합니다. (음수=회수)")
    @app_commands.describe(user="대상 유저", amount="지급 금액(음수 가능)")
    @app_commands.check(owner_only)
    async def give(self, interaction: discord.Interaction, user: discord.Member, amount: int):

        await interaction.response.defer()
        new_bal = await self.db.add_balance(user.id, int(amount))

        e = _embed(
            "🧾 돈 지급/회수",
            f"대상: {user.mention}",
            interaction.user,
        )
        e.add_field(name="변동", value=f"{int(amount):,}원", inline=True)
        e.add_field(name="현재 잔액", value=f"{new_bal:,}", inline=True)
        await interaction.followup.send(embed=e)

    @app_commands.command(name="돈설정", description="(본인전용) 유저 잔액을 특정 값으로 설정합니다.")
    @app_commands.describe(user="대상 유저", balance="새 잔액(0 이상)")
    @app_commands.check(owner_only)
    async def setbal(self, interaction: discord.Interaction, user: discord.Member, balance: int):

        await interaction.response.defer()
        bal = max(0, int(balance))
        await self.db.set_balance(user.id, bal)

        e = _embed("🧾 잔액 설정", f"대상: {user.mention}", interaction.user)
        e.add_field(name="설정 잔액", value=f"{bal:,}", inline=True)
        await interaction.followup.send(embed=e)

    @app_commands.command(name="배당설정", description="토토 경기 기본 배당을 직접 설정합니다.")
    @app_commands.describe(
        match_id="경기 ID",
        home="홈승 배당",
        draw="무승부 배당",
        away="원정승 배당",
    )
    @app_commands.check(owner_only)
    async def set_odds(
        self,
        interaction: discord.Interaction,
        match_id: str,
        home: float,
        draw: float,
        away: float,
    ):
        await interaction.response.defer(ephemeral=True)

        await self.db.toto_update_base_odds(
            match_id=match_id.strip(),
            base_home=home,
            base_draw=draw,
            base_away=away,
        )

        await interaction.followup.send(
            f"✅ 배당 설정 완료\n"
            f"홈승: {home} / 무: {draw} / 원정승: {away}",
            ephemeral=True,
        )

    @app_commands.command(name="유저초기화", description="(본인전용) 특정 유저의 모든 데이터를 DB에서 삭제합니다.")
    @app_commands.describe(user_id="삭제할 유저의 Discord ID (/랭킹에서 확인)")
    @app_commands.check(owner_only)
    async def delete_user(self, interaction: discord.Interaction, user_id: str):
        await interaction.response.defer(ephemeral=True)

        try:
            uid = int(user_id.strip())
        except ValueError:
            return await interaction.followup.send("❌ 올바른 Discord ID를 입력하세요.", ephemeral=True)

        from services.player_market_db import PlayerMarketDB
        pm = PlayerMarketDB()

        eco_result = await self.db.delete_user(uid)
        pm_result  = await pm.delete_user(uid)
        merged = {**eco_result, **pm_result}

        if not merged:
            return await interaction.followup.send(
                f"ℹ️ ID `{uid}` 유저의 DB 데이터가 없습니다.", ephemeral=True
            )

        lines = [f"• `{table}` : {cnt}행" for table, cnt in merged.items()]
        await interaction.followup.send(
            f"✅ ID `{uid}` 유저 데이터 삭제 완료\n" + "\n".join(lines),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Economy(bot))
