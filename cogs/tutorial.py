# cogs/tutorial.py — 단계별 안내 + 오늘의 체크리스트 + 목차 메뉴로 바로 이동
# 2.8: 2.8 이 적용된 서버에서는 새 유저가 먼저 🎓 입문 튜토리얼(실습하며 약 10분)을 끝내야 한다 (services/onboarding.py).
import random
import time

import discord
from discord import app_commands
from discord.ext import commands

import release
from services import onboarding as OB
from services import ui
from services.club_db import (
    PROSPECT_DAILY_GROWTH, PROSPECT_PRICE, PROSPECT_PRIME_END, PROSPECT_RETIRE_AGE, PROSPECT_YEAR,
)
from services.economy_db import (
    ITEMS, ROOKIE_DAYS, ROOKIE_GRIND_MULT, SCOUT_DAILY_LIMIT, TRAIN_DAILY_LIMIT, WATCH_DAILY_LIMIT, EconomyDB,
)

SECTION = "📘 튜토리얼"

# (목차 이모지, 제목, 본문). 첫 장(체크리스트)은 유저마다 내용이 달라서 따로 만든다.
TUTORIAL_STEPS = [
    ("📋", "오늘의 체크리스트", None),
    ("🏟️", "구단 만들기", (
        "모든 콘텐츠는 **구단**에서 시작합니다.\n\n"
        "`/구단생성 이름:` — 창단 보너스 **50,000원** + 아마추어 스쿼드 18명\n"
        "`/구단` — 포메이션 · 선발 명단 · 전력 보기\n"
        "`/포메이션` `/선발` `/자동편성` `/주장` — 팀 꾸미기\n"
        "`/감독` — 감독 영입으로 전력 보너스 (선호 포메이션이면 추가)\n"
        "`/시설` — 경기장 · 훈련장 · 유스 아카데미 · 메디컬 센터 업그레이드\n"
        "`/구단꾸미기` — 엠블럼 · 경기장 이름\n"
        "`/명문구단` `/스쿼드` — 명문 구단을 인수해 스쿼드 B로 경기 · 매일 수입\n\n"
        "💡 처음엔 `/자동편성` 한 번이면 충분해요."
    )),
    ("💰", "하루 루틴으로 돈 벌기", (
        "매일 이 순서대로 하면 가장 많이 법니다.\n\n"
        "1️⃣ `/출석` — 하루 1번 · 누적 7 · 14 · 30 · 50 · 100일… 보너스 (빠져도 초기화 없음)\n"
        f"2️⃣ `/스카우트` — 하루 {SCOUT_DAILY_LIMIT}회 · 드물게 **실제 선수** 발굴\n"
        f"3️⃣ `/훈련` — 스카우트를 다 하면 열림 · 하루 {TRAIN_DAILY_LIMIT}회\n"
        f"4️⃣ `/직관` — 훈련을 다 하면 열림 · 쿨타임 10초 · 하루 {WATCH_DAILY_LIMIT}회\n"
        "　관람 65% (돈 +) · 실패 25% (돈 -) · 🎉 이벤트 10% (아이템)\n\n"
        "레벨이 오르면 보상이 **레벨 배**가 됩니다 (Lv.3 = 3배)."
    )),
    ("🎒", "아이템", (
        "🧣 머플러는 `/직관` 경기장 이벤트 · `/아이템상점`, 리셋권 · 스킵권은 `/쿠폰` 같은 이벤트로 얻어요.\n\n"
        "🧣 **응원 머플러** — 다음 5경기 동안 구단 전력 +3\n"
        f"🧳 **스카우트 리셋권** — 오늘 스카우트 +{SCOUT_DAILY_LIMIT}회\n"
        f"🔄 **훈련 리셋권** — 오늘 훈련 +{TRAIN_DAILY_LIMIT}회\n"
        f"🎟️ **직관 리셋권** — 오늘 직관 +{WATCH_DAILY_LIMIT}회\n"
        "🛫 ⏩ 📺 **스카우트 · 훈련 · 직관 스킵권** — 오늘 남은 횟수를 한 번에 끝내고 결과(+/-)를 그대로\n"
        "🧾 **토토 용지** — `/직관` 길에서 줍는 용지 · 가지면 3배 / 그대로 / 불법 -2배, 신고하면 포상금 + 경험치\n"
        "💉 **스테로이드 주사기** — `/아이템상점` 3,000만원(하루 3개) · `/직관` 이벤트 · **내 유망주**에게만 주사\n"
        "　→ OVR · 잠재력 상승… 또는 약물 검출 · 은퇴\n\n"
        "`/가방` — 나만 보이는 가방 · 버튼으로 바로 사용 (사용 결과는 모두에게 보여요)\n"
        "💸 `/아이템상점`에서 **판매** — 원가의 50% · 머플러 · 스테로이드만 (리셋권 · 스킵권 · 토토 용지는 사고팔 수 없어요)"
    )),
    ("🃏", "선수 얻기", (
        "`/선수팩상점` — 메뉴에서 선수팩을 고르고 **몇 장**(1~10) 살지 선택\n"
        "　브론즈 ~ 얼티밋, 그리고 **포지션 팩**(공격수 · 미드필더 · 수비수 · 골키퍼)\n"
        "`/팩정보` — 팩별 가격대 · 잭팟 · 남은 선수 수\n"
        "`/선수` — 이름 · 국적 · 포지션 · #번호 검색, 키 · 몸무게 · 주발 · 잠재력까지\n\n"
        "💡 처음엔 브론즈 ~ 실버팩으로 시작해 보세요."
    )),
    ("🌟", "유망주", (
        f"`/유망주생성` — **{PROSPECT_PRICE:,}원**으로 나만의 선수를 만들어요\n"
        "　이름 · 국적 · 포지션 · 등번호 · 생일 · 주발 · 키를 직접 정하고, 능력치는 17세 · OVR 50~58 · 잠재력 75~94 랜덤\n"
        "　🔒 이적시장 · 판매 · 트레이드 불가 · 한 번에 한 명\n\n"
        "`/선발`로 내 구단에 넣고 `/친선경기` `/공식경기`에 내보내면 **출전 · 골 · 도움**이 쌓이고 성장해요\n"
        f"　(하루 {PROSPECT_DAILY_GROWTH}경기까지 · 잠재력까지 · 💉 스테로이드는 유망주에게만)\n"
        f"🕰️ 1살 = 실제 {PROSPECT_YEAR // 86400}일 · {PROSPECT_PRIME_END + 1}세부터 노쇠 · {PROSPECT_RETIRE_AGE}세 은퇴\n"
        "🔒 히든 능력치(자신감 · 부상 빈도 · 프로 의식) · 🚑 경기 중 부상 — `/의료진`으로 결장 기간 단축\n\n"
        "`/유망주` — 세부 능력치 · 커리어 · 명예의 전당\n"
        "`/유망주은퇴` — 전성기 커리어로 저장 · 🏅 등번호 영구결번 (`/영구결번`)\n"
        "`/유망주삭제` — 기록 없이 삭제 (환불 없음)"
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
        "`/송금` — 하루 최대 100,000,000원 (새 유저는 출석 7일 · 공식경기 10판 · 체크리스트 완료 · 루키 미션 8개 후)\n"
        "`/알림설정` — 판매 · 정산 · 스폰서 만기 등 DM 알림\n"
        "`/쿠폰 코드:` — 쿠폰 코드로 보상 받기\n"
        "`/파산신청` — 잔액이 마이너스일 때 스폰서를 강제 해지하고 빚 30~70% 랜덤 탕감 (한 시간에 1회)\n\n"
        "언제든 `/튜토리얼`로 다시 볼 수 있어요."
    )),
]

# 2.8: 아이템 장을 한 줄에 효과 하나로 줄이고, 토토 용지 · 판매 규칙은 다음 장으로 (2.8 서버에서만)
ITEMS_28 = ("🎒", "아이템", (
    "🧣 **응원 머플러** — 다음 5경기 전력 +3\n"
    "🧳 🔄 🎟️ **리셋권** — 오늘 스카우트 · 훈련 · 직관 횟수를 다시\n"
    "🛫 ⏩ 📺 **스킵권** — 오늘 남은 횟수를 한 번에 끝내기\n"
    "💉 **스테로이드 주사기** — 내 유망주 OVR · 잠재력 ↑ (검출되면 은퇴)\n"
    "🎁 **점검 보상 상자** — 카드 한 장을 골라 스킵권 · 리셋권\n"
    "🎣 **낚시** — 미끼 · 낚싯대 · 아이스박스 · 부적 · 양념\n"
    "🏰 **던전** — 횃불 · 포션 · 해독제 · 열쇠 · 두루마리 · 부활 깃털 · 추가 입장권\n"
    "🏹 **양궁** — 고급 활 · 바람막이 · 집중 물약\n\n"
    "📍 얻는 곳: `/아이템상점` · 이벤트 · `/쿠폰`\n"
    "`/가방`에서 버튼으로 바로 써요."
))
SLIP_28 = ("🧾", "토토 용지 · 판매", (
    "🧾 **토토 용지** — `/직관` 길에서 주워요\n"
    "　가지면 3배 / 그대로 / 불법 -2배 · 신고하면 포상금 + 경험치\n\n"
    "💸 **판매** — `/아이템상점`에서 원가의 50%\n"
    "　팔 수 있는 것: 머플러 · 스테로이드\n"
    "　팔 수 없는 것: 리셋권 · 스킵권 · 토토 용지 · 낚시 · 던전 · 양궁 아이템"
))
TUTORIAL_STEPS_28 = [x for st in TUTORIAL_STEPS for x in ((ITEMS_28, SLIP_28) if st[1] == "아이템" else (st,))]


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
    if done == len(lines):   # 체크리스트를 다 채운 날 = 튜토리얼 완료 (2.5 송금 조건)
        await db.mark_tutorial(user.id, int(time.time()))
    e = ui.card(f"📋 오늘의 체크리스트 · {done}/{len(lines)}",
                f"{ui.bar(done, len(lines))}\n\n" + "\n".join(lines)
                + "\n\n아래 **목차**에서 궁금한 기능으로 바로 이동할 수 있어요.\n🐣 처음이라면 `/루키미션`으로 보상을 받으며 시작해 보세요!",
                ui.GOLD if done == len(lines) else ui.INFO, user, SECTION)
    return e


# ───────────── 루키 미션 (2.5) ─────────────
def _reward_text(m: dict) -> str:
    parts = [f"💰 {m['money']:,}원"] if m["money"] else []
    parts += [f"{ITEMS[k][0]} {ITEMS[k][1]}" + (f" ×{n}" if n > 1 else "") for k, n in m["items"].items()]
    return " · ".join(parts)


async def rookie_embed(db: EconomyDB, user) -> tuple[discord.Embed, int]:
    """(루키 미션 화면, 지금 받을 수 있는 보상 수)."""
    now = int(time.time())
    s = await db.rookie_status(user.id, now)
    ms = s["missions"]
    lines = []
    for i, m in enumerate(ms, 1):
        mark = "✅" if m["claimed"] else ("🎁" if m["done"] else "⬜")
        prog = f" ({m['value']}/{m['goal']})" if m["goal"] > 1 and not m["done"] else ""
        lines.append(f"{mark} **{i}. {m['title']}**{prog} — {m['how']}\n　 {_reward_text(m)}")
    claimed = sum(m["claimed"] for m in ms)
    ready = sum(m["done"] and not m["claimed"] for m in ms)
    boost = (f"🚀 **신인 부스트** — <t:{s['boost_until']}:R> 끝 · 스카우트 · 훈련 · 직관 보상 ×{ROOKIE_GRIND_MULT} · "
             "첫 유망주 반값\n\n" if now < s["boost_until"] else "")
    e = ui.card(f"🐣 루키 미션 · {claimed}/{len(ms)}", f"{ui.bar(claimed, len(ms))}\n\n{boost}" + "\n".join(lines),
                ui.GOLD if claimed == len(ms) else ui.INFO, user, "🐣 루키")
    e.set_footer(text=f"✅ 받음 · 🎁 받을 수 있음 · ⬜ 진행 중 — 미션은 한 번씩 · 신인 부스트는 처음 시작하고 {ROOKIE_DAYS}일")
    return e, ready


class RookieView(discord.ui.View):
    def __init__(self, db: EconomyDB, user, ready: int):
        super().__init__(timeout=600)
        self.db, self.user = db, user
        self.claim.label = f"보상 받기 ({ready}개)" if ready else "받을 보상 없음"
        self.claim.disabled = not ready

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user.id

    @discord.ui.button(label="보상 받기", emoji="🎁", style=discord.ButtonStyle.success)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        r = await self.db.claim_rookie(self.user.id, int(time.time()))
        e, ready = await rookie_embed(self.db, self.user)
        await interaction.response.edit_message(embed=e, view=RookieView(self.db, self.user, ready))
        if r["claimed"]:   # 받은 보상은 모두에게
            got = "\n".join(f"✅ **{m['title']}** — {_reward_text(m)}" for m in r["claimed"])
            await interaction.followup.send(embed=ui.card(
                f"🐣 루키 미션 보상 {len(r['claimed'])}개!", f"{got}\n\n`잔액` **{r['balance']:,}원**"
                + ("\n🎒 아이템은 `/가방`에서 바로 쓸 수 있어요" if r["items"] else ""), ui.WIN, self.user, "🐣 루키"))


class TutorialView(discord.ui.View):
    def __init__(self, db: EconomyDB, user, step: int = 0, replay_cb=None):
        super().__init__(timeout=600)
        self.db, self.user, self.step, self.replay_cb = db, user, step, replay_cb
        self.steps = TUTORIAL_STEPS_28 if replay_cb else TUTORIAL_STEPS   # replay_cb 가 있으면 2.8 서버
        if replay_cb:   # 2.8: 입문 튜토리얼 다시 보기
            b = discord.ui.Button(label="입문 튜토리얼 다시 보기", emoji="🎓", style=discord.ButtonStyle.success, row=2)
            b.callback = replay_cb
            self.add_item(b)
        menu = discord.ui.Select(placeholder="📚 목차 — 원하는 장으로 바로 이동", row=0, options=[
            discord.SelectOption(label=f"{i}. {title}", value=str(i), emoji=emoji, default=(i == step))
            for i, (emoji, title, _) in enumerate(self.steps)])
        menu.callback = self._jump
        self.menu = menu
        self.add_item(menu)
        self.prev_btn.disabled = step == 0
        self.next_btn.disabled = step == len(self.steps) - 1

    async def make_embed(self) -> discord.Embed:
        emoji, title, body = self.steps[self.step]
        if body is None:
            e = await checklist_embed(self.db, self.user)
        else:
            e = ui.card(f"{emoji} {title}", body, ui.INFO, self.user, SECTION)
        e.set_footer(text=f"{self.step + 1} / {len(self.steps)} · ◀ ▶ 로 넘기거나 목차에서 바로 이동")
        return e

    async def _show(self, interaction: discord.Interaction, step: int):
        view = TutorialView(self.db, self.user, step, self.replay_cb)
        await interaction.response.edit_message(embed=await view.make_embed(), view=view)

    async def _jump(self, interaction: discord.Interaction):
        await self._show(interaction, int(self.menu.values[0]))

    @discord.ui.button(label="◀ 이전", style=discord.ButtonStyle.secondary, row=1)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show(interaction, max(0, self.step - 1))

    @discord.ui.button(label="다음 ▶", style=discord.ButtonStyle.primary, row=1)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show(interaction, min(len(self.steps) - 1, self.step + 1))


# ───────────── 🎓 입문 튜토리얼 (2.8) ─────────────
OB_SECTION = "🎓 입문"
ATTEND_REWARD = 30_000      # cogs.economy.Economy.ATTEND_REWARD 와 같다
CLUB_BONUS = 50_000         # cogs.club.Club.BONUS 와 같다

OB_TEXT = {
    "club": ("축구 봇의 모든 콘텐츠는 **내 구단**에서 시작해요.\n\n"
             "[실습하기]를 누르고 구단 이름을 정하면 바로 만들어지고, 이것까지 자동으로 해 둘게요.\n"
             "　👥 아마추어 선수 18명 입단 · 가장 강한 11명 선발\n"
             "　©️ 선발 중 가장 강한 선수를 주장으로 (전력 +1)\n"
             "　📋 기본 포메이션 4-4-2 · 🐣 초보 감독 · 🩹 인턴 트레이너 (무료)\n"
             "　🔔 DM 알림 기본 ON (`/알림설정`에서 끄기)"),
    "squad": ("선수는 `/선발` `/포메이션` `/주장`으로 직접 바꿀 수 있지만, **`/자동편성` 한 번**이면 충분해요.\n"
              "새 선수를 얻을 때마다 `/자동편성`을 눌러 주세요. 지금 한번 해 볼까요?"),
    "prospect": (f"🌟 **유망주** = 내가 직접 만드는 선수예요. `/유망주생성` · **{PROSPECT_PRICE:,}원** (처음 7일은 첫 유망주 반값)\n\n"
                 "　이름 · 국적 · 포지션 · 등번호를 정하면 17세 · OVR 50~58 · 잠재력 75~94로 태어나요.\n"
                 "　`/선발`에 넣고 경기에 나가면 출전 · 골 · 도움으로 성장해요.\n"
                 f"　1살 = 실제 {PROSPECT_YEAR // 86400}일 · {PROSPECT_RETIRE_AGE}세 은퇴 · 은퇴하면 영구결번도 가능\n\n"
                 "💡 돈이 모이면 만들어 보세요. 루키 미션 '유망주 데뷔'로 보상도 있어요."),
    "daily": ("매일 이 순서대로 하면 가장 많이 벌어요.\n\n"
              f"1️⃣ `/출석` 하루 1번\n2️⃣ `/스카우트` {SCOUT_DAILY_LIMIT}회 → 3️⃣ `/훈련` {TRAIN_DAILY_LIMIT}회 → 4️⃣ `/직관` {WATCH_DAILY_LIMIT}회\n"
              f"🚀 처음 {ROOKIE_DAYS}일은 스카우트 · 훈련 · 직관 보상 ×{ROOKIE_GRIND_MULT}\n\n"
              "`/오늘`을 열면 오늘 남은 할 일이 한눈에 보여요. 지금은 출석부터 해 볼게요."),
    "match": ("구단끼리 경기를 할 수 있어요.\n"
              "　`/친선경기` — 돈 없이 전적만 · `/공식경기` — 돈을 걸고 승/무/패 예측\n\n"
              f"먼저 **{OB.NPC_NAME}**와 연습 경기를 해 볼게요. 결과는 내 구단 전적에 남아요."),
    "tour": ("더 해 볼 수 있는 것들이에요. 지금은 넘어가도 돼요.\n\n"
             "📈 `/시장` — 선수 시세 · 선수팩 · 이적시장 · 트레이드를 한곳에서\n"
             "🎲 `/게임` — 경마 · 조정 · 양궁 · 페널티킥 같은 미니게임 모음\n"
             "🎣 `/낚시` — 기다릴수록 희귀한 물고기 · 팔고, 요리하고, 선수에게 먹이기\n"
             "🏰 `/던전` — 하루 한 번, 내 선발 11명으로 10층 탐험\n"
             "🐣 `/루키미션` — 단계별 미션 보상"),
    "finish": ("수고했어요! 상자를 열면 입문이 끝나고 모든 명령어가 열려요.\n\n"
               f"🎁 **보상 상자** — 💰 {OB.FINAL_MONEY:,}원 · "
               + " · ".join(f"{ITEMS[k][0]} {ITEMS[k][1]} ×{n}" for k, n in OB.FINAL_ITEMS.items())
               + "\n🐣 이미 달성한 루키 미션(구단 창단 · 첫 경기 …) 보상도 같이 받아요."),
}
PRACTICE_LABEL = {"club": "구단 만들기", "squad": "자동편성", "daily": "출석하기", "match": "연습 경기", "finish": "상자 열기"}


class ClubNameModal(discord.ui.Modal, title="🏟️ 구단 이름 정하기"):
    def __init__(self, view: "OnboardingView"):
        super().__init__(timeout=300)
        self.view = view
        self.name = discord.ui.TextInput(label="구단 이름 (비우면 '닉네임 FC')", required=False, max_length=20,
                                         placeholder=f"{view.user.display_name} FC")
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction):
        await self.view.make_club(interaction, (self.name.value or "").strip() or f"{self.view.user.display_name} FC")


class OnboardingView(discord.ui.View):
    """입문 튜토리얼 한 화면. replay=True 면 다시 보기 (실습 · 보상 없음)."""

    def __init__(self, cog: "Tutorial", user, row: dict | None, step: int = 0, replay: bool = False, note: str = ""):
        super().__init__(timeout=600)
        self.cog, self.user, self.row, self.step, self.replay, self.note = cog, user, row, step, replay, note
        key, _, _, needs = OB.STEPS[step]
        done = replay or key in (row or {}).get("practiced", [])
        if not replay and key in PRACTICE_LABEL and not done:
            self._btn(f"실습하기: {PRACTICE_LABEL[key]}", "🧪", discord.ButtonStyle.success, self._practice)
        if step > 0:
            self._btn("이전", "◀️", discord.ButtonStyle.secondary, self._move(-1))
        if step < len(OB.STEPS) - 1:
            nb = self._btn("다음", "▶️", discord.ButtonStyle.primary, self._move(1))
            nb.disabled = needs and not done

    def _btn(self, label, emoji, style, cb):
        b = discord.ui.Button(label=label, emoji=emoji, style=style)
        b.callback = cb
        self.add_item(b)
        return b

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user.id

    def embed(self) -> discord.Embed:
        key, emoji, title, _ = OB.STEPS[self.step]
        n = len(OB.STEPS)
        body = f"{ui.bar(self.step + 1, n)} **{self.step + 1}/{n}**\n\n{OB_TEXT[key]}"
        if self.note:
            body += "\n\n" + self.note
        done = self.replay or key in (self.row or {}).get("practiced", [])
        if not self.replay and key in PRACTICE_LABEL and not done:
            body += "\n\n👇 **[실습하기]**를 눌러 직접 해 보세요."
        e = ui.card(f"{emoji} {title}" + (" · 다시 보기" if self.replay else ""), body,
                    ui.WIN if done and key in PRACTICE_LABEL else ui.INFO, self.user, OB_SECTION)
        e.set_footer(text=("다시 보기 — 실습 · 보상 없이 내용만" if self.replay else
                           f"🎓 입문 튜토리얼 · 약 {OB.TARGET_MIN}분 · 중간에 나가도 /튜토리얼로 이어서"))
        return e

    async def on_timeout(self):
        pass

    async def show(self, interaction: discord.Interaction, step: int | None = None, note: str = "", row=None):
        step = self.step if step is None else step
        row = row if row is not None else self.row
        v = OnboardingView(self.cog, self.user, row, step, self.replay, note)
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=v.embed(), view=v)
        else:
            await interaction.response.edit_message(embed=v.embed(), view=v)

    def _move(self, d: int):
        async def cb(interaction: discord.Interaction):
            step = max(0, min(len(OB.STEPS) - 1, self.step + d))
            row = self.row if self.replay else await self.cog.ob.set_step(self.user.id, step)
            await self.show(interaction, step, row=row)
        return cb

    async def _practice(self, interaction: discord.Interaction):
        key = OB.STEP_KEYS[self.step]
        if key == "club":
            if await self.cog.clubs.get_club(self.user.id):   # 이미 구단이 있으면 자동 설정만
                return await self.make_club(interaction, None)
            return await interaction.response.send_modal(ClubNameModal(self))
        await interaction.response.defer()
        note = await getattr(self.cog, f"_ob_{key}")(self.user)
        row = await self.cog.ob.practiced(self.user.id, key)
        if key == "finish":
            return await interaction.edit_original_response(embed=note, view=None)
        await self.show(interaction, note=note, row=row)

    async def make_club(self, interaction: discord.Interaction, name: str | None):
        await interaction.response.defer()
        uid = self.user.id
        lines = []
        if name is not None:
            ok, msg, bonus = await self.cog.clubs.create_club(uid, name, int(time.time()))
            if not ok:
                return await self.show(interaction, note=f"❌ {msg}")
            await self.cog.pm.give_amateur_squad(uid)
            lines.append(f"🎉 **{name}** 창단!")
            if bonus:
                await self.cog.db.add_balance(uid, CLUB_BONUS)
                lines.append(f"💰 창단 보너스 **+{CLUB_BONUS:,}원**")
        r = await self.cog.ob.setup_club(uid)
        lines.append(f"👥 선발 **{r['filled']}/11** · ©️ 주장 **{r['captain'] or '-'}** · 📋 4-4-2 · 🐣 초보 감독 · 🩹 인턴 트레이너")
        row = await self.cog.ob.practiced(uid, "club")
        await self.show(interaction, note="\n".join(lines), row=row)


class Tutorial(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = EconomyDB()
        self.ob = OB.OnboardingDB()
        from services.club_db import ClubDB
        from services.player_market_db import PlayerMarketDB
        self.clubs, self.pm = ClubDB(), PlayerMarketDB()

    # 실습 — 결과 한두 줄을 돌려준다
    async def _ob_squad(self, user) -> str:
        await self.clubs.auto_lineup(user.id)
        t = await self.clubs.get_team(user.id)
        return f"✅ 자동편성 완료 · 선발 **{t['filled']}/11** · 전력 **{t['rating']}**"

    async def _ob_daily(self, user) -> str:
        ok, bal, _, total, bonus = await self.db.claim_daily(user.id, ATTEND_REWARD, int(time.time()))
        if not ok:
            return "✅ 오늘은 이미 출석했어요. 내일 또 `/출석`!"
        return f"✅ 출석 완료 **+{ATTEND_REWARD + bonus:,}원** · 잔액 {bal:,}원 · 누적 {total}일"

    async def _ob_match(self, user) -> str:
        t = await self.clubs.get_team(user.id)
        r = await self.ob.npc_match(user.id, t, random.Random())
        hl = [f"`{x['minute']:>2}'` {x['icon']} " + (f"**{x['text']}**" if x["goal"] else x["text"])
              for x in r["highlights"]][-5:]
        res = "승리! 🎉" if r["home"] > r["away"] else ("무승부" if r["home"] == r["away"] else "패배… 다음엔 이길 거예요")
        return (f"⚽ **{r['h']['name']} {r['home']} : {r['away']} {r['a']['name']}** — {res}\n" + "\n".join(hl)
                + f"\n`전력` {r['h']['rating']} vs {r['a']['rating']}")

    async def _ob_finish(self, user) -> discord.Embed:
        now = int(time.time())
        r = await self.ob.finish(user.id, now)
        rk = await self.db.claim_rookie(user.id, now)
        lines = ["🎓 **입문 튜토리얼 완료!** 이제 모든 명령어를 쓸 수 있어요.", ""]
        if r["first"]:
            lines.append(f"🎁 💰 **{OB.FINAL_MONEY:,}원** · "
                         + " · ".join(f"{ITEMS[k][0]} {ITEMS[k][1]} ×{n}" for k, n in OB.FINAL_ITEMS.items()))
        for m in rk["claimed"]:
            lines.append(f"🐣 루키 미션 **{m['title']}** — {_reward_text(m)}")
        lines += [f"`잔액` **{rk['balance']:,}원**", "", "📅 내일부터는 `/오늘` 하나로 할 일을 확인하세요.",
                  "📘 다시 보고 싶으면 언제든 `/튜토리얼`."]
        dm = await self._welcome_dm(user)
        if not dm:
            lines.append("📭 DM을 보낼 수 없었어요. 판매 · 정산 알림을 받으려면 서버 DM을 켜 주세요.")
        return ui.card("🎉 축구 봇에 오신 걸 환영해요!", "\n".join(lines), ui.GOLD, user, OB_SECTION)

    async def _welcome_dm(self, user) -> bool:
        """필수 DM: 알림이 DM 으로 온다는 안내 (DM 이 막혀 있으면 False)."""
        try:
            await user.send(embed=ui.card("🔔 알림은 여기로 와요",
                                          "선수 판매 · 토토 정산 · 스폰서 만기 같은 소식을 이 DM으로 알려 드려요.\n"
                                          "`/알림설정`에서 항목별로 끌 수 있어요.", ui.INFO, user, OB_SECTION))
            return True
        except Exception:
            return False

    async def open_onboarding(self, interaction: discord.Interaction, replay: bool = False):
        user = interaction.user
        row = None if replay else await self.ob.begin(user.id, int(time.time()))
        v = OnboardingView(self, user, row, 0 if replay else row["step"], replay)
        await interaction.response.send_message(embed=v.embed(), view=v, ephemeral=True)

    @app_commands.command(name="튜토리얼", description="오늘 할 일 체크리스트와 기능별 안내 (목차로 바로 이동)")
    async def tutorial(self, interaction: discord.Interaction):
        if release.preview(interaction.guild_id) and (await self.ob.state(interaction.user.id))["gate"] == "tutorial":
            return await self.open_onboarding(interaction)
        async def replay(i: discord.Interaction):
            await self.open_onboarding(i, replay=True)
        view = TutorialView(self.db, interaction.user, replay_cb=replay if release.preview(interaction.guild_id) else None)
        await interaction.response.send_message(embed=await view.make_embed(), view=view, ephemeral=True)

    @app_commands.command(name="루키미션", description="처음 시작하는 유저를 위한 단계별 미션 — 깰 때마다 돈 · 아이템 보상 (나만 보기)")
    async def rookie(self, interaction: discord.Interaction):
        e, ready = await rookie_embed(self.db, interaction.user)
        await interaction.response.send_message(embed=e, view=RookieView(self.db, interaction.user, ready), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Tutorial(bot))
