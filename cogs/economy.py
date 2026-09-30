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

from services.economy_db import (
    ATTEND_BONUS, BANKRUPT_BET_BAN, BANKRUPT_COOLDOWN, EconomyDB, SCOUT_MAX_LEVEL, TRAIN_MAX_LEVEL, TRANSFER_DAILY_LIMIT,
)
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
        # 베팅금은 출주표를 띄울 때 이미 빠져나갔다 — 안 고르면 그대로 잃는다.
        e = ui.card("⌛ 출주 실격", '> 🎙️ *"기수가 끝내 나타나지 않았습니다! 실격 처리됩니다."*'
                    f"\n\n`정산` **{ui.won(-self.amount)}** · 1배 손실", ui.LOSE, self.user, Economy.RACE_SECTION)
        try:
            await self.message.edit(embed=e, view=None)
        except discord.HTTPException:
            pass


class BankruptConfirm(discord.ui.View):
    def __init__(self, cog: "Economy", user):
        super().__init__(timeout=60)
        self.cog, self.user = cog, user

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("🙅 신청한 본인만 누를 수 있어요.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="파산 신청", style=discord.ButtonStyle.danger, emoji="⚖️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        r = await self.cog.db.declare_bankruptcy(self.user.id, int(time.time()))
        if not r["ok"]:
            msg = ("이미 빚이 없어요." if r["reason"] == "not_negative"
                   else f"파산은 {BANKRUPT_COOLDOWN // 86400}일에 한 번만 가능해요. 다음 가능: <t:{r['until']}:R>")
            e = ui.card("❌ 파산 신청 불가", msg, ui.LOSE, self.user, "⚖️ 파산")
        else:
            e = ui.card("⚖️ 파산 처리 완료",
                        f"`빚` **{r['debt']:,}원**\n"
                        f"`선수 카드 정리` {r['cards']}장 → **+{r['cards_value']:,}원**\n"
                        f"`스폰서 원금` **+{r['sponsor']:,}원**\n"
                        f"`탕감` **{r['forgiven']:,}원**\n"
                        f"`잔액` **{r['balance']:,}원**\n\n"
                        f"🚫 베팅 금지 해제 <t:{r['ban_until']}:R> · `/스카우트` `/훈련` `/출석`으로 새 출발!",
                        ui.DOOM, self.user, "⚖️ 파산")
        await interaction.response.edit_message(embed=e, view=None)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            embed=ui.card("파산 신청 취소", "아무것도 바뀌지 않았습니다.", ui.EVEN, self.user, "⚖️ 파산"), view=None)


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
    PK_MIN_BET = 1_000
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
            "success_rate": 0.88,
            "win": (4000, 18000),
            "lose": (-3500, -1000),
            "success_text": "호흡이 안정적으로 잡혔습니다.",
            "fail_text": "무리해서 컨디션이 떨어졌습니다.",
        },
        {
            "name": "드리블 훈련",
            "emoji": "🧠",
            "success_rate": 0.88,
            "win": (4000, 18000),
            "lose": (-3500, -1000),
            "success_text": "수비를 깔끔하게 벗겨냈습니다.",
            "fail_text": "볼을 빼앗겼습니다.",
        },
        {
            "name": "페널티킥 훈련",
            "emoji": "🥅",
            "success_rate": 0.78,
            "win": (8000, 22000),
            "lose": (-5000, -2000),
            "success_text": "연습이지만 아주 깔끔한 골입니다.",
            "fail_text": "골키퍼가 읽었습니다.",
        },
        {
            "name": "야구 타격 훈련",
            "emoji": "⚾",
            "success_rate": 0.78,
            "win": (8000, 22000),
            "lose": (-5000, -2000),
            "success_text": "정타! 타이밍이 맞았습니다.",
            "fail_text": "헛스윙… 타이밍이 늦었습니다.",
        },
        {
            "name": "프리킥 훈련",
            "emoji": "🎯",
            "success_rate": 0.55,
            "win": (12000, 30000),
            "lose": (-7500, -3000),
            "success_text": "환상적인 궤적입니다.",
            "fail_text": "벽에 걸렸습니다.",
        },
        {
            "name": "자유투 훈련",
            "emoji": "🏀",
            "success_rate": 0.78,
            "win": (8000, 22000),
            "lose": (-5000, -2000),
            "success_text": "클린! 림에도 안걸렸습니다.",
            "fail_text": "백보드에 맞고 튕겨져 나옵니다.",
        },
        {
            "name": "샌드백 훈련",
            "emoji": "🥊",
            "success_rate": 0.85,
            "win": (5000, 18000),
            "lose": (-4000, -1500),
            "success_text": "묵직한 타격감! 폼이 완벽합니다.",
            "fail_text": "타이밍이 어긋나 손목을 삐끗했습니다.",
        },
        {
            "name": "스파이크 훈련",
            "emoji": "🏐",
            "success_rate": 0.78,
            "win": (8000, 22000),
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

    ATTEND_REWARD = 30_000

    @staticmethod
    def _attend_track(total: int) -> str:
        """누적 출석 보너스 표: 받은 곳 ✅, 다음 목표 👉."""
        nxt = next((d for d in ATTEND_BONUS if d > total), None)
        cells = []
        for d, bonus in ATTEND_BONUS.items():
            mark = "✅" if total >= d else ("👉" if d == nxt else "▫️")
            cells.append(f"{mark} {d}일 **+{bonus // 10_000:,}만**")
        track = " · ".join(cells)
        if nxt:
            track += f"\n`다음 보너스` **{nxt}일** 까지 {nxt - total}일"
        return track

    @app_commands.command(name="출석", description="하루 1번 출석 보상 · 누적 출석 일수에 따라 보너스 (빠져도 초기화 없음)")
    async def daily(self, interaction: discord.Interaction):
        await interaction.response.defer()
        user, reward = interaction.user, self.ATTEND_REWARD
        try:
            ok, new_bal, remaining, total, bonus = await self.db.claim_daily(user.id, reward, int(time.time()))
        except Exception as ex:
            return await interaction.followup.send(f"❌ DB 오류: {type(ex).__name__}")

        if not ok:
            e = ui.card("⏳ 오늘은 이미 출석했어요",
                        f"`다음 출석` **{_format_time_left(remaining)}** 뒤 (00:00 초기화)\n"
                        f"`누적 출석` **{total}일**\n\n" + self._attend_track(total),
                        ui.EVEN, user, "📅 출석")
            return await interaction.followup.send(embed=e)

        e = ui.card(f"🎁 누적 {total}일 달성 보너스!" if bonus else "✅ 출석 완료",
                    f"`기본` **+{reward:,}원**" + (f"\n`보너스` **+{bonus:,}원**" if bonus else "")
                    + f"\n`잔액` **{new_bal:,}원**\n`누적 출석` **{total}일**\n\n" + self._attend_track(total),
                    ui.GOLD if bonus else ui.WIN, user, "📅 출석")
        await interaction.followup.send(embed=e)

    @app_commands.command(name="송금", description=f"다른 유저에게 돈을 보냅니다. (하루 최대 {TRANSFER_DAILY_LIMIT:,}원)")
    @app_commands.describe(to_user="받을 유저", amount="보낼 금액(1 이상)")
    async def transfer(self, interaction: discord.Interaction, to_user: discord.Member, amount: int):
        await interaction.response.defer()
        user, amount, now_ts = interaction.user, int(amount), int(time.time())
        err = await self.db.transfer(user.id, to_user.id, amount, now_ts)
        if err:
            return await interaction.followup.send(embed=ui.card("❌ 송금 실패", err, ui.LOSE, user, "💸 송금"))

        my_bal = await self.db.get_balance(user.id)
        to_bal = await self.db.get_balance(to_user.id)
        left = await self.db.transfer_remaining(user.id, now_ts)
        e = ui.card("✅ 송금 완료",
                    f"{user.mention} → {to_user.mention}\n\n`금액` **{amount:,}원**\n"
                    f"`내 잔액` **{my_bal:,}원**\n`오늘 남은 한도` **{left:,}원** / {TRANSFER_DAILY_LIMIT:,}원",
                    ui.WIN, user, "💸 송금")
        await interaction.followup.send(embed=e)

        dm_embed = discord.Embed(
            title="💸 송금 수신",
            description=(
                f"**{user.display_name}**님에게서 **{amount:,}원**을 받았습니다.\n"
                f"현재 잔액: **{to_bal:,}원**"
            ),
            color=0x2ecc71,
        )
        await send_notify(self.bot, self.db, to_user.id, "송금_수신", dm_embed)

    # ✅ 파산 신청: 잔액이 마이너스일 때 선수 카드·스폰서 원금으로 갚고 남은 빚 탕감 (30일에 한 번, 이후 3일 베팅 금지)
    async def _bet_ban_card(self, user) -> discord.Embed | None:
        """파산 후 베팅 금지 중이면 안내 카드, 아니면 None."""
        until = await self.db.bet_ban_until(user.id, int(time.time()))
        if not until:
            return None
        return ui.card("⚖️ 파산 후 베팅 금지 기간이에요",
                       f"`해제` <t:{until}:R> · 그동안 `/스카우트` `/훈련` `/출석`으로 다시 일어서 보세요!",
                       ui.DOOM, user, "⚖️ 파산")

    @app_commands.command(name="파산신청", description="잔액이 마이너스일 때: 선수·스폰서 정리 후 남은 빚 탕감 (30일 1회 · 3일 베팅 금지)")
    async def bankruptcy(self, interaction: discord.Interaction):
        user = interaction.user
        bal = await self.db.get_balance(user.id)
        if bal >= 0:
            return await interaction.response.send_message(embed=ui.card(
                "🙅 파산 신청 대상이 아니에요", f"잔액이 마이너스일 때만 신청할 수 있어요.\n`잔액` **{bal:,}원**",
                ui.EVEN, user, "⚖️ 파산"), ephemeral=True)
        e = ui.card("⚖️ 정말 파산 신청할까요?",
                    f"`현재 빚` **{-bal:,}원**\n\n"
                    "1️⃣ 보유 선수 카드를 **전부** 즉시판매가(기준가 50% · 은퇴 30%)로 넘겨 빚을 갚아요 (아마추어 제외)\n"
                    "2️⃣ 진행 중인 스폰서 계약을 모두 해지하고 원금으로 갚아요\n"
                    "3️⃣ 그래도 남은 빚은 **0원으로 탕감**돼요\n"
                    f"4️⃣ 이후 **{BANKRUPT_BET_BAN // 86400}일간** 페널티킥 · 야구 · 경마 · 토토 베팅 금지\n"
                    f"5️⃣ 파산은 **{BANKRUPT_COOLDOWN // 86400}일에 한 번**만 할 수 있어요",
                    ui.DOOM, user, "⚖️ 파산")
        await interaction.response.send_message(embed=e, view=BankruptConfirm(self, user))

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
            base = random.randint(*ev["lose"])
            return base * mult, -1, {"ev": ev, "ok": False, "line": ev["fail_text"], "base": base, "mult": mult}
        base = random.randint(*ev["win"])
        return base * mult, 3, {"ev": ev, "ok": True, "line": ev["success_text"], "base": base, "mult": mult}

    @staticmethod
    def _settle_line(delta: int, info: dict) -> str:
        """`정산` 줄 — 레벨 배율이 붙으면 원금(기본 금액)도 함께 보여준다."""
        line = f"`정산` **{ui.won(delta)}**"
        if info.get("mult", 1) > 1:
            line += f" · 기본 {ui.won(info['base'])} × {info['mult']}배"
        return line

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
            "\n\n" + self._settle_line(r["delta"], info) + "\n"
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
            base = random.randint(*ev["lose"])
            return base * mult, -1, {"ev": ev, "ok": False, "line": ev["fail_text"], "found": None,
                                     "base": base, "mult": mult}
        found = None
        if random.random() < SCOUT_FIND_PROB[min(level, SCOUT_MAX_LEVEL) - 1]:
            found = scout_find_player(con, level)
            if found:
                give_player(con, user_id, found["player_id"])
        base = random.randint(*ev["win"])
        return base * mult, 3, {"ev": ev, "ok": True, "line": ev["success_text"], "found": found,
                                "base": base, "mult": mult}

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
            "\n\n" + self._settle_line(r["delta"], info) + "\n"
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

    @app_commands.command(name="페널티킥", description="돈을 걸고 슛! 최대 200배 수익, 최악은 10배 손실 (최소 1,000원)")
    @app_commands.describe(amount="베팅 금액 (최소 1,000원)")
    async def penalty_kick(self, interaction: discord.Interaction, amount: app_commands.Range[int, PK_MIN_BET]):
        user = interaction.user
        # 도배 방지: 직전 킥에서 1초가 안 지났으면 본인에게만 중계 멘트를 보여주고 끝낸다.
        now = time.monotonic()
        if now - self._pk_last.get(user.id, 0.0) < self.PK_SPAM_GAP:
            title, caster = random.choice(self.PK_SPAM_LINES)
            return await interaction.response.send_message(embed=self._pk_card(user, title, caster, ui.DARK), ephemeral=True)
        self._pk_last[user.id] = now

        await interaction.response.defer()
        if (ban := await self._bet_ban_card(user)):
            return await interaction.followup.send(embed=ban)
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

    # ✅ 한 판 게임(야구 · 농구 · UFC): 페널티킥과 같은 방식 — 결과표에서 한 줄 뽑아 순이익 = 베팅 x 배수.
    # 세 게임 모두 같은 확률 구조(기대값 약 -1.1%)에 연출만 다르다. 각 표의 마지막 줄이 부동소수 잔여 구간을 받는다.
    BAT_MIN_BET = 1_000
    GAME_ODDS = (0.001, 0.005, 0.020, 0.030, 0.070, 0.270, 0.080, 0.182, 0.080, 0.020, 0.003, 0.239)
    GAME_MULTS = ("50", "10", "5", "3", "2", "1", "0", "-1", "-2", "-5", "-10", "-1")
    BAT_SPAM_LINES = [
        ("🧢 타자가 장갑을 고쳐 끼는 중", "타임! 타자가 배터박스를 잠깐 벗어났어요. 곧 다시 섭니다!"),
        ("🤚 투수가 사인을 거부합니다", "포수와 사인이 안 맞네요. 잠시만 기다려 주세요!"),
        ("🧹 심판이 홈플레이트를 쓸고 있어요", "홈플레이트 청소 중입니다. 먼지가 좀 많았네요!"),
    ]
    BAT_TABLE = [   # (확률, 순이익 배수, 이모지, 헤드라인, 캐스터 멘트)
        (0.001, "50",  "🌌", "장외홈런!!!",   "공이… 구장 밖으로 사라집니다!! 비거리 측정 불가!"),
        (0.005, "10",  "🎆", "끝내기 홈런!!", "9회말 투아웃! 이 한 방으로 경기를 끝냅니다!!"),
        (0.020, "5",   "💥", "홈런!",         "넘어갑니다! 담장 밖으로!"),
        (0.030, "3",   "🏃", "3루타!",        "우중간을 가릅니다! 3루까지 전력 질주!"),
        (0.070, "2",   "⚾", "2루타!",        "라인 타고 빠집니다! 여유 있게 2루!"),
        (0.270, "1",   "✅", "안타",          "깔끔한 안타! 1루에 나갑니다."),
        (0.080, "0",   "🚶", "볼넷",          "끝까지 골라냅니다. 걸어서 1루로… 본전이에요."),
        (0.182, "-1",  "🎈", "뜬공",          "높이 떴지만… 중견수가 편하게 잡아냅니다."),
        (0.080, "-2",  "🌀", "삼진",          "헛스윙 삼진! 방망이가 허공을 가릅니다."),
        (0.020, "-5",  "💀", "병살타",        "6-4-3 병살… 찬스가 한순간에 날아갑니다."),
        (0.003, "-10", "☠️", "트리플 플레이", "트리플 플레이?! 한 번에 아웃 세 개… 믿을 수 없는 참사입니다!"),
        (0.239, "-1",  "🐛", "땅볼",          "유격수 앞 땅볼, 1루에서 아웃."),
    ]
    HOOP_SPAM_LINES = [
        ("💦 선수가 땀을 닦는 중", "코트가 미끄러워요! 볼보이가 바닥을 닦고 있습니다."),
        ("⏱️ 작전 타임", "감독이 작전 타임을 불렀어요. 잠시 후 재개합니다!"),
        ("👟 신발 끈이 풀렸어요", "신발 끈 다시 묶는 중! 금방 돌아옵니다."),
    ]
    HOOP_TABLE = [
        (0.001, "50",  "🌠", "하프라인 버저비터!!!", "하프라인에서 던졌는데— 들어갑니다!!! 경기장이 뒤집어졌어요!"),
        (0.005, "10",  "🔔", "역전 버저비터!!",      "종료 부저와 함께 림을 가릅니다! 역전승!!"),
        (0.020, "5",   "💥", "앤드원 덩크!",         "림이 흔들리는 덩크에 파울까지! 앤드원!"),
        (0.030, "3",   "🎯", "3점슛!",               "깨끗한 3점! 그물만 출렁입니다."),
        (0.070, "2",   "🏀", "미드레인지 점퍼",      "풀업 점퍼! 부드럽게 들어갑니다."),
        (0.270, "1",   "✅", "레이업",               "침착하게 레이업 성공!"),
        (0.080, "0",   "🆓", "자유투 1/2",           "파울을 얻었지만 자유투는 하나만… 본전이에요."),
        (0.182, "-1",  "🧱", "림 맞고 아웃",         "림을 돌다가… 튕겨 나옵니다."),
        (0.080, "-2",  "🚫", "블록슛",               "쳐냈습니다! 관중석까지 날아간 블록슛!"),
        (0.020, "-5",  "💨", "스틸 → 속공 실점",     "공을 뺏기고 그대로 속공 덩크 허용… 흐름이 넘어갑니다."),
        (0.003, "-10", "☠️", "에어볼 + 테크니컬",    "에어볼에 항의하다 테크니컬 파울까지… 최악의 한 수입니다!"),
        (0.239, "-1",  "🙅", "슛 실패",              "슛이 짧았어요. 리바운드는 상대 차지."),
    ]
    UFC_SPAM_LINES = [
        ("🩹 컷맨이 상처를 막는 중", "코너에서 지혈 중이에요. 잠시만 기다려 주세요!"),
        ("🧑‍⚖️ 주심이 글러브를 점검합니다", "글러브 점검 중! 곧 다시 시작합니다."),
        ("🥤 라운드 사이 휴식", "1분 휴식 시간입니다. 숨 고르고 다시 가죠!"),
    ]
    UFC_TABLE = [
        (0.001, "50",  "👑", "플라잉 니킥 KO!!!",  "플라잉 니킥 한 방에 경기가 끝났습니다!!! 올해의 KO 확정!"),
        (0.005, "10",  "🥊", "1라운드 KO승!!",     "시작 30초 만에 카운터 한 방! 상대가 그대로 쓰러집니다!"),
        (0.020, "5",   "🔒", "서브미션승!",        "리어네이키드 초크! 상대가 탭을 칩니다!"),
        (0.030, "3",   "🩸", "TKO승!",            "파운딩 세례에 주심이 경기를 멈춥니다!"),
        (0.070, "2",   "📋", "만장일치 판정승",    "세 명의 심판 모두 당신 손을 들어줍니다!"),
        (0.270, "1",   "✅", "스플릿 판정승",      "아슬아슬했지만… 2대 1 판정승!"),
        (0.080, "0",   "🤝", "무승부",             "치열한 5라운드 끝에 무승부. 본전이에요."),
        (0.182, "-1",  "📉", "스플릿 판정패",      "아쉽게도 1대 2 판정패…"),
        (0.080, "-2",  "😵", "TKO패",              "연타를 허용하고 주심이 경기를 중단합니다…"),
        (0.020, "-5",  "💤", "KO패",               "카운터에 정통으로… 캔버스에 누워 버렸습니다."),
        (0.003, "-10", "☠️", "실신 KO + 부상",     "하이킥에 실신… 병원비까지 청구됩니다!"),
        (0.239, "-1",  "❌", "만장일치 판정패",    "끝까지 버텼지만 판정은 상대 편이었습니다."),
    ]

    @staticmethod
    def _caster_card(user, title: str, caster: str, color: int, section: str) -> discord.Embed:
        return ui.card(title, f"> 🎙️ *\"{caster}\"*", color, user, section)

    async def _play_table(self, interaction: discord.Interaction, amount: int, *, key: str, table, section: str,
                          spam_lines, broke: tuple, windup: tuple):
        """한 판 게임 공통: 도배 방지 → 베팅 금지·잔액 확인 → 결과 뽑기 → 저장 → 연출 한 장면 → 결과."""
        user = interaction.user
        now = time.monotonic()
        if now - self._pk_last.get((key, user.id), 0.0) < self.PK_SPAM_GAP:
            title, caster = random.choice(spam_lines)
            return await interaction.response.send_message(
                embed=self._caster_card(user, title, caster, ui.DARK, section), ephemeral=True)
        self._pk_last[(key, user.id)] = now

        await interaction.response.defer()
        if (ban := await self._bet_ban_card(user)):
            return await interaction.followup.send(embed=ban)
        amount = int(amount)
        cur_bal = await self.db.get_balance(user.id)
        if cur_bal < amount:
            e = self._caster_card(user, *broke, ui.LOSE, section)
            e.description += f"\n\n`베팅` **{amount:,}원**\n`잔액` **{cur_bal:,}원**"
            return await interaction.followup.send(embed=e)

        roll, acc = random.random(), 0.0
        for prob, mult_s, emoji, headline, caster in table:
            acc += prob
            if roll < acc:
                break
        mult = Fraction(mult_s)
        delta = int(amount * mult)
        try:
            new_bal = await self.db.add_balance(user.id, delta)
        except Exception as ex:
            return await interaction.followup.send(f"❌ DB 오류: {type(ex).__name__}")

        color = ui.GOLD if mult >= 5 else (ui.DOOM if mult <= -5 else ui.tone(delta))
        e = self._caster_card(user, f"{emoji} {headline}", caster, color, section)
        e.description += f"\n\n`정산` **{ui.won(delta)}** · {self._pk_label(mult)}\n`잔액` **{new_bal:,}원**"
        e.set_thumbnail(url=ui.emoji_url(emoji))
        try:
            intro = self._caster_card(user, windup[0], windup[1].format(amount=f"{amount:,}"), ui.DARK, section)
            msg = await interaction.followup.send(embed=intro, wait=True)
            await asyncio.sleep(1.2)
            await msg.edit(embed=e)
        except discord.HTTPException:
            await interaction.followup.send(embed=e)

    @app_commands.command(name="야구", description="타석에 서서 한 방! 장외홈런 50배 수익, 트리플 플레이 10배 손실 (최소 1,000원)")
    @app_commands.rename(amount="베팅액")
    @app_commands.describe(amount="베팅 금액 (최소 1,000원)")
    async def batting(self, interaction: discord.Interaction, amount: app_commands.Range[int, BAT_MIN_BET]):
        await self._play_table(interaction, amount, key="bat", table=self.BAT_TABLE, section="🎙️ 야구 중계",
                               spam_lines=self.BAT_SPAM_LINES,
                               broke=("🙅 타석에 설 수 없어요", "잔액이 부족해 대기 타석에서 돌아갑니다!"),
                               windup=("⚾ 투수, 와인드업…", "{amount}원이 걸린 한 타석! 던졌습니다—"))

    @app_commands.command(name="농구", description="마지막 슛 한 방! 하프라인 버저비터 50배 수익, 에어볼 10배 손실 (최소 1,000원)")
    @app_commands.rename(amount="베팅액")
    @app_commands.describe(amount="베팅 금액 (최소 1,000원)")
    async def basketball(self, interaction: discord.Interaction, amount: app_commands.Range[int, BAT_MIN_BET]):
        await self._play_table(interaction, amount, key="hoop", table=self.HOOP_TABLE, section="🎙️ 농구 중계",
                               spam_lines=self.HOOP_SPAM_LINES,
                               broke=("🙅 코트에 들어갈 수 없어요", "잔액이 부족해 벤치로 돌아갑니다!"),
                               windup=("🏀 공을 잡았습니다…", "{amount}원이 걸린 마지막 공격! 슛—"))

    @app_commands.command(name="ufc", description="옥타곤 한 판! 플라잉 니킥 KO 50배 수익, 실신 KO 10배 손실 (최소 1,000원)")
    @app_commands.rename(amount="베팅액")
    @app_commands.describe(amount="베팅 금액 (최소 1,000원)")
    async def ufc_fight(self, interaction: discord.Interaction, amount: app_commands.Range[int, BAT_MIN_BET]):
        await self._play_table(interaction, amount, key="ufc", table=self.UFC_TABLE, section="🎙️ UFC 중계",
                               spam_lines=self.UFC_SPAM_LINES,
                               broke=("🙅 옥타곤에 오를 수 없어요", "잔액이 부족해 계체량에서 탈락했습니다!"),
                               windup=("🥊 옥타곤 입장…", "{amount}원이 걸린 한 판! 공이 울립니다—"))

    # ✅ 경마: 말 10마리 중 4마리 출주. 말 정보(승률·각질·컨디션)는 분위기용 — 순위는 완전 랜덤.
    # 상금표도 경주마다 랜덤: 1·2위 수익, 3·4위 손실. 수익 합 = 손실 합이라 기대값 0.
    # 베팅금은 출주표를 띄울 때 먼저 빠져나간다 — 상금표만 보고 안 고르면(60초) 베팅금을 잃는다.
    RACE_MIN_BET = 1_000
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
    # 장면별 (소제목, 캐스터 멘트) — {0}{1} = 그 장면의 1·2번째 말. 마지막 두 장면은 역전 여부에 따라 멘트가 갈린다.
    RACE_STAGES = (0.2, 0.4, 0.6, 0.75, 0.88, 1.0)
    RACE_CALLS = (
        ("출발!", "게이트가 열립니다! {0}, 스타트가 좋아요!"),
        ("1코너", "1코너! {0} 선두, {1} 바짝 추격!"),
        ("백스트레치", "백스트레치! 선두는 여전히 {0}!"),
        ("4코너", "4코너를 돌아 나옵니다! {0}, {1}! 치열합니다!"),
        ("마지막 직선", "마지막 직선 주로!! {0}, 이대로 들어가나요?!"),
        ("결승선", "{0}, 그대로 결승선 통과!!"),
    )
    RACE_COMEBACK_CALL = "아아— {0}!!! 바깥쪽에서 무섭게 치고 올라옵니다!! {1}를 제치고 대역전!!!"
    RACE_COMEBACK_PROB = 0.35   # 선두를 달리던 말이 막판에 뒤집히는 경주 비율 (결과 확률과는 무관)
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
    def _race_frames(cls, finish: list[int], rng=random, comeback: int | None = None) -> list[list[int]]:
        """장면마다 말 위치(출주 번호순). 초반엔 뒤섞이고, 갈수록 최종 순위대로 벌어진다.
        comeback = 막판까지 선두를 달리다 뒤집히는 말 — 우승마는 뒤에서 기다렸다가 결승선 직전에 치고 나온다.
        마지막 장면은 결승선: 우승마가 🏁 에 닿고 나머지는 순위대로 한 칸씩 뒤."""
        rank = {h: r for r, h in enumerate(finish)}
        w, frames = cls.RACE_TRACK, []
        for f in cls.RACE_STAGES[:-1]:
            row = []
            for i in range(len(finish)):
                bias = (1.5 - rank[i]) * f * 1.4
                if comeback is not None and i == comeback:
                    bias = 3.0 * f / 0.88            # 가짜 선두: 4코너 · 마지막 직선까지 확실히 앞선다
                elif comeback is not None and i == finish[0]:
                    bias = -0.6 * f                  # 우승마: 뒤에서 힘을 아낀다
                # 결승선 장면을 위해 세 칸은 남겨 둔다 (마지막에 한꺼번에 치고 들어오는 느낌)
                row.append(max(0, min(w - 1, round(f * (w - 3) + bias + rng.uniform(-1.5, 1.5) * (1 - f)))))
            frames.append(row)
        frames.append([w - rank[i] for i in range(len(finish))])
        return frames

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
        e.set_footer(text="60초 안에 말을 고르지 않으면 실격 — 베팅금은 돌려받지 못합니다")
        return e

    def _race_broke(self, user, amount: int, bal: int) -> discord.Embed:
        return ui.card("🙅 마권을 살 수 없어요",
                       f'> 🎙️ *"잔액이 부족해 매표소에서 돌려보냈습니다!"*\n\n`베팅` **{amount:,}원**\n`잔액` **{bal:,}원**',
                       ui.LOSE, user, self.RACE_SECTION)

    @app_commands.command(name="경마", description="출주마 4마리 중 한 마리에 베팅! 1·2위는 수익, 3·4위는 손실 (최소 1,000원)")
    @app_commands.rename(amount="베팅액")
    @app_commands.describe(amount="베팅 금액 (최소 1,000원)")
    async def horse_race(self, interaction: discord.Interaction, amount: app_commands.Range[int, RACE_MIN_BET]):
        user, amount = interaction.user, int(amount)
        await interaction.response.defer()
        if (ban := await self._bet_ban_card(user)):
            return await interaction.followup.send(embed=ban)
        bal = await self.db.get_balance(user.id)
        if bal < amount:
            return await interaction.followup.send(embed=self._race_broke(user, amount, bal))

        horses = random.sample(self.RACE_HORSES, 4)
        race = {"no": random.randint(1, 12), "horses": horses,
                "moods": [random.choice(self.RACE_MOODS) for _ in horses], "prizes": self._race_prizes()}
        await self.db.add_balance(user.id, -amount)   # 마권 발매: 베팅금 먼저 차감
        view = RaceView(self, user, amount, race)
        try:
            view.message = await interaction.followup.send(embed=self._race_card(user, amount, race), view=view, wait=True)
        except discord.HTTPException:
            await self.db.add_balance(user.id, amount)   # 출주표를 못 띄웠으면 환불
            raise

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
        # 베팅금은 이미 빠져 있으니 돌려주면서 순이익을 더한다 → 전체 변동 = delta.
        try:
            new_bal = await self.db.add_balance(user.id, amount + delta)
        except Exception as ex:
            return await interaction.followup.send(f"❌ DB 오류: {type(ex).__name__}")

        # 연출용 역전극: 결과(finish)는 이미 정해졌고, 누가 막판까지 앞서 보일지만 고른다.
        comeback = random.choice(finish[1:]) if random.random() < self.RACE_COMEBACK_PROB else None
        try:
            frames = self._race_frames(finish, comeback=comeback)
            for k, ((stage, call), pos) in enumerate(zip(self.RACE_CALLS, frames)):
                order = sorted(range(4), key=lambda i: (-pos[i], finish.index(i)))
                if k == len(frames) - 1 and comeback is not None:
                    stage, call = "🔥 대역전!!", self.RACE_COMEBACK_CALL
                    names = (horses[finish[0]][1], horses[comeback][1])
                else:
                    names = (horses[order[0]][1], horses[order[1]][1])
                last = k >= len(frames) - 2
                await asyncio.sleep(1.5 if last else 1.1)   # 막판은 한 박자 늦게 — 긴장감
                await interaction.edit_original_response(embed=ui.card(
                    f"{title} — {stage}",
                    f'> 🎙️ *"{call.format(*names)}"*\n\n' + self._race_lanes(horses, pos, pick),
                    ui.GOLD if (k == len(frames) - 1 and comeback is not None) else ui.DARK, user, sec))
            await asyncio.sleep(1.5)
        except discord.HTTPException:
            pass

        emoji, headline, caster = self.RACE_RESULTS[place]
        if comeback is not None and pick == finish[0]:
            headline, caster = "역전 우승!!!", "끝까지 기다렸다가 결승선 앞에서 뒤집었습니다! 이게 경마죠!!"
        elif comeback is not None and pick == comeback:
            caster = "다 잡은 우승을 결승선 앞에서 놓쳤습니다… 너무 아쉬워요!"
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
