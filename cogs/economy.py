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

    # 훈련 레벨 효과: 보상·손실 배율 = 레벨 (Lv.1 1배 … Lv.10 10배),
    # 성공률은 레벨당 +1%p 오르지만 화면에는 표시하지 않는다.
    TRAIN_RATE_PER_LV = 0.01
    TRAIN_RATE_CAP    = 0.95
    TRAIN_TIERS = [             # (시작 레벨, 등급) — 표시용
        (1, "🌱 유스"), (3, "🥉 2군"), (5, "🥈 1군"), (7, "🥇 주전"), (9, "⭐ 에이스"), (10, "👑 레전드"),
    ]

    # 페널티킥 배당표: (확률, 순이익 배수, 이모지, 이름, 중계 헤드라인, 캐스터 멘트)
    # 순이익 = 베팅 x 배수 (1,000만원 x1.5 → +1,500만원). 기대값 약 -1.0%.
    # 수익 단계: 골 1배 → 골대 맞고 인 2배 → 노룩 킥 5배 → 탑코너 20배 → 파넨카 200배.
    # 손실 단계: 선방 1배(가장 흔함, 골과 비슷) → 골대 강타 2배 → 부정킥 5배 → 관중석 홈런 10배.
    # 마지막 줄(선방)이 부동소수 잔여 구간을 받는다.
    PK_MIN_BET = 5_000
    PK_SPAM_GAP = 1.0   # 도배 방지 간격(초)
    PK_SPAM_LINES = [   # 도배 방지에 걸렸을 때 (제목, 캐스터 멘트) — 남은 시간은 굳이 말하지 않는다
        ("🏃 볼보이가 공을 가져오는 중!", "공이 아직 안 돌아왔어요! 볼보이가 열심히 뛰어오고 있습니다."),
        ("✋ 주심이 잠깐 멈춰 세웁니다", "주심이 휘슬을 입에 물고 있어요. 신호가 떨어지면 차 주세요!"),
        ("😤 키커가 숨을 고릅니다", "너무 서두르면 실축합니다! 숨 한 번 크게 쉬고 다시 가죠."),
        ("📺 VAR 확인 중", "직전 킥을 VAR로 돌려 보고 있어요. 잠시만요!"),
    ]
    PK_TABLE = [
        (0.0005, "200", "🌟", "전설의 파넨카", "파넨카!!! 전설이 탄생합니다",
         "골키퍼가 먼저 몸을 날렸어요! 한가운데로 툭— 믿을 수 없는 배짱입니다!!"),
        (0.003, "20",  "🚀", "무회전 탑코너", "무회전 탑코너!!",
         "공이 흔들리면서… 구석 상단에 그대로 꽂힙니다! 골키퍼는 손도 못 댔어요!"),
        (0.020, "5",   "😎", "노룩 킥",       "노룩 킥! 여유가 넘칩니다",
         "골키퍼는 쳐다보지도 않았어요! 반대쪽 구석으로 여유롭게 밀어 넣습니다!"),
        (0.060, "2",   "🎯", "골대 맞고 인",  "골대 맞고… 들어갑니다!!",
         "골대를 때렸는데— 들어갔어요! 심장이 멎는 줄 알았습니다!"),
        (0.3627, "1",  "⚽", "골",            "골! 깔끔합니다",
         "구석을 정확히 찔렀어요. 교과서 같은 페널티킥입니다."),
        (0.060, "0",   "🫳", "손 맞고 골",    "손 맞고… 골! 본전입니다",
         "골키퍼 손끝에 걸렸지만 겨우 넘어갔어요. 휴, 간신히 살았습니다."),
        (0.070, "-2",  "🥅", "골대 강타",     "골대! 그리고 역습 실점…",
         "골대를 맞고 튕겨 나온 공이 그대로 역습으로 이어집니다… 이건 최악이에요!"),
        (0.025, "-5",  "🚫", "부정킥",        "부정킥 선언!",
         "도움닫기 중 멈칫하는 동작! 주심이 부정킥을 선언합니다. 징계금이 나오겠네요."),
        (0.010, "-10", "💥", "관중석 홈런",   "관중석으로 날아갑니다…",
         "공이… 관중석 전광판을 박살냈어요! 수리비 청구서가 날아옵니다!"),
        (0.3888, "-1", "🧤", "선방",          "막아냅니다!",
         "골키퍼가 방향을 완벽하게 읽었어요! 키커는 고개를 숙입니다."),
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
        self._pk_last: dict[int, float] = {}   # 유저별 마지막 페널티킥 시각 (도배 방지)

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

    # ✅ 훈련: 쿨타임 30초 + 하루 30회 + 레벨(Lv.N = 보상 N배)
    @staticmethod
    def train_money_mult(level: int) -> int:
        """보상·손실 배율 = 레벨 (Lv.1 1배 … Lv.10 10배)."""
        return min(int(level), TRAIN_MAX_LEVEL)

    @classmethod
    def train_tier(cls, level: int) -> str:
        return [name for start, name in cls.TRAIN_TIERS if level >= start][-1]

    def _train_roll(self, level: int):
        """(돈 변동, 경험치 변동, 표시 정보). 성공 +3 XP · 실패 -1 XP. 성공률은 레벨마다 조금씩 오르지만 화면엔 안 보인다."""
        ev = random.choice(self.TRAIN_EVENTS)
        rate = min(self.TRAIN_RATE_CAP, ev["success_rate"] + self.TRAIN_RATE_PER_LV * (level - 1))
        mult = self.train_money_mult(level)
        if random.random() >= rate:
            return random.randint(*ev["lose"]) * mult, -1, {"ev": ev, "ok": False, "line": ev["fail_text"]}
        return random.randint(*ev["win"]) * mult, 3, {"ev": ev, "ok": True, "line": ev["success_text"]}

    def _train_status(self, r: dict, xp_gain: int | None = None) -> str:
        """`레벨` / `경험치` / `오늘` 세 줄."""
        lv = r["level"]
        if lv >= TRAIN_MAX_LEVEL:
            xp = f"`{ui.bar(1, 1)}` **MAX**"
        else:
            xp = f"`{ui.bar(r['xp'], r['need'])}` {r['xp']}/{r['need']}"
            if xp_gain is not None:
                xp += f" ({xp_gain:+d})"
        return (f"`레벨` **Lv.{lv}** {self.train_tier(lv)} · 보상 **{self.train_money_mult(lv)}배**\n"
                f"`경험` {xp}\n"
                f"`오늘` {r['used']}/{r['limit']}회")

    @staticmethod
    def _train_card(user, title: str, caster: str, color: int) -> discord.Embed:
        return ui.card(title, f"> 🎙️ *\"{caster}\"*", color, user, "🎙️ 훈련장 리포트")

    @app_commands.command(name="훈련", description="랜덤 훈련으로 돈과 경험치를 얻습니다. (쿨타임 30초 · 하루 30회 · Lv.N = 보상 N배)")
    async def training(self, interaction: discord.Interaction):
        await interaction.response.defer()
        user = interaction.user
        now_ts = int(time.time())
        try:
            r = await self.db.play_training(user.id, now_ts, self._train_roll, cooldown_sec=self.TRAIN_COOLDOWN)
        except Exception as e:
            return await interaction.followup.send(f"❌ DB 오류: {type(e).__name__}")

        if not r["ok"]:
            if r["reason"] == "limit":
                e = self._train_card(user, "😮‍💨 오늘 훈련 끝", "오늘 훈련은 여기까지! 내일 00:00에 다시 뵙겠습니다.", ui.DARK)
                e.description += "\n\n" + self._train_status(r)
                return await interaction.followup.send(embed=e)

            # Discord 상대 시간(<t:..:R>)은 0초가 지나면 '1초 전'으로 계속 흘러가므로,
            # 쿨타임이 끝나는 순간 메시지를 '준비 완료'로 바꿔 멈춘다.
            e = self._train_card(user, "⏳ 숨 고르는 중", "선수가 아직 숨이 차 있어요. 조금만 쉬었다 가죠!", ui.DARK)
            e.description += f"\n\n`다음` <t:{now_ts + r['remaining']}:R>\n" + self._train_status(r)
            msg = await interaction.followup.send(embed=e, wait=True)
            await asyncio.sleep(r["remaining"])
            ready = self._train_card(user, "✅ 훈련 준비 완료", "숨을 다 골랐습니다! 지금 바로 `/훈련` 가능해요.", ui.WIN)
            ready.description += "\n\n" + self._train_status(r)
            try:
                await msg.edit(embed=ready)
            except discord.HTTPException:
                pass
            return

        info, ev = r["info"], r["info"]["ev"]
        if r["leveled"]:
            old_lv = r["level"] - r["leveled"]
            title, color = f"🆙 레벨 업! Lv.{old_lv} → Lv.{r['level']}", ui.GOLD
        elif info["ok"]:
            title, color = f"{ev['emoji']} {ev['name']} — 성공!", ui.WIN
        else:
            title, color = f"{ev['emoji']} {ev['name']} — 실패…", ui.LOSE
        e = self._train_card(user, title, info["line"], color)
        e.description += (
            f"\n\n`정산` **{ui.won(r['delta'])}**\n"
            f"`잔액` **{r['new_bal']:,}원**\n"
            + self._train_status(r, 3 if info["ok"] else -1)
        )
        if r["leveled"]:
            new_tier = self.train_tier(r["level"])
            promo = f"**{new_tier} 승격!** " if new_tier != self.train_tier(old_lv) else ""
            e.description += (f"\n\n🆙 {promo}보상 {self.train_money_mult(old_lv)}배 → "
                              f"**{self.train_money_mult(r['level'])}배**")
        e.set_thumbnail(url=ui.emoji_url(ev["emoji"]))
        await interaction.followup.send(embed=e)

    # ✅ 페널티킥: 방향 선택 없이 완전 랜덤, 쿨타임 없음 — 중계 연출 후 결과
    @staticmethod
    def _pk_label(mult: Fraction) -> str:
        """1 → '1배 수익', 0 → '본전', -2 → '2배 손실'."""
        if mult > 0:
            return f"{float(mult):g}배 수익"
        if mult == 0:
            return "본전"
        return f"{float(-mult):g}배 손실"

    @staticmethod
    def _pk_card(user, title: str, caster: str, color: int) -> discord.Embed:
        """페널티킥 화면의 공통 틀: '닉네임 · 🎙️ 페널티킥 중계' + 캐스터 멘트."""
        return ui.card(title, f"> 🎙️ *\"{caster}\"*", color, user, "🎙️ 페널티킥 중계")

    @app_commands.command(name="페널티킥", description="돈을 걸고 슛! 최대 200배 수익, 최악은 10배 손실 (최소 5,000원)")
    @app_commands.describe(amount="베팅 금액 (최소 5,000원)")
    async def penalty_kick(self, interaction: discord.Interaction, amount: app_commands.Range[int, PK_MIN_BET]):
        user = interaction.user
        # 도배 방지: 직전 킥에서 1초가 안 지났으면 본인에게만 중계 멘트를 보여주고 끝낸다.
        now = time.monotonic()
        if now - self._pk_last.get(user.id, 0.0) < self.PK_SPAM_GAP:
            title, caster = random.choice(self.PK_SPAM_LINES)
            return await interaction.response.send_message(embed=self._pk_card(user, title, caster, ui.DARK), ephemeral=True)
        self._pk_last[user.id] = now

        await interaction.response.defer()
        amount = int(amount)

        cur_bal = await self.db.get_balance(user.id)
        if cur_bal < amount:
            e = self._pk_card(user, "🙅 키커가 입장하지 못합니다", "잔액이 부족해 경기장에 들어오지 못했어요!", ui.LOSE)
            e.description += f"\n\n`베팅` **{amount:,}원**\n`잔액` **{cur_bal:,}원**"
            return await interaction.followup.send(embed=e)

        roll, acc = random.random(), 0.0
        for prob, mult_s, emoji, name, headline, caster in self.PK_TABLE:
            acc += prob
            if roll < acc:
                break
        mult = Fraction(mult_s)
        delta = int(amount * mult)  # 순이익 = 베팅 x 배수

        # 결과를 먼저 저장하고 나서 연출한다 (연출이 실패해도 돈은 정확하다).
        try:
            _, new_bal, _ = await self.db.play_penalty_kick(user.id, delta, int(time.time()))
        except Exception as e:
            return await interaction.followup.send(f"❌ DB 오류: {type(e).__name__}")

        if mult >= 5:
            color = ui.GOLD
        elif mult <= -5:
            color = ui.DOOM
        else:
            color = ui.tone(delta)
        e = self._pk_card(user, f"{emoji} {headline}", caster, color)
        e.description += (
            f"\n\n`정산` **{ui.won(delta)}** · {self._pk_label(mult)}"
            f"\n`잔액` **{new_bal:,}원**"
        )
        e.set_thumbnail(url=ui.emoji_url(emoji))

        try:
            ready = self._pk_card(user, "⚽ 키커가 공을 내려놓습니다…",
                                  f"{amount:,}원이 걸린 11m 승부! 도움닫기… 슛!", ui.DARK)
            msg = await interaction.followup.send(embed=ready, wait=True)
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
