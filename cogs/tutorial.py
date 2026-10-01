# cogs/tutorial.py — 단계별 안내 + 오늘의 체크리스트 + 목차 메뉴로 바로 이동
import time

import discord
from discord import app_commands
from discord.ext import commands

from services import ui
from services.economy_db import SCOUT_DAILY_LIMIT, TRAIN_DAILY_LIMIT, WATCH_DAILY_LIMIT, EconomyDB

SECTION = "📘 튜토리얼"

# (목차 이모지, 제목, 본문). 첫 장(체크리스트)은 유저마다 내용이 달라서 따로 만든다.
TUTORIAL_STEPS = [
    ("📋", "오늘의 체크리스트", None),
    ("🏟️", "구단 만들기", (
        "모든 콘텐츠는 **구단**에서 시작합니다.\n\n"
        "`/구단생성 이름:` — 창단 보너스 **50,000원** + 아마추어 스쿼드 18명\n"
        "`/구단` — 포메이션 · 선발 명단 · 전력 보기\n"
        "`/포메이션` `/선발` `/자동편성` `/주장` — 팀 꾸미기\n"
        "`/감독` — 감독 영입으로 전력 보너스 (선호 포메이션이면 추가)\n\n"
        "💡 처음엔 `/자동편성` 한 번이면 충분해요."
    )),
    ("💰", "하루 루틴으로 돈 벌기", (
        "매일 이 순서대로 하면 가장 많이 법니다.\n\n"
        "1️⃣ `/출석` — 하루 1번 · 누적 7 · 14 · 30 · 50 · 100일… 보너스 (빠져도 초기화 없음)\n"
        f"2️⃣ `/스카우트` — 하루 {SCOUT_DAILY_LIMIT}회 · 드물게 **실제 선수** 발굴\n"
        f"3️⃣ `/훈련` — 스카우트를 다 하면 열림 · 하루 {TRAIN_DAILY_LIMIT}회\n"
        f"4️⃣ `/직관` — 훈련을 다 하면 열림 · 쿨타임 10초 · 하루 {WATCH_DAILY_LIMIT}회\n"
        "　관람 80% (돈 +) · 🎉 이벤트 15% (아이템) · 실패 5% (돈 -)\n\n"
        "레벨이 오르면 보상이 **레벨 배**가 됩니다 (Lv.3 = 3배)."
    )),
    ("🎒", "아이템", (
        "🧣 머플러는 `/직관` 경기장 이벤트 · `/상점`, 리셋권 · 스킵권은 `/쿠폰` 같은 이벤트로 얻어요.\n\n"
        "🧣 **응원 머플러** — 다음 5경기 동안 구단 전력 +3\n"
        f"🧳 **스카우트 리셋권** — 오늘 스카우트 +{SCOUT_DAILY_LIMIT}회\n"
        f"🔄 **훈련 리셋권** — 오늘 훈련 +{TRAIN_DAILY_LIMIT}회\n"
        f"🎟️ **직관 리셋권** — 오늘 직관 +{WATCH_DAILY_LIMIT}회\n"
        "🛫 ⏩ 📺 **스카우트 · 훈련 · 직관 스킵권** — 오늘 남은 횟수를 한 번에 끝내고 결과(+/-)를 그대로\n"
        "🧾 **토토 용지** — `/직관` 길에서 줍는 용지 · 가지면 3배 / 그대로 / 불법 -2배, 신고하면 포상금 + 경험치\n\n"
        "`/가방` — 나만 보이는 가방 · 버튼으로 바로 사용 (사용 결과는 모두에게 보여요)\n"
        "💸 `/상점`에서 **판매** — 원가의 50% (리셋권 · 스킵권 · 토토 용지는 사고팔 수 없어요)"
    )),
    ("🃏", "선수 얻기", (
        "`/상점` — 메뉴에서 선수팩을 고르고 **몇 장**(1~10) 살지 선택\n"
        "　브론즈 ~ 얼티밋, 그리고 **포지션 팩**(공격수 · 미드필더 · 수비수 · 골키퍼)\n"
        "`/팩정보` — 팩별 가격대 · 잭팟 · 남은 선수 수\n"
        "`/선수` — 이름 · 국적 · 포지션 · #번호 검색, 키 · 몸무게 · 주발 · 잠재력까지\n\n"
        "💡 처음엔 브론즈 ~ 실버팩으로 시작해 보세요."
    )),
    ("📈", "선수 시장", (
        "선수 가격은 **10분마다** 움직여요. (거래 시간 09:00 ~ 23:00)\n\n"
        "`/시장` `/시세` — 가격 · 그래프\n"
        "`/판매` → 12시간 뒤 안 팔리면 `/매각`(70%)\n"
        "`/즉시판매` — 바로 50% (구단 선발 선수는 1장 남겨야 해요)\n"
        "`/이적시장` `/구매` — 다른 유저 매물\n"
        "`/트레이드` — 유저끼리 선수 · 돈 교환"
    )),
    ("⚽", "경기", (
        "`/친선경기 상대:` — 90분 문자중계 · 돈 없이 전적만 · 🔁 다시 붙기\n"
        "`/공식경기 베팅액: 예측: 상대:` — 돈을 걸고 승/무/패 예측 · 전력으로 정한 **배당** · 횟수 제한 없음\n"
        "　(상대를 비우면 비슷한 전력 구단과 자동 매칭)\n"
        "`/공식순위` — 이번 달 시즌 순위 (승 3점 · 무 1점)\n\n"
        "킥오프 때 **예상 승률**이 나와요. 머플러를 쓰면 전력 +3!"
    )),
    ("🎲", "미니게임", (
        "모두 최소 **1,000원** · 결과는 완전 랜덤이에요.\n\n"
        "`/페널티킥` — 200배 파넨카 ~ 10배 손실\n"
        "`/야구` — 50배 ~ 10배 손실 · `/농구` — 30배 ~ 8배 손실 · `/ufc` — 100배 ~ 20배 손실\n"
        "`/경마` — 4마리 중 한 마리 · 막판 대역전 연출\n"
        "`/리그` — 내 구단을 랜덤 리그에 넣고 한 시즌 · 우승 20배 ~ 강등 6배 손실\n\n"
        "⚠️ 큰 손실은 잔액이 마이너스가 될 수 있어요."
    )),
    ("🤝", "스폰서 · 토토", (
        "`/스폰서` `/스폰서계약` — 돈을 맡기고 기간이 끝나면 수익과 함께 (1 · 7 · 30일)\n"
        "　길게 맡길수록 유리 · 스폰서마다 위험도가 달라요 · 계약 관리는 `/스폰서` 메뉴\n\n"
        "`/토토` — 축구 · UFC 실제 경기 베팅 (메뉴 → 버튼 → 금액)\n"
        "`/내베팅` — 내역 · 경기 전 취소"
    )),
    ("🆘", "도움말", (
        "`/지갑` — 잔액 보기\n"
        "`/송금` — 하루 최대 100,000,000원\n"
        "`/알림설정` — 판매 · 정산 · 스폰서 만기 등 DM 알림\n"
        "`/쿠폰 코드:` — 쿠폰 코드로 보상 받기\n"
        "`/파산신청` — 잔액이 마이너스일 때 스폰서를 강제 해지하고 빚 30~70% 랜덤 탕감 (한 시간에 1회)\n\n"
        "언제든 `/튜토리얼`로 다시 볼 수 있어요."
    )),
]


def _check(done: bool, text: str) -> str:
    return f"{'✅' if done else '⬜'} {text}"


async def checklist_embed(db: EconomyDB, user) -> discord.Embed:
    s = await db.today_status(user.id, int(time.time()))
    lines = [
        _check(s["club"], "구단 만들기 — `/구단생성`"),
        _check(s["attended"], "오늘 출석 — `/출석`"),
        _check(s["scouting"] >= SCOUT_DAILY_LIMIT, f"스카우트 **{s['scouting']}/{SCOUT_DAILY_LIMIT}** — `/스카우트`"),
        _check(s["training"] >= TRAIN_DAILY_LIMIT, f"훈련 **{s['training']}/{TRAIN_DAILY_LIMIT}** — `/훈련`"),
        _check(s["spectating"] >= WATCH_DAILY_LIMIT, f"직관 **{s['spectating']}/{WATCH_DAILY_LIMIT}** — `/직관`"),
    ]
    done = sum(line.startswith("✅") for line in lines)
    e = ui.card(f"📋 오늘의 체크리스트 · {done}/{len(lines)}",
                f"{ui.bar(done, len(lines))}\n\n" + "\n".join(lines)
                + "\n\n아래 **목차**에서 궁금한 기능으로 바로 이동할 수 있어요.",
                ui.GOLD if done == len(lines) else ui.INFO, user, SECTION)
    return e


class TutorialView(discord.ui.View):
    def __init__(self, db: EconomyDB, user, step: int = 0):
        super().__init__(timeout=600)
        self.db, self.user, self.step = db, user, step
        menu = discord.ui.Select(placeholder="📚 목차 — 원하는 장으로 바로 이동", row=0, options=[
            discord.SelectOption(label=f"{i}. {title}", value=str(i), emoji=emoji, default=(i == step))
            for i, (emoji, title, _) in enumerate(TUTORIAL_STEPS)])
        menu.callback = self._jump
        self.menu = menu
        self.add_item(menu)
        self.prev_btn.disabled = step == 0
        self.next_btn.disabled = step == len(TUTORIAL_STEPS) - 1

    async def make_embed(self) -> discord.Embed:
        emoji, title, body = TUTORIAL_STEPS[self.step]
        if body is None:
            e = await checklist_embed(self.db, self.user)
        else:
            e = ui.card(f"{emoji} {title}", body, ui.INFO, self.user, SECTION)
        e.set_footer(text=f"{self.step + 1} / {len(TUTORIAL_STEPS)} · ◀ ▶ 로 넘기거나 목차에서 바로 이동")
        return e

    async def _show(self, interaction: discord.Interaction, step: int):
        view = TutorialView(self.db, self.user, step)
        await interaction.response.edit_message(embed=await view.make_embed(), view=view)

    async def _jump(self, interaction: discord.Interaction):
        await self._show(interaction, int(self.menu.values[0]))

    @discord.ui.button(label="◀ 이전", style=discord.ButtonStyle.secondary, row=1)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show(interaction, max(0, self.step - 1))

    @discord.ui.button(label="다음 ▶", style=discord.ButtonStyle.primary, row=1)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show(interaction, min(len(TUTORIAL_STEPS) - 1, self.step + 1))


class Tutorial(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = EconomyDB()

    @app_commands.command(name="튜토리얼", description="오늘 할 일 체크리스트와 기능별 안내 (목차로 바로 이동)")
    async def tutorial(self, interaction: discord.Interaction):
        view = TutorialView(self.db, interaction.user)
        await interaction.response.send_message(embed=await view.make_embed(), view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Tutorial(bot))
