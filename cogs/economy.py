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

from services.economy_db import EconomyDB, TRAIN_MAX_LEVEL, SCOUT_MAX_LEVEL
from services.player_market_db import SCOUT_FIND_PROB, give_player, scout_find_player
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


class RaceView(discord.ui.View):
    """경마 출주표의 말 선택 버튼 — 경주를 연 사람만, 한 번만 누를 수 있다."""

    def __init__(self, cog: "Economy", user, amount: int, race: dict):
        super().__init__(timeout=60)
        self.cog, self.user, self.amount, self.race = cog, user, amount, race
        self.message = None
        self.done = False
        for i, h in enumerate(race["horses"]):
            b = discord.ui.Button(label=f"{i + 1}번 {h[1]}", emoji=h[0], style=discord.ButtonStyle.primary)
            b.callback = self._picker(i)
            self.add_item(b)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("🙅 경주를 연 사람만 말을 고를 수 있어요.", ephemeral=True)
            return False
        return True

    def _picker(self, idx: int):
        async def pick(interaction: discord.Interaction):
            if self.done:   # 연타 방지
                return await interaction.response.defer()
            self.done = True
            self.stop()
            await self.cog._run_race(interaction, self, idx)
        return pick

    async def on_timeout(self):
        if self.done or self.message is None:
            return
        e = ui.card("⌛ 출주 취소", '> 🎙️ *"말을 고르지 않아 경주가 취소됐습니다. 베팅금은 그대로예요."*',
                    ui.EVEN, self.user, Economy.RACE_SECTION)
        try:
            await self.message.edit(embed=e, view=None)
        except discord.HTTPException:
            pass


class Economy(commands.Cog):
    TRAIN_COOLDOWN   = 30

    # 훈련 레벨 효과: 보상·손실 배율 = 레벨 (Lv.1 1배 … Lv.10 10배),
    # 성공률은 레벨당 +1%p 오르지만 화면에는 표시하지 않는다.
    TRAIN_RATE_PER_LV = 0.01
    TRAIN_RATE_CAP    = 0.95
    TRAIN_TIERS = [             # (시작 레벨, 등급) — 표시용
        (1, "🌱 유스"), (3, "🥉 2군"), (5, "🥈 1군"), (7, "🥇 주전"), (9, "⭐ 에이스"), (10, "👑 레전드"),
    ]

    # 스카우트: 레벨 = 보상 배율(1~5배). 성공률은 훈련처럼 레벨당 +1%p (표시 안 함).
    SCOUT_COOLDOWN = 60
    SCOUT_LEVEL_NAMES = ["🔍 지역 스카우트", "🗺️ 국내 스카우트", "✈️ 해외 스카우트", "🌍 수석 스카우트", "👁️ 전설의 스카우트"]
    SCOUT_EVENTS = [
        {"emoji": "🇧🇷", "name": "브라질 유스 리그", "success_rate": 0.75, "win": (12000, 30000), "lose": (-9000, -3000),
         "success_text": "해변 풋살장에서 번뜩이는 재능들을 잔뜩 봤습니다!", "fail_text": "경기가 폭우로 취소됐어요… 출장비만 날렸습니다."},
        {"emoji": "🇦🇷", "name": "아르헨티나 동네 구장", "success_rate": 0.70, "win": (12000, 30000), "lose": (-9000, -3000),
         "success_text": "좁은 공간에서 춤추듯 드리블하는 아이를 발견했습니다!", "fail_text": "현지 에이전트에게 소개비만 뜯겼습니다."},
        {"emoji": "🇫🇷", "name": "프랑스 유스 아카데미", "success_rate": 0.75, "win": (12000, 30000), "lose": (-9000, -3000),
         "success_text": "스피드와 피지컬이 남다른 선수들이 가득합니다.", "fail_text": "이미 빅클럽 스카우트들이 다 쓸어 갔네요."},
        {"emoji": "🇳🇬", "name": "나이지리아 유스 토너먼트", "success_rate": 0.70, "win": (12000, 30000), "lose": (-9000, -3000),
         "success_text": "폭발적인 운동능력의 유망주를 체크했습니다!", "fail_text": "비행기가 연착돼 결승전을 놓쳤습니다."},
        {"emoji": "🇪🇸", "name": "스페인 3부 리그", "success_rate": 0.80, "win": (12000, 30000), "lose": (-9000, -3000),
         "success_text": "패스 센스가 기가 막힌 미드필더를 봤습니다.", "fail_text": "보러 간 선수가 부상으로 결장했습니다."},
        {"emoji": "🇯🇵", "name": "일본 고교 선수권", "success_rate": 0.80, "win": (12000, 30000), "lose": (-9000, -3000),
         "success_text": "기본기가 탄탄한 선수들을 꼼꼼히 확인했습니다.", "fail_text": "스카우트 보고서를 호텔에 두고 왔습니다…"},
        {"emoji": "🇰🇷", "name": "K리그 유스 경기", "success_rate": 0.80, "win": (12000, 30000), "lose": (-9000, -3000),
         "success_text": "투지 넘치는 유망주를 리스트에 올렸습니다!", "fail_text": "주차만 1시간… 전반전을 통째로 놓쳤습니다."},
        {"emoji": "🇬🇧", "name": "잉글랜드 챔피언십", "success_rate": 0.70, "win": (12000, 30000), "lose": (-9000, -3000),
         "success_text": "거친 리그에서 살아남은 강심장을 찾았습니다.", "fail_text": "런던 물가에 경비만 잔뜩 썼습니다."},
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

    def _train_roll(self, level: int, con=None):
        """(돈 변동, 경험치 변동, 표시 정보). 성공 +3 XP · 실패 -1 XP. 성공률은 레벨마다 조금씩 오르지만 화면엔 안 보인다."""
        ev = random.choice(self.TRAIN_EVENTS)
        rate = min(self.TRAIN_RATE_CAP, ev["success_rate"] + self.TRAIN_RATE_PER_LV * (level - 1))
        mult = self.train_money_mult(level)
        if random.random() >= rate:
            return random.randint(*ev["lose"]) * mult, -1, {"ev": ev, "ok": False, "line": ev["fail_text"]}
        return random.randint(*ev["win"]) * mult, 3, {"ev": ev, "ok": True, "line": ev["success_text"]}

    @staticmethod
    def _grind_status(r: dict, max_level: int, tier: str, mult: int, xp_gain: int | None = None) -> str:
        """훈련·스카우트 공용 `레벨` / `경험` / `오늘` 세 줄."""
        lv = r["level"]
        if lv >= max_level:
            xp = f"`{ui.bar(1, 1)}` **MAX**"
        else:
            xp = f"`{ui.bar(r['xp'], r['need'])}` {r['xp']}/{r['need']}"
            if xp_gain is not None:
                xp += f" ({xp_gain:+d})"
        return (f"`레벨` **Lv.{lv}** {tier} · 보상 **{mult}배**\n"
                f"`경험` {xp}\n"
                f"`오늘` {r['used']}/{r['limit']}회")

    def _train_status(self, r: dict, xp_gain: int | None = None) -> str:
        lv = r["level"]
        return self._grind_status(r, TRAIN_MAX_LEVEL, self.train_tier(lv), self.train_money_mult(lv), xp_gain)

    async def _grind_blocked(self, interaction, r: dict, now_ts: int, card, status: str,
                             limit_text: tuple, wait_text: tuple, ready_text: tuple):
        """오늘 횟수 소진 / 쿨타임 화면. (제목, 캐스터 멘트) 튜플을 받는다.
        Discord 상대 시간(<t:..:R>)은 0초가 지나면 '1초 전'으로 계속 흘러가므로,
        쿨타임이 끝나는 순간 메시지를 '준비 완료'로 바꿔 멈춘다."""
        if r["reason"] == "limit":
            e = card(interaction.user, *limit_text, ui.DARK)
            e.description += "\n\n" + status
            return await interaction.followup.send(embed=e)
        e = card(interaction.user, *wait_text, ui.DARK)
        e.description += f"\n\n`다음` <t:{now_ts + r['remaining']}:R>\n" + status
        msg = await interaction.followup.send(embed=e, wait=True)
        await asyncio.sleep(r["remaining"])
        ready = card(interaction.user, *ready_text, ui.WIN)
        ready.description += "\n\n" + status
        try:
            await msg.edit(embed=ready)
        except discord.HTTPException:
            pass

    @staticmethod
    def _train_card(user, title: str, caster: str, color: int) -> discord.Embed:
        return ui.card(title, f"> 🎙️ *\"{caster}\"*", color, user, "🎙️ 훈련장 리포트")

    @app_commands.command(name="훈련", description="랜덤 훈련으로 돈과 경험치를 얻습니다. (오늘 스카우트 15회 완료 후 열림 · 쿨타임 30초 · 하루 30회)")
    async def training(self, interaction: discord.Interaction):
        await interaction.response.defer()
        user = interaction.user
        now_ts = int(time.time())
        try:
            r = await self.db.play_training(user.id, now_ts, self._train_roll, cooldown_sec=self.TRAIN_COOLDOWN)
        except Exception as e:
            return await interaction.followup.send(f"❌ DB 오류: {type(e).__name__}")

        if not r["ok"] and r["reason"] == "locked":
            e = self._train_card(user, "🔒 훈련장이 아직 잠겨 있어요",
                                 "오늘 스카우트 출장을 모두 마쳐야 훈련장이 열립니다!", ui.DARK)
            e.description += (f"\n\n`스카우트` **{r['req_used']}/{r['req_limit']}회** — `/스카우트`로 먼저 다녀오세요\n"
                              + self._train_status(r))
            return await interaction.followup.send(embed=e)
        if not r["ok"]:
            return await self._grind_blocked(
                interaction, r, now_ts, self._train_card, self._train_status(r),
                ("😮‍💨 오늘 훈련 끝", "오늘 훈련은 여기까지! 내일 00:00에 다시 뵙겠습니다."),
                ("⏳ 숨 고르는 중", "선수가 아직 숨이 차 있어요. 조금만 쉬었다 가죠!"),
                ("✅ 훈련 준비 완료", "숨을 다 골랐습니다! 지금 바로 `/훈련` 가능해요."),
            )

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

    # ✅ 스카우트: 훈련의 상위 버전 — 쿨타임 60초 · 하루 15회 · 최대 Lv.5 · 드물게 실제 선수 발굴
    @staticmethod
    def scout_money_mult(level: int) -> int:
        return min(int(level), SCOUT_MAX_LEVEL)

    @classmethod
    def scout_tier(cls, level: int) -> str:
        return cls.SCOUT_LEVEL_NAMES[min(int(level), SCOUT_MAX_LEVEL) - 1]

    def _scout_roll(self, level: int, con, user_id: int):
        """(돈 변동, 경험치 변동, 표시 정보). 성공하면 레벨별 확률로 선수 카드를 같은 트랜잭션에서 지급한다."""
        ev = random.choice(self.SCOUT_EVENTS)
        rate = min(self.TRAIN_RATE_CAP, ev["success_rate"] + self.TRAIN_RATE_PER_LV * (level - 1))
        mult = self.scout_money_mult(level)
        if random.random() >= rate:
            return random.randint(*ev["lose"]) * mult, -1, {"ev": ev, "ok": False, "line": ev["fail_text"], "found": None}
        found = None
        if random.random() < SCOUT_FIND_PROB[min(level, SCOUT_MAX_LEVEL) - 1]:
            found = scout_find_player(con, level)
            if found:
                give_player(con, user_id, found["player_id"])
        return random.randint(*ev["win"]) * mult, 3, {"ev": ev, "ok": True, "line": ev["success_text"], "found": found}

    def _scout_status(self, r: dict, xp_gain: int | None = None) -> str:
        lv = r["level"]
        return self._grind_status(r, SCOUT_MAX_LEVEL, self.scout_tier(lv), self.scout_money_mult(lv), xp_gain)

    @staticmethod
    def _scout_card(user, title: str, caster: str, color: int) -> discord.Embed:
        return ui.card(title, f"> 🎙️ *\"{caster}\"*", color, user, "🎙️ 스카우트 리포트")

    @app_commands.command(name="스카우트", description="세계를 돌며 선수를 찾습니다. 돈을 벌고, 드물게 실제 선수를 영입! (쿨타임 60초 · 하루 15회 · 최대 Lv.5)")
    async def scout(self, interaction: discord.Interaction):
        await interaction.response.defer()
        user = interaction.user
        now_ts = int(time.time())
        try:
            r = await self.db.play_scout(user.id, now_ts, lambda lv, con: self._scout_roll(lv, con, user.id),
                                         cooldown_sec=self.SCOUT_COOLDOWN)
        except Exception as e:
            return await interaction.followup.send(f"❌ DB 오류: {type(e).__name__}")

        if not r["ok"]:
            return await self._grind_blocked(
                interaction, r, now_ts, self._scout_card, self._scout_status(r),
                ("🧳 오늘 출장 끝", "오늘 스카우트 일정은 여기까지! 내일 00:00에 다시 떠나죠."),
                ("✈️ 이동 중", "다음 경기장으로 이동하고 있어요. 조금만 기다려 주세요!"),
                ("✅ 출장 준비 완료", "짐을 다 쌌습니다! 지금 바로 `/스카우트` 가능해요."),
            )

        info, ev, found = r["info"], r["info"]["ev"], r["info"]["found"]
        if found:
            title = f"💎 선수 발굴!! — {found['tier']}"
            color = ui.GOLD if found["tier_index"] >= 3 else ui.WIN
        elif info["ok"]:
            title, color = f"{ev['emoji']} {ev['name']} — 스카우트 성공!", ui.WIN
        else:
            title, color = f"{ev['emoji']} {ev['name']} — 허탕…", ui.LOSE
        if r["leveled"]:
            old_lv = r["level"] - r["leveled"]
            title, color = f"🆙 레벨 업! Lv.{old_lv} → Lv.{r['level']}" + (" · 💎 선수 발굴" if found else ""), ui.GOLD

        e = self._scout_card(user, title, info["line"], color)
        if found:
            e.description += (
                f"\n\n📝 **{found['tier']}** 영입 — **{found['name']}** `#{found['player_id']}`\n"
                f"{found['nation']} · {found['pos']} · OVR **{found['ovr']}** · 잠재 {found['pot_grade']} · "
                f"시세 **{found['price']:,}원**"
            )
        e.description += (
            f"\n\n`정산` **{ui.won(r['delta'])}**\n"
            f"`잔액` **{r['new_bal']:,}원**\n"
            + self._scout_status(r, 3 if info["ok"] else -1)
        )
        if r["leveled"]:
            e.description += (f"\n\n🆙 **{self.scout_tier(r['level'])}** 승급! 보상 {self.scout_money_mult(old_lv)}배 → "
                              f"**{self.scout_money_mult(r['level'])}배** · 선수 발굴 확률과 희귀 선수 비중 상승")
        if r["used"] >= r["limit"]:
            e.description += "\n\n🔓 오늘 스카우트 완료! 이제 `/훈련`을 할 수 있어요."
        e.set_thumbnail(url=ui.emoji_url("💎" if found else ev["emoji"]))
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

    # ✅ 경마: 말 10마리 중 4마리 출주. 말 정보(승률·각질·컨디션)는 분위기용 — 순위는 완전 랜덤.
    # 상금표도 경주마다 랜덤: 1·2위 수익, 3·4위 손실. 수익 합 = 손실 합이라 기대값 0.
    RACE_MIN_BET = 5_000
    RACE_TRACK = 12
    RACE_SECTION = "🎙️ 경마 중계"
    RACE_HORSES = [   # (이모지, 이름, 승률 %, 각질, 한 줄 소개) — 표시용
        ("👑", "킹스로드",   35, "선입", "지난 시즌 챔피언"),
        ("⚡", "번개질주",   31, "선행", "출발 반응은 리그 최고"),
        ("💎", "다이아스텝", 29, "선입", "몸값만 50억"),
        ("🔥", "불꽃심장",   27, "선행", "지는 걸 제일 싫어해요"),
        ("🌪️", "폭풍추격",   24, "추입", "막판 스퍼트가 무섭습니다"),
        ("🌊", "파도타기",   20, "선입", "비 오는 날 유독 강함"),
        ("🌙", "달빛소나타", 18, "자유", "야간 경주만 되면 펄펄"),
        ("🍗", "치킨런",     15, "추입", "당근보다 치킨을 좋아함"),
        ("🍀", "네잎클로버", 12, "추입", "운 하나로 버티는 중"),
        ("🐢", "느긋한오후",  6, "자유", "오늘도 풍경 감상 중"),
    ]
    RACE_MOODS = ("😆 최상", "🙂 좋음", "😐 보통", "😫 나쁨")
    RACE_PLACES = ("🥇", "🥈", "🥉", "4️⃣")
    RACE_CALLS = (   # 장면별 (소제목, 캐스터 멘트) — {0}{1} = 그 장면의 1·2번째 말
        ("출발!", "게이트가 열립니다! {0}, 스타트가 좋아요!"),
        ("3코너", "3코너를 돌아 나옵니다! 선두는 {0}, 바짝 따라붙는 {1}!"),
        ("마지막 직선", "마지막 직선 주로!! {0}, {1}! 치열한 경합입니다!!"),
    )
    RACE_RESULTS = (   # 순위별 (이모지, 헤드라인, 캐스터 멘트)
        ("🏆", "우승!!", "결승선을 가장 먼저 통과합니다! 탁월한 안목이에요!"),
        ("🥈", "2위!", "아깝게 2위! 그래도 상금은 챙겨 갑니다."),
        ("🥉", "3위…", "3위로 들어옵니다. 조금만 더 버텼다면…"),
        ("🐌", "꼴찌…", "마지막으로 들어옵니다… 오늘은 영 컨디션이 아니었네요."),
    )

    @staticmethod
    def _race_prizes(rng=random) -> list[int]:
        """[1위, 2위, 3위, 4위] 순이익 배수. 1위 > 2위 ≥ 1, 4위 손실 > 3위 손실, 수익 합 = 손실 합(3~8)."""
        s = rng.choices(range(3, 9), weights=(25, 25, 18, 14, 10, 8))[0]
        top = rng.randint(s // 2 + 1, s - 1)     # 1위 몫
        worst = rng.randint(s // 2 + 1, s - 1)   # 4위 손실
        return [top, s - top, -(s - worst), -worst]

    @classmethod
    def _race_frames(cls, finish: list[int], rng=random) -> list[list[int]]:
        """장면 3개의 말 위치(출주 번호순). 초반엔 뒤섞이고, 갈수록 최종 순위대로 벌어진다."""
        rank = {h: r for r, h in enumerate(finish)}
        w = cls.RACE_TRACK
        return [[max(0, min(w - 1, round(f * w + (1.5 - rank[i]) * f * 1.4 + rng.uniform(-2, 2) * (1 - f))))
                 for i in range(len(finish))]
                for f in (0.25, 0.55, 0.85)]

    @classmethod
    def _race_lanes(cls, horses, pos: list[int], pick: int) -> str:
        return "\n".join(
            f"`{i + 1}` {h[0]} {ui.bar(p, cls.RACE_TRACK, cls.RACE_TRACK)}🏁 "
            + (f"**{h[1]}** 👈" if i == pick else h[1])
            for i, (h, p) in enumerate(zip(horses, pos)))

    def _race_card(self, user, amount: int, race: dict) -> discord.Embed:
        horses = race["horses"]
        lines = [f"`{i + 1}` {h[0]} **{h[1]}** · 승률 {h[2]}% · {h[3]} · {m}\n　 *{h[4]}*"
                 for i, (h, m) in enumerate(zip(horses, race["moods"]))]
        prize = [f"{p} {i + 1}위 **{ui.won(amount * m)}** · {self._pk_label(m)}"
                 for i, (p, m) in enumerate(zip(self.RACE_PLACES, race["prizes"]))]
        e = ui.card(f"🏇 제{race['no']}경주 출주표",
                    '> 🎙️ *"오늘의 출주마 네 마리입니다! 어느 말에 거시겠어요?"*\n\n' + "\n".join(lines)
                    + f"\n\n**🏆 상금표** · `베팅` **{amount:,}원**\n" + "\n".join(prize),
                    ui.INFO, user, self.RACE_SECTION)
        e.set_thumbnail(url=ui.emoji_url("🏇"))
        e.set_footer(text="60초 안에 아래 버튼으로 말을 골라 주세요")
        return e

    def _race_broke(self, user, amount: int, bal: int) -> discord.Embed:
        return ui.card("🙅 마권을 살 수 없어요",
                       f'> 🎙️ *"잔액이 부족해 매표소에서 돌려보냈습니다!"*\n\n`베팅` **{amount:,}원**\n`잔액` **{bal:,}원**',
                       ui.LOSE, user, self.RACE_SECTION)

    @app_commands.command(name="경마", description="출주마 4마리 중 한 마리에 베팅! 1·2위는 수익, 3·4위는 손실 (최소 5,000원)")
    @app_commands.rename(amount="베팅액")
    @app_commands.describe(amount="베팅 금액 (최소 5,000원)")
    async def horse_race(self, interaction: discord.Interaction, amount: app_commands.Range[int, RACE_MIN_BET]):
        user, amount = interaction.user, int(amount)
        await interaction.response.defer()
        bal = await self.db.get_balance(user.id)
        if bal < amount:
            return await interaction.followup.send(embed=self._race_broke(user, amount, bal))

        horses = random.sample(self.RACE_HORSES, 4)
        race = {"no": random.randint(1, 12), "horses": horses,
                "moods": [random.choice(self.RACE_MOODS) for _ in horses], "prizes": self._race_prizes()}
        view = RaceView(self, user, amount, race)
        view.message = await interaction.followup.send(embed=self._race_card(user, amount, race), view=view, wait=True)

    async def _run_race(self, interaction: discord.Interaction, view, pick: int):
        race, amount, user = view.race, view.amount, view.user
        horses, sec, title = race["horses"], self.RACE_SECTION, f"🏇 제{race['no']}경주"
        finish = random.sample(range(4), 4)   # 순위는 완전 랜덤
        place = finish.index(pick)
        mult = race["prizes"][place]
        delta = amount * mult

        await interaction.response.edit_message(
            embed=ui.card(f"{title} — 출발 대기", f'> 🎙️ *"{horses[pick][1]}에 {amount:,}원! 게이트에 들어갑니다…"*',
                          ui.DARK, user, sec),
            view=None)

        # 결과를 먼저 저장하고 나서 연출한다 (연출이 실패해도 돈은 정확하다).
        try:
            bal = await self.db.get_balance(user.id)
            if bal < amount:   # 고르는 사이 돈을 다른 데 썼다
                return await interaction.edit_original_response(embed=self._race_broke(user, amount, bal))
            new_bal = await self.db.add_balance(user.id, delta)
        except Exception as ex:
            return await interaction.followup.send(f"❌ DB 오류: {type(ex).__name__}")

        try:
            for (stage, call), pos in zip(self.RACE_CALLS, self._race_frames(finish)):
                order = sorted(range(4), key=lambda i: (-pos[i], finish.index(i)))
                await asyncio.sleep(1.2)
                await interaction.edit_original_response(embed=ui.card(
                    f"{title} — {stage}",
                    f'> 🎙️ *"{call.format(horses[order[0]][1], horses[order[1]][1])}"*\n\n'
                    + self._race_lanes(horses, pos, pick),
                    ui.DARK, user, sec))
            await asyncio.sleep(1.2)
        except discord.HTTPException:
            pass

        emoji, headline, caster = self.RACE_RESULTS[place]
        board = "\n".join(f"{self.RACE_PLACES[r]} {horses[i][0]} " + (f"**{horses[i][1]}** 👈" if i == pick else horses[i][1])
                          for r, i in enumerate(finish))
        color = ui.GOLD if place == 0 else (ui.DOOM if mult <= -5 else ui.tone(delta))
        e = ui.card(f"{emoji} {horses[pick][1]} {headline}",
                    f'> 🎙️ *"{caster}"*\n\n{board}\n\n'
                    f"`정산` **{ui.won(delta)}** · {self._pk_label(mult)}\n`잔액` **{new_bal:,}원**",
                    color, user, sec)
        e.set_thumbnail(url=ui.emoji_url(emoji))
        try:
            await interaction.edit_original_response(embed=e)
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
