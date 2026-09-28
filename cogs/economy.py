# cogs/economy.py
import asyncio
import os
import time
import random
from fractions import Fraction
import discord
from discord import app_commands
from discord.ext import commands
from auth import owner_only

from services.economy_db import EconomyDB, TRAIN_MAX_LEVEL
from services.notifier import send_notify
from services import ui

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


def _embed(title: str, desc: str, user: discord.abc.User) -> discord.Embed:
    e = discord.Embed(title=title, description=desc)
    e.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    return e


class Economy(commands.Cog):
    TRAIN_COOLDOWN   = 30

    # 훈련 레벨 효과: 레벨당 성공률 +1%p(최대 95%), 보상·손실 배율 +8% (Lv.1 x1.00 → Lv.30 x3.32)
    TRAIN_RATE_PER_LV  = 0.01
    TRAIN_RATE_CAP     = 0.95
    TRAIN_MONEY_PER_LV = 0.08
    TRAIN_CRIT_RATE    = 0.07   # 성공 중 대성공 비율 (보상 x3)
    TRAIN_TIERS = [             # (시작 레벨, 등급) — 표시용
        (1, "🌱 유스"), (6, "🥉 2군"), (11, "🥈 1군"), (16, "🥇 주전"),
        (21, "⭐ 에이스"), (26, "🌟 월드클래스"), (30, "👑 레전드"),
    ]

    # 페널티킥 배당표: (확률, 순이익 배수, 결과 이름, 연출 문구)
    # 순이익 = 베팅 x 배수 (1,000만원 x1.5 → +1,500만원). 기대값 약 -1.1%.
    # 손실 단계: 골대 강타 1배 → 선방 2배 → 레드카드 5배 → 관중석 홈런 10배.
    # 마지막 줄(선방)이 부동소수 잔여 구간을 받는다.
    PK_TABLE = [
        (0.001, "200",  "🌟 전설의 파넨카", "골키퍼가 먼저 몸을 날린 순간, 한가운데로 툭… 관중석이 폭발합니다!! 🤯"),
        (0.003, "20",   "🚀 무회전 탑코너", "공이 흔들리며 날아가 골대 구석 상단에 그대로 꽂혔습니다! 🔥"),
        (0.010, "5",    "🎯 골대 맞고 인",  "골대를 때린 공이 그대로 골라인을 넘었습니다! 😆"),
        (0.466, "1.5",  "⚽ 골",            "깔끔하게 구석을 찔렀습니다. 😎"),
        (0.060, "0",    "🫳 손 맞고 골",    "골키퍼 손끝에 맞고 간신히 들어갔습니다… 휴 😅"),
        (0.040, "-1",   "🥅 골대 강타",     "골대를 맞고 튕겨 나왔습니다… 아까워라 😣"),
        (0.020, "-5",   "🟥 레드카드",      "슛 직후 골키퍼와 충돌해 레드카드! 벌금이 청구됩니다 😵"),
        (0.010, "-10",  "💥 관중석 홈런",   "공이 관중석 전광판을 박살냈습니다… 수리비 청구서가 날아옵니다 😱"),
        (0.390, "-2",   "🧤 선방",          "골키퍼가 완벽하게 읽었습니다… 😭"),
    ]

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

    # ✅ 훈련: 쿨타임 30초 + 하루 횟수 제한 + 레벨이 오를수록 성공률·보상 증가
    @classmethod
    def train_money_mult(cls, level: int) -> float:
        return 1 + cls.TRAIN_MONEY_PER_LV * (level - 1)

    @classmethod
    def train_tier(cls, level: int) -> str:
        return [name for start, name in cls.TRAIN_TIERS if level >= start][-1]

    def _train_roll(self, level: int):
        """(돈 변동, 경험치 변동, 표시 정보). 성공 +3 XP · 대성공 +5 XP · 실패 -1 XP."""
        ev = random.choice(self.TRAIN_EVENTS)
        rate = min(self.TRAIN_RATE_CAP, ev["success_rate"] + self.TRAIN_RATE_PER_LV * (level - 1))
        mult = self.train_money_mult(level)
        info = {"ev": ev, "rate": rate, "mult": mult}
        if random.random() >= rate:
            return int(random.randint(*ev["lose"]) * mult), -1, {**info, "kind": "fail", "line": ev["fail_text"]}
        delta = int(random.randint(*ev["win"]) * mult)
        if random.random() < self.TRAIN_CRIT_RATE:
            return delta * 3, 5, {**info, "kind": "crit", "line": ev["success_text"]}
        return delta, 3, {**info, "kind": "ok", "line": ev["success_text"]}

    def _train_level_field(self, e: discord.Embed, r: dict, xp_gain: int | None = None):
        lv = r["level"]
        e.add_field(name="🎚️ 레벨", value=f"**Lv.{lv}** {self.train_tier(lv)}", inline=True)
        e.add_field(name="💰 보상 배율", value=f"x{self.train_money_mult(lv):.2f}", inline=True)
        e.add_field(name="📅 오늘", value=f"{r['used']}/{r['limit']}회", inline=True)
        if lv >= TRAIN_MAX_LEVEL:
            xp_line = f"`{ui.bar(1, 1)}` **MAX**"
        else:
            xp_line = f"`{ui.bar(r['xp'], r['need'])}` {r['xp']}/{r['need']} XP"
        if xp_gain is not None:
            xp_line += f"  ({xp_gain:+d})"
        e.add_field(name="✨ 경험치", value=xp_line, inline=False)

    @app_commands.command(name="훈련", description="랜덤 훈련으로 돈과 경험치를 얻습니다. (쿨타임 30초 · 하루 횟수 제한 · 레벨이 오르면 보상 증가)")
    async def training(self, interaction: discord.Interaction):
        await interaction.response.defer()
        user = interaction.user
        now_ts = int(time.time())
        try:
            r = await self.db.play_training(user.id, now_ts, self._train_roll, cooldown_sec=self.TRAIN_COOLDOWN)
        except Exception as e:
            return await interaction.followup.send(f"❌ DB 오류: {type(e).__name__}")

        if not r["ok"]:
            if r["reason"] == "cooldown":
                e = ui.card("⏳ 숨 고르는 중", f"다음 훈련은 <t:{now_ts + r['remaining']}:R> 가능합니다.",
                            ui.DARK, user, "🏋️ 훈련")
            else:
                midnight = ((now_ts + 9 * 3600) // 86400 + 1) * 86400 - 9 * 3600
                e = ui.card("😮‍💨 오늘 훈련 끝", f"오늘 훈련 횟수를 모두 썼습니다.\n다시 훈련할 수 있는 시간: <t:{midnight}:R>",
                            ui.DARK, user, "🏋️ 훈련")
            self._train_level_field(e, r)
            return await interaction.followup.send(embed=e)

        info, ev = r["info"], r["info"]["ev"]
        kind = info["kind"]
        badge = {"ok": "성공 ✅", "crit": "🔥 대성공! 보상 x3", "fail": "실패 ❌"}[kind]
        color = {"ok": ui.WIN, "crit": ui.GOLD, "fail": ui.LOSE}[kind]
        e = ui.card(
            f"{ev['emoji']} {ev['name']} — {badge}",
            f"> {info['line']}\n## {ui.won(r['delta'])}",
            color, user, "🏋️ 훈련",
        )
        e.add_field(name="📈 성공률", value=f"{info['rate'] * 100:.0f}%", inline=True)
        e.add_field(name="💵 잔액", value=f"{r['new_bal']:,}원", inline=True)
        e.add_field(name="​", value="​", inline=True)
        self._train_level_field(e, r, {"ok": 3, "crit": 5, "fail": -1}[kind])
        if r["leveled"]:
            old_lv = r["level"] - r["leveled"]
            e.color = ui.GOLD
            e.add_field(
                name=f"🆙 레벨 업! Lv.{old_lv} → Lv.{r['level']}",
                value=(f"보상 배율 x{self.train_money_mult(old_lv):.2f} → **x{self.train_money_mult(r['level']):.2f}**\n"
                       f"성공률 +{self.TRAIN_RATE_PER_LV * 100 * r['leveled']:.0f}%p · 하루 {r['limit']}회"),
                inline=False,
            )
        e.set_footer(text=f"다음 훈련까지 {self.TRAIN_COOLDOWN}초 · 성공 +3 / 대성공 +5 / 실패 -1 XP")
        await interaction.followup.send(embed=e)

    # ✅ 페널티킥: 방향 선택 없이 완전 랜덤, 쿨타임 없음 — 짧은 연출 후 결과 보고
    @staticmethod
    def _pk_money_line(mult: Fraction, delta: int) -> str:
        """1.5 → '베팅액의 1.5배인 1,500,000원을 얻었습니다!!' 처럼 결과를 말로 푼다."""
        if mult > 0:
            return f"베팅액의 {float(mult):g}배인 {delta:,}원을 얻었습니다!!"
        if mult == 0:
            return "다행히 잃은 돈은 없습니다. 본전!"
        if mult == -1:
            return f"베팅액 {-delta:,}원을 모두 잃었습니다…"
        return f"베팅액의 {float(-mult):g}배인 {-delta:,}원을 잃었습니다!!"

    @app_commands.command(name="페널티킥", description="돈을 걸고 슛! 최대 200배 수익, 최악은 10배 손실")
    @app_commands.describe(amount="베팅 금액(1 이상)")
    async def penalty_kick(self, interaction: discord.Interaction, amount: int):
        await interaction.response.defer()

        amount = int(amount)
        if amount <= 0:
            return await interaction.followup.send(embed=ui.card("⚽ 페널티킥", "**베팅 금액은 1원 이상이어야 합니다.**", ui.LOSE))

        cur_bal = await self.db.get_balance(interaction.user.id)
        if cur_bal < amount:
            return await interaction.followup.send(embed=ui.card(
                "⚽ 페널티킥", f"**잔액이 부족합니다.**\n\n베팅 {amount:,}원 · 잔액 {cur_bal:,}원", ui.LOSE))

        roll, acc = random.random(), 0.0
        for prob, mult_s, tier_name, tier_text in self.PK_TABLE:
            acc += prob
            if roll < acc:
                break
        mult = Fraction(mult_s)
        delta = int(amount * mult)  # 순이익 = 베팅 x 배수

        # 결과를 먼저 저장하고 나서 연출한다 (연출이 실패해도 돈은 정확하다).
        try:
            _, new_bal, _ = await self.db.play_penalty_kick(interaction.user.id, delta, int(time.time()))
        except Exception as e:
            return await interaction.followup.send(f"❌ DB 오류: {type(e).__name__}")

        if mult >= 5:
            color = ui.GOLD
        elif mult <= -10:
            color = ui.DOOM
        else:
            color = ui.tone(delta)
        e = ui.card(
            "⚽ 페널티킥 결과",
            f"**[결과] {tier_name}**\n\n"
            f"{tier_text}\n\n"
            f"**{self._pk_money_line(mult, delta)}**\n\n"
            f"**잔액 : {new_bal:,}원 ({f'{delta:+,}' if delta else '±0'})**",
            color,
        )
        e.set_thumbnail(url=ui.emoji_url(tier_name.split()[0]))

        try:
            msg = await interaction.followup.send(embed=ui.card(
                "⚽ 페널티킥", f"**{amount:,}원**을 걸고 공 앞에 섰습니다…\n\n🏃 도움닫기… 💨 **슛!**", ui.DARK), wait=True)
            await asyncio.sleep(1.2)
            await msg.edit(embed=e)
        except discord.HTTPException:
            await interaction.followup.send(embed=e)

    # ✅ 홀인원: 확률 극악 잭팟
    HOLEINONE_MIN  = 100_000
    HOLEINONE_PROB = 0.0005  # 0.05%

    @app_commands.command(name="홀인원", description="최소 100,000원으로 100배 잭팟 도전! 확률 0.05%")
    @app_commands.describe(금액="베팅 금액 (최소 100,000원)")
    async def hole_in_one(self, interaction: discord.Interaction, 금액: int):
        await interaction.response.defer()

        if 금액 < self.HOLEINONE_MIN:
            return await interaction.followup.send(
                embed=_embed("❌ 최소 금액 미달", f"최소 베팅: **{self.HOLEINONE_MIN:,}원**\n입력 금액: **{금액:,}원**", interaction.user)
            )

        cur_bal = await self.db.get_balance(interaction.user.id)
        if cur_bal < 금액:
            return await interaction.followup.send(
                embed=_embed("❌ 잔액 부족", f"베팅 금액: **{금액:,}원**\n현재 잔액: **{cur_bal:,}원**", interaction.user)
            )

        reward  = 금액 * 100
        console = 금액 // 20  # 꽝 시 5% 위로금

        hit = random.random() < self.HOLEINONE_PROB
        if hit:
            delta   = reward - 금액
            new_bal = await self.db.add_balance(interaction.user.id, delta)
            e = _embed(
                "⛳ 홀인원!!!",
                f"{interaction.user.mention}\n🎉 **축하합니다! 홀인원 달성!**\n\n베팅: **{금액:,}원**\n당첨 보상: **+{reward:,}원**\n순이익: **+{delta:,}원**\n현재 잔액: **{new_bal:,}원**",
                interaction.user,
            )
            e.color = discord.Color.gold()
        else:
            delta   = -(금액 - console)
            new_bal = await self.db.add_balance(interaction.user.id, delta)
            e = _embed(
                "💨 아쉽게 빗나갔습니다",
                f"{interaction.user.mention}\n\n베팅: **{금액:,}원**\n위로금: **+{console:,}원**\n실손실: **-{금액 - console:,}원**\n현재 잔액: **{new_bal:,}원**\n\n*당첨 확률: 0.05% | 당첨 시 100배*",
                interaction.user,
            )
            e.color = discord.Color.dark_gray()

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
