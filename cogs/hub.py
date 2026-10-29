# cogs/hub.py — 2.8 허브: /오늘 · /게임 (새 명령어) + /구단 · /시장 허브 화면 (2.8 서버에서 내 구단 · 시장을 열 때)
# 한 메시지를 계속 고쳐 쓰고 맨 아래 줄은 ◀ 🏠 ▶ (services/hub.py). 예전 명령어(/출석 /시설 /이적시장 …)는 그대로 남는다.
import asyncio
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import release
from services import ui
from services.club_db import (
    CLUB_NAME_MAX, EMBLEM_PRICE, EMBLEMS, FACILITIES, FACILITY_COSTS, FACILITY_MAX, MANAGERS, MEDICS, PROSPECT_PRICE,
    RESERVE_COOLDOWN, RESERVE_XP, STADIUM_NAME_MAX, STADIUM_PRICE, ClubDB, facility_effect, match_highlights,
    simulate_match,
)
from services.enhance import ENH_MIN_OVR
from services.dungeon import THEMES, DungeonDB, theme_of
from services.economy_db import (
    ROOKIE_GRIND_MULT, SCOUT_DAILY_LIMIT, TRAIN_DAILY_LIMIT, WATCH_DAILY_LIMIT, EconomyDB, rookie_until,
)
from services.fishing import FishingDB
from services.hub import HubView, Page
from services import management as M
from services.management import MgmtDB
from services.notifier import send_notify
from services import tactics as T
from services.tactics import TacticsDB
from services import youth as Y
from services.youth import YouthDB
from services.player_market_db import PACKS, PlayerMarketDB

ATTEND_REWARD = 30_000   # cogs.economy.Economy.ATTEND_REWARD 와 같다


def _check(done: bool, text: str) -> str:
    return f"{'✅' if done else '⬜'} {text}"


def _won(n: int) -> str:
    return f"{n // 100_000_000}억" if n >= 100_000_000 and n % 100_000_000 == 0 else (
        f"{n // 10_000:,}만" if n >= 10_000 and n % 10_000 == 0 else f"{n:,}")


def _mgmt_lines(r: dict) -> str:
    sell = " 🎉 **매진!**" if r["sellout"] else f" · 빈자리 {r['capacity'] - r['fans']:,}석"
    return (f"　관중 **{r['fans']:,}** / {r['capacity']:,}석{sell}\n"
            f"　매출 {r['gross']:,}원 − 선수 유지비 {r['wage']:,}원 − 시설 유지비 {r['upkeep']:,}원"
            + (" (수입까지만)" if r["cost"] < r["wage"] + r["upkeep"] else "") + f"\n　순이익 **+{r['net']:,}원**")


def _mgmt_body_340(p: dict) -> str:
    """3.4.0 경영 화면 — 받을 돈 → 오늘 밤 수입 → 왜 그런지 · 늘리는 법 순서로, 쉬운 말로."""
    t, parts = p["today"], p["parts"]
    te, tn, price, _, _ = M.TICKETS[p["ticket"]]
    lines = ["매일 밤 12시(KST)에 경기장 관중 수입이 쌓여요. 쌓인 돈은 아래 **[정산하기]**로 받아요.\n",
             f"💰 **받을 돈 {p['stored']:,}원**" + (f" ({p['stored_days']}일치)" if p["stored_days"] else ""),
             f"\n🌙 **오늘 밤 예상 수입 +{t['net']:,}원**",
             f"　👥 관중 **{t['fans']:,}명** / {t['capacity']:,}석" + (" 🎉 매진!" if t["sellout"] else ""),
             f"　💵 티켓 수입 {t['gross']:,}원 ({te} {tn} {price}원 × {t['fans']:,}명)",
             f"　💸 비용 −{t['cost']:,}원 (선수 월급 {t['wage']:,} · 시설 관리비 {t['upkeep']:,})"
             + (" — 수입보다 많이 나가진 않아요" if t["cost"] < t["wage"] + t["upkeep"] else "")]
    zero = M.BASE_DEMAND / M.DEMAND_PER_RATING
    if t["fans"] == 0:
        tip = (f"구단 전력 **{t['rating']:.0f}**이 너무 낮아서 관중이 안 와요. "
               f"전력 **{50 - zero:.0f}** 넘으면 오기 시작해요 — `/구단`에서 선발을 강하게 바꿔 보세요.")
    elif t["sellout"]:
        tip = "경기장이 꽉 찼어요! 시설에서 **경기장**을 올리면 자리가 늘어나요."
    else:
        tip = "관중 늘리기: 구단 전력 ↑ · 팬 만족도 ↑ · 티켓 값 ↓"
    lines.append(f"\n💡 {tip}")
    arrow = "↗️ 오르는 중" if p["target"] > p["sat"] + 0.5 else "↘️ 내리는 중" if p["target"] < p["sat"] - 0.5 else "➡️ 그대로"
    sp, el = M.fan_bonus(p["sat"])
    lines += [f"\n😊 **팬 만족도 {p['sat']:.0f}/100** {arrow} (매일 {p['target']:.0f} 쪽으로)",
              "　높을수록 관중이 많이 와요"
              + (f" · 지금 보너스: 스폰서 +{sp:.0%} · 명문 구단 +{el:.0%}" if sp else
                 f" · **{M.BONUS[1][0]}** 넘으면 스폰서 +{M.BONUS[1][1]:.0%} · 명문 구단 +{M.BONUS[1][2]:.0%}"),
              f"　올리는 법: 경기 이기기 ({parts['form']:+d}) · 시설 ({parts['facility']:+d}) · "
              f"싼 티켓 ({parts['ticket']:+d}) · 🐠 수족관 ({parts['aquarium']:+d})"]
    if p["last"]:
        lines.append(f"\n📒 지난 밤: 관중 {p['last']['fans']:,}명 · **+{p['last']['net']:,}원**")
    return "\n".join(lines)


def mgmt_report_card(r: dict, user, title: str) -> discord.Embed:
    held = (f"\n\n💰 쌓아 둔 수입 **{r['stored']:,}원** ({r['stored_days']}일치) — `/구단` → 💼 경영 → **[정산하기]**로 받아요"
            if r.get("held") else "")   # 3.1.1 묵혀두기
    return ui.card(title, _mgmt_lines(r) + f"\n\n😊 팬 만족도 {r['sat_before']:.0f} → **{r['sat']:.0f}** (목표 {r['target']:.0f})"
                   + held, ui.WIN, user, "💼 경영")


# /게임 페이지: (이모지, 제목, 한 줄, 본문)
GAMES = (
    ("🏇", "레이스", "경마 · 달팽이 · 조정 · F1", (
        "`/경마` — 4마리 중 한 마리 · 1·2위 배당 · 막판 대역전\n"
        "`/달팽이` — 느리지만 예측 불가\n"
        "`/조정` — [노 젓기]를 직접 연타 · 물살 타이밍\n"
        "`/f1` — 2026 그리드에서 드라이버를 골라 베팅")),
    ("🎯", "한 방 게임", "페널티킥 · 야구 · 농구 · UFC", (
        "`/페널티킥` — 최대 200배 파넨카 ~ 10배 손실\n"
        "`/야구` — 50배 ~ 10배 손실 · `/농구` — 30배 ~ 8배 손실\n"
        "`/ufc` — 100배 ~ 20배 손실")),
    ("⚽", "드리블", "제칠수록 배당이 오르는 솔로 드리블", (
        "`/드리블 베팅액` — 수비수마다 [왼쪽] · [오른쪽] · 성공 확률 65% → 35%로 점점 어려워져요\n"
        "제칠 때마다 1.45 → 2.4 → 4.4 → 8.8 → 19.5 → 49배 · 골키퍼까지 제치면 **140배**\n"
        "언제든 [슛!]으로 지금 배당을 받고 끝 · 막히면 베팅금을 잃어요")),
    ("🏹", "양궁", "바늘을 가운데에 맞춰 6발", (
        "`/양궁 베팅액` — 궁수와 함께 6발. 조준 바늘이 가운데일 때 [쏘기!]\n"
        "합계 57점 이상 +1.5배 · X(정중앙) 3개 이상 보너스\n"
        "🏹 고급 활 · 🧥 바람막이 · 🧪 집중 물약은 던전에서 얻어요")),
    ("🏆", "리그", "내 구단으로 한 시즌", (
        "`/리그` — 내 구단을 랜덤 리그에 넣고 한 시즌\n우승 40배 ~ 강등 15배 손실")),
    ("🎣", "낚시", "기다릴수록 희귀한 물고기", (
        "`/낚시` — 찌를 던지고 입질을 기다려요 (10초 ~ 10분)\n"
        "팔기 · 요리(24시간 버프) · 선수에게 먹이기 · 수족관 · 도감 · 주간 최대어\n"
        "`/낚시시세` — 오늘의 물고기 시세")),
    ("🏰", "던전", "하루 한 번 10층 탐험", (
        "`/던전` — 내 선발 11명으로 요일 테마 던전 10층\n"
        "문 2~3개(힌트는 가끔 거짓) · 5 · 10층 보스 · [귀환]하면 보상 전부\n"
        "보상은 아이템 위주 · 이번 주 최고 층 랭킹")),
    ("🎰", "토토", "실제 경기 베팅", (
        "`/토토` — 축구 · UFC 실제 경기 배당\n`/내베팅` — 내역 · 경기 전 취소")),
)


class Hubs(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = EconomyDB()
        self.clubs = ClubDB()
        self.fish = FishingDB()
        self.dungeon = DungeonDB()
        self.pm = PlayerMarketDB()
        self.tac = TacticsDB()
        self.youth = YouthDB()
        self.mgmt = MgmtDB()
        self._reserve_at: dict[int, int] = {}   # 3.6.0 2군 경기 마지막 시각 (쿨타임)

    async def cog_load(self):
        from cogs.economy import ITEM_HANDLERS
        ITEM_HANDLERS.update({"tactic_note": self._use_note, "analysis_report": self._use_report})
        self.mgmt_loop.start()

    async def cog_unload(self):
        from cogs.economy import ITEM_HANDLERS
        for k in ("tactic_note", "analysis_report"):
            ITEM_HANDLERS.pop(k, None)
        self.mgmt_loop.cancel()

    # ───────────── 🏟️ 구단 경영 정산 (2.8 · 00시 KST) ─────────────
    @tasks.loop(minutes=10)
    async def mgmt_loop(self):
        if release.released_311():   # 3.1.1 양궁 아이템 삭제 → 공개 뒤 상점 원가로 환불 (한 번만)
            for uid, amount in await self.db.refund_archery():
                print(f"[3.2.0] 양궁 아이템 환불 {uid} +{amount:,}원")
        for r in await self.mgmt.settle(int(time.time())):
            print(f"[경영] {r['user_id']} 관중 {r['fans']:,} · 순이익 +{r['net']:,} · 만족도 {r['sat_before']}→{r['sat']}")
            await send_notify(self.bot, self.db, r["user_id"], "구단_경영", mgmt_report_card(r, None, "🏟️ 어제 구단 경영 정산"))

    @mgmt_loop.before_loop
    async def _before_mgmt(self):
        await self.bot.wait_until_ready()

    # ───────────── /오늘 ─────────────
    def today_view(self, user, page: int = 0) -> HubView:
        pages = [Page("📋", "오늘 할 일", self._p_today, "출석 · 스카우트 · 훈련 · 직관 · 낚시 · 던전"),
                 Page("🐣", "루키 미션", self._p_rookie, "처음 시작하는 유저 보상")]
        return HubView(user, "오늘", "📅 오늘", pages, "/오늘", page)

    async def _p_today(self, view: HubView) -> discord.Embed:
        user, now = view.user, int(time.time())
        s = await self.db.today_status(user.id, now)
        f = await self.fish.status(user.id, now)
        d = await self.dungeon.today(user.id, now)
        th = THEMES[theme_of(now)]
        lines = [
            _check(s["attended"], "출석 — `/출석` (아래 버튼)"),
            _check(s["scouting"] >= SCOUT_DAILY_LIMIT, f"스카우트 **{s['scouting']}/{SCOUT_DAILY_LIMIT}** — `/스카우트`"),
            _check(s["training"] >= TRAIN_DAILY_LIMIT, f"훈련 **{s['training']}/{TRAIN_DAILY_LIMIT}** — `/훈련`"),
            _check(s["spectating"] >= WATCH_DAILY_LIMIT, f"직관 **{s['spectating']}/{WATCH_DAILY_LIMIT}** — `/직관`"),
            _check(d["runs"] >= d["allowed"] and not d["active"],
                   f"던전 **{d['runs']}/{d['allowed']}** — `/던전` 오늘은 {th[0]} {th[1]}"
                   + (" · ⏸️ 진행 중" if d["active"] else "")),
            _check(f["used"] >= f["limit"], f"낚시 거래 **{f['used']}/{f['limit']}**포인트 — `/낚시`"
                   + (" · 🎣 찌 던져 둠" if f.get("cast_ts") else "")),
        ]
        done = sum(x.startswith("✅") for x in lines)
        rs = await self.db.rookie_status(user.id, now)
        ready = sum(m["done"] and not m["claimed"] for m in rs["missions"])
        extra = []
        if now < rs["boost_until"]:
            extra.append(f"🚀 신인 부스트 <t:{rs['boost_until']}:R> 끝 · 스카우트 · 훈련 · 직관 보상 ×{ROOKIE_GRIND_MULT}")
        if ready:
            extra.append(f"🎁 받을 수 있는 루키 미션 보상 **{ready}개** — ▶ 다음 화면")
        e = ui.card(f"📋 오늘 할 일 · {done}/{len(lines)}", f"{ui.bar(done, len(lines))}\n\n" + "\n".join(lines)
                    + ("\n\n" + "\n".join(extra) if extra else ""), ui.GOLD if done == len(lines) else ui.INFO, user, "📅 오늘")
        if not s["attended"]:
            b = discord.ui.Button(label=f"출석하기 +{ATTEND_REWARD:,}원", emoji="📅", style=discord.ButtonStyle.success, row=0)

            async def attend(interaction: discord.Interaction):
                ok, bal, _, total, bonus = await self.db.claim_daily(user.id, ATTEND_REWARD, int(time.time()))
                note = ui.card("✅ 출석 완료" if ok else "⏳ 이미 출석했어요",
                               (f"`보상` **+{ATTEND_REWARD + bonus:,}원** · `잔액` **{bal:,}원** · `누적` {total}일") if ok else
                               "내일 00시에 다시 할 수 있어요.", ui.WIN if ok else ui.EVEN, user, "📅 출석")
                await view.refresh(interaction, note)
            b.callback = attend
            view.add_item(b)
        return e

    async def _p_rookie(self, view: HubView) -> discord.Embed:
        from cogs.tutorial import rookie_embed
        user = view.user
        e, ready = await rookie_embed(self.db, user)
        if ready:
            b = discord.ui.Button(label=f"보상 받기 ({ready}개)", emoji="🎁", style=discord.ButtonStyle.success, row=0)

            async def claim(interaction: discord.Interaction):
                r = await self.db.claim_rookie(user.id, int(time.time()))
                note = ui.card(f"🐣 루키 미션 보상 {len(r['claimed'])}개!", f"`잔액` **{r['balance']:,}원**", ui.WIN, user, "🐣 루키")
                await view.refresh(interaction, note)
            b.callback = claim
            view.add_item(b)
        return e

    @app_commands.command(name="오늘", description="📅 오늘 할 일 한눈에 — 출석 · 스카우트 · 훈련 · 직관 · 낚시 · 던전 · 루키 미션")
    async def today(self, interaction: discord.Interaction):
        await self.today_view(interaction.user, 0).send(interaction)

    # ───────────── /게임 ─────────────
    def games_view(self, user, page: int = -1) -> HubView:
        def render(i):
            async def r(view: HubView) -> discord.Embed:
                emoji, title, _, body = GAMES[i]
                return ui.card(f"{emoji} {title}", body + "\n\n⚠️ 미니게임은 최소 1,000원 · 큰 손실은 잔액이 마이너스가 될 수 있어요.",
                               ui.INFO, view.user, "🎮 게임")
            return r
        new = release.preview_310(getattr(getattr(user, "guild", None), "id", None))
        pages = [Page(e, t, render(i), hint) for i, (e, t, hint, _) in enumerate(GAMES)
                 if new or t != "드리블"]   # 3.1.0 /드리블 은 공개 서버에만
        return HubView(user, "게임", "🎮 게임", pages, "/게임", page, home_note="해 보고 싶은 게임을 골라 보세요.")

    @app_commands.command(name="게임", description="🎮 미니게임 모음 — 레이스 · 한 방 게임 · 양궁 · 리그 · 낚시 · 던전 · 토토")
    async def games(self, interaction: discord.Interaction):
        await self.games_view(interaction.user).send(interaction)

    # ───────────── /구단 허브 (2.8 서버 · 내 구단) ─────────────
    def club_view(self, user, page: int = 0, guild_id: int | None = None) -> HubView:
        g311 = release.preview_311(guild_id)   # 3.1.1: /포메이션 · /선발 · /주장 · /감독 · /의료진 · /시설 을 이 화면으로
        g360 = release.preview_360(guild_id)   # 3.6.0: 유망주 · 2군 · 유스는 /2군 으로 따로
        pages = [Page("🏟️", "구단", self._p_club, "선발 명단 · 전력 · 전적")]
        if g311:
            pages.append(Page("📋", "편성", self._p_lineup, "포메이션 · 선발 · 주장"))
        if not g360:
            pages.append(Page("🌟", "유망주", self._p_prospect, "나만의 선수"))
        pages += [Page("🏗️", "시설", self._p_facility, "경기장 · 훈련장 · 유스 · 메디컬"),
                 Page("👔", "감독 · 의료진" + (" · 스카우터" if g311 else ""), self._p_staff,
                      "전력 보너스 · 부상 치료" + (" · 유스 스카우터" if g311 else "")),
                 Page("⚔️", "전술", self._p_tactics, "기본 전술 · 적응도 · 하프타임 규칙"),
                 Page("🔍", "상대 분석", self._p_scout, "상대 최근 5경기 전술")]
        if not g360:
            pages.append(Page("🎓", "유스", self._p_youth, "입단 테스트 · 훈련 · 판매 · 1군 등록"))
        pages.append(Page("💼", "경영", self._p_mgmt, "관중 수입 · 팬 만족도 · 티켓 가격"))
        if release.preview_2101(guild_id):   # 2.10.1 (테스트 서버 먼저)
            pages.append(Page("🎨", "꾸미기 · 설정", self._p_decor, "구단명 · 엠블럼 · 경기장 이름 · 구단 삭제"))
        return HubView(user, "내 구단", "🏟️ 구단", pages, "/구단", page, ctx={"g311": g311, "g360": g360})

    # ───────────── /2군 (3.6.0 · 유망주 · 2군 경기 · 유스) ─────────────
    def reserve_view(self, user, page: int = 0, guild_id: int | None = None) -> HubView:
        pages = [Page("🎓", "2군 · 유스", self._p_youth, "2군 경기 · 입단 테스트 · 훈련 · 판매 · 1군 등록"),
                 Page("🌟", "유망주", self._p_prospect, "나만의 선수 · 1군 승격 · 강화는 `/유망주`")]
        return HubView(user, "2군", "🎓 2군", pages, "/2군", page,
                       ctx={"g311": release.preview_311(guild_id), "g360": True})

    @app_commands.command(name="2군", description="🎓 2군 · 유스 · 유망주 — 다른 유저 2군과 경기 · 입단 테스트 · 훈련 · 1군 등록")
    async def reserve(self, interaction: discord.Interaction):
        if not release.preview_360(interaction.guild_id):
            return await interaction.response.send_message("아직 이 서버에서는 쓸 수 없는 명령어예요.", ephemeral=True)
        if not await self.clubs.get_club(interaction.user.id):
            return await interaction.response.send_message("구단이 있어야 해요. `/구단생성`", ephemeral=True)
        await self.reserve_view(interaction.user, guild_id=interaction.guild_id).send(interaction, ephemeral=False)

    # ───────────── 🎨 꾸미기 · 설정 (2.10.1: /구단명변경 · /구단꾸미기 · /구단삭제 를 여기로) ─────────────
    async def _p_decor(self, view: HubView) -> discord.Embed:
        user = view.user
        team = await self.clubs.get_team(user.id)
        e = ui.card("🎨 꾸미기 · 설정",
                    f"`구단명` **{team['name']}** · 무료 (최대 {CLUB_NAME_MAX}자)\n"
                    f"`엠블럼` {team.get('emblem') or '없음'} · {_won(EMBLEM_PRICE)}원\n"
                    f"`경기장` {team.get('stadium') or '없음'} · {_won(STADIUM_PRICE)}원 (경기 중계에 나와요)",
                    ui.INFO, user, "🎨 꾸미기")
        emb = discord.ui.Select(placeholder=f"🛡️ 엠블럼 바꾸기 · {_won(EMBLEM_PRICE)}원", row=0, options=[
            discord.SelectOption(label=f"{x} 엠블럼", value=x, default=x == team.get("emblem")) for x in EMBLEMS[:25]])

        async def set_emblem(interaction: discord.Interaction):
            await view.refresh(interaction, await self._decorate(user, "emblem", emb.values[0], "엠블럼"))
        emb.callback = set_emblem
        view.add_item(emb)
        for label, emoji, field, cap, current in (("구단명 변경", "✏️", "name", CLUB_NAME_MAX, team["name"]),
                                                  ("경기장 이름", "🏟️", "stadium", STADIUM_NAME_MAX, team.get("stadium") or "")):
            b = discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.primary, row=1)
            b.callback = self._name_modal(view, field, label, cap, current)
            view.add_item(b)
        d = discord.ui.Button(label="구단 삭제", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)

        async def delete(interaction: discord.Interaction):
            from cogs.club import DeleteConfirm
            c = ui.card(f"⚠️ {team['name']} 을(를) 삭제할까요?",
                        "구단 이름·포메이션·선발 명단·전적과 **아마추어 스쿼드**가 사라집니다.\n"
                        "돈과 일반 선수 카드는 그대로 남습니다. 다시 창단해도 창단 보너스는 다시 지급되지 않습니다.",
                        ui.DOOM, user, "🏟️ 구단")
            await interaction.response.send_message(embed=c, view=DeleteConfirm(self, user.id), ephemeral=True)
        d.callback = delete
        view.add_item(d)
        return e

    def _name_modal(self, view: HubView, field: str, label: str, cap: int, current: str):
        async def open_(interaction: discord.Interaction):
            modal = discord.ui.Modal(title=label)
            box = discord.ui.TextInput(label=label, default=current[:cap] or None, max_length=cap, min_length=1)
            modal.add_item(box)

            async def submit(mi: discord.Interaction):
                value = str(box.value).strip()
                if field == "name":
                    ok, msg = await self.clubs.rename_club(view.user.id, value)
                    note = ui.card("✏️ 구단명 변경" if ok else "❌ 변경 실패",
                                   f"구단명이 **{value}**(으)로 바뀌었습니다." if ok else msg,
                                   ui.WIN if ok else ui.LOSE, view.user, "🎨 꾸미기")
                else:
                    note = await self._decorate(view.user, field, value, "경기장")
                await view.refresh(mi, note)
            modal.on_submit = submit
            await interaction.response.send_modal(modal)
        return open_

    async def _decorate(self, user, col: str, value: str, label: str) -> discord.Embed:
        r = await self.clubs.decorate(user.id, col, value)
        if r["ok"]:
            return ui.card(f"🎨 {label} 변경", f"`{label}` **{value}** (-{r['fee']:,}원)", ui.GOLD, user, "🎨 꾸미기")
        text = {"no_club": "구단이 없어요.", "same": f"{label}이(가) 이미 **{value}**이에요.",
                "balance": f"{label} 변경에 **{r.get('fee', 0):,}원**이 필요해요."}[r["reason"]]
        return ui.card("❌ 꾸미기 실패", text, ui.LOSE, user, "🎨 꾸미기")

    # ───────────── ⚔️ 전술 (2.8) ─────────────
    async def _p_tactics(self, view: HubView) -> discord.Embed:
        user, now = view.user, int(time.time())
        s = await self.tac.settings(user.id)
        team = await self.clubs.get_team(user.id)
        xi = [p for p in team["lineup"] if p.get("player_id")]
        fits = await self.tac.player_fits(user.id, [str(p["player_id"]) for p in xi], now)
        lines = []
        for t, (e_, n_, _, attr) in T.TACTICS.items():
            fit = sum(f[t] for f in fits.values()) / len(fits) if fits else T.FIT_START
            mark = " ✅ **기본 전술**" if t == s["tactic"] else ""
            lines.append(f"{e_} **{n_}** · 적응도 {fit:.0f} · 효과 **{T.effect(fit, T.composition(xi, t)):.0%}**{mark}")
        short = {"losing": "질 때", "draw": "비길 때", "winning": "이길 때"}
        rules = " · ".join(f"{short.get(k, n)} {T.label(s['rules'][k]) if s['rules'].get(k) else '유지'}" for k, n in T.RULE_STATES)
        e = ui.card("⚔️ 전술", "🔥 압박 > 🎯 점유 > ⚡ 역습 > 🔥 압박\n\n" + "\n".join(lines)
                    + f"\n\n**하프타임 규칙** {rules}", ui.INFO, user, "⚔️ 전술")
        e.set_footer(text="유리하면 기대 득점 최대 +6% · 효과는 적응도(그 전술로 뛸수록 ↑)와 선수 구성"
                          "(압박 피지컬 · 점유 패스 · 역습 속력)으로 정해져요")
        base = discord.ui.Select(placeholder="⚔️ 기본 전술 바꾸기", row=0, options=[
            discord.SelectOption(label=n_, value=t, emoji=e_, default=t == s["tactic"]) for t, (e_, n_, *_) in T.TACTICS.items()])

        async def set_base(interaction: discord.Interaction):
            await self.tac.set_tactic(user.id, base.values[0])
            await view.refresh(interaction)
        base.callback = set_base
        view.add_item(base)
        for row, (k, n) in enumerate(T.RULE_STATES, 1):
            sel = discord.ui.Select(placeholder=f"하프타임 · {n}", row=row, options=[
                discord.SelectOption(label=f"{n} → 유지", value="keep", default=not s["rules"].get(k))] + [
                discord.SelectOption(label=f"{n} → {n_}", value=t, emoji=e_, default=s["rules"].get(k) == t)
                for t, (e_, n_, *_) in T.TACTICS.items()])
            sel.callback = self._rule_cb(view, k, sel)
            view.add_item(sel)
        return e

    def _rule_cb(self, view: HubView, state: str, sel):
        async def cb(interaction: discord.Interaction):
            v = sel.values[0]
            await self.tac.set_rule(view.user.id, state, None if v == "keep" else v)
            await view.refresh(interaction)
        return cb

    async def _p_scout(self, view: HubView) -> discord.Embed:
        user = view.user
        opp_id = view.ctx.get("scout")
        body = "상대를 고르면 최근 5경기 전술을 보여 드려요."
        if opp_id:
            rows = await self.tac.recent(opp_id, 5)
            m = self.bot.get_user(opp_id) if self.bot else None
            name = m.display_name if m else f"<@{opp_id}>"
            if rows:
                cnt = {t: sum(st == t for st, _ in rows) for t in T.TACTICS}
                sw = sum(st != en for st, en in rows)
                body = (f"**{name}** 최근 {len(rows)}경기\n시작 전술 "
                        + " · ".join(f"{T.label(t)} {c}" for t, c in cnt.items() if c)
                        + f"\n하프타임에 바꾼 경기 {sw}번")
            else:
                body = f"**{name}** — 아직 전술 경기 기록이 없어요."
        e = ui.card("🔍 상대 분석", body, ui.INFO, user, "⚔️ 전술")
        e.set_footer(text="📊 분석 리포트(아이템)를 쓰면 다음 경기 킥오프 때 상대 전술이 보여요")
        pick = discord.ui.UserSelect(placeholder="🔍 분석할 상대 고르기", row=0)

        async def chosen(interaction: discord.Interaction):
            view.ctx["scout"] = pick.values[0].id
            await view.refresh(interaction)
        pick.callback = chosen
        view.add_item(pick)
        return e

    # ───────────── 💼 경영 (2.8) ─────────────
    async def _p_mgmt(self, view: HubView) -> discord.Embed:
        user, g311 = view.user, view.ctx.get("g311")
        await self.mgmt.join(user.id, int(time.time()), hold=bool(g311))   # 3.1.1 테스트 서버에서 열면 묵혀두기 시작
        p = await self.mgmt.preview(user.id)
        if not p:
            return ui.card("💼 경영", "구단이 있어야 경영을 할 수 있어요. `/구단생성`을 먼저 해 주세요.", ui.EVEN, user, "💼 경영")
        t, parts = p["today"], p["parts"]
        if release.preview_340(getattr(getattr(user, "guild", None), "id", None)):   # 3.4.0 알기 쉬운 경영 화면
            e = ui.card("💼 구단 경영", _mgmt_body_340(p), ui.INFO, user, "💼 경영")
            e.set_footer(text="🎟️ 티켓: 싸면 관중 · 만족도 ↑ / 비싸면 1명당 수입 ↑ — 바꾸면 다음 정산부터")
            self._mgmt_buttons(view, p, True)
            return e
        sp, el = M.fan_bonus(p["sat"])
        bonus = (f"스폰서 플러스 수익 **+{sp:.0%}** · 명문 구단 하루 수입 **+{el:.0%}**" if sp else
                 f"없음 (70 이상: 스폰서 +{M.BONUS[1][1]:.0%} · 명문 구단 +{M.BONUS[1][2]:.0%} / "
                 f"85 이상: +{M.BONUS[0][1]:.0%} · +{M.BONUS[0][2]:.0%})")
        arrow = "↗️" if p["target"] > p["sat"] + 0.5 else "↘️" if p["target"] < p["sat"] - 0.5 else "➡️"
        why = " · ".join(f"{n} {v:+d}" for n, v in (("최근 10경기", parts["form"]), ("시설", parts["facility"]),
                                                    ("티켓", parts["ticket"]), ("🐠 수족관", parts["aquarium"])))
        te, tn, price, _, _ = M.TICKETS[p["ticket"]]
        body = (f"**😊 팬 만족도** {ui.bar(p['sat'], 100, 10)} **{p['sat']:.0f}** {arrow} 목표 {p['target']:.0f}\n"
                f"　목표 = 50 · {why}\n　보너스: {bonus}\n\n"
                f"**🎟️ 티켓** {te} {tn} ({price}원) — 바꾸면 다음 정산부터 적용돼요\n\n"
                f"**🌙 오늘 밤 예상** (지금 전력 {t['rating']:.0f} 기준)\n" + _mgmt_lines(t))
        if p["last"]:
            body += "\n\n**📒 지난 정산**\n" + _mgmt_lines(p["last"])
        held = g311 or p["hold"] or p["stored"] > 0
        if held:
            body = (f"**💰 쌓인 수입** **{p['stored']:,}원**" + (f" ({p['stored_days']}일치)" if p["stored_days"] else "")
                    + " — **[정산하기]**를 눌러 받아요\n\n" + body)
        e = ui.card("💼 구단 경영", body, ui.INFO, user, "💼 경영")
        e.set_footer(text=("매일 00시(KST) 수입이 쌓여요 (상한 없음) · " if held else "매일 00시(KST) 정산 · ")
                          + "관중 = 경기장 수용 한도까지 · 비용은 그날 수입까지만 (마이너스 없음) · /알림설정 → 구단 경영 정산 DM")
        self._mgmt_buttons(view, p, held)
        return e

    def _mgmt_buttons(self, view: HubView, p: dict, held: bool):
        for k, (e_, n_, pr, mult, _) in M.TICKETS.items():
            b = discord.ui.Button(label=f"{n_} {pr}원 (관중 ×{mult:g})", emoji=e_, row=0, disabled=k == p["ticket"],
                                  style=discord.ButtonStyle.success if k == p["ticket"] else discord.ButtonStyle.secondary)
            b.callback = self._ticket_cb(view, k)
            view.add_item(b)
        if held:
            b = discord.ui.Button(label=f"정산하기 (+{_won(p['stored'])}원)" if p["stored"] else "정산하기 (쌓인 돈 없음)",
                                  emoji="💰", style=discord.ButtonStyle.primary, row=1, disabled=p["stored"] <= 0)
            b.callback = self._collect_cb(view)
            view.add_item(b)

    def _collect_cb(self, view: HubView):
        async def cb(interaction: discord.Interaction):
            r = await self.mgmt.collect(view.user.id)
            note = (ui.card("💰 경영 수입 정산!", f"`받은 돈` **+{r['amount']:,}원** ({r['days']}일치)\n`잔액` **{r['balance']:,}원**",
                            ui.GOLD, view.user, "💼 경영") if r["ok"] else
                    ui.card("💰 정산할 돈이 없어요", "매일 00시(KST)에 수입이 쌓여요.", ui.EVEN, view.user, "💼 경영"))
            await view.refresh(interaction, note)
        return cb

    def _ticket_cb(self, view: HubView, key: str):
        async def cb(interaction: discord.Interaction):
            await self.mgmt.set_ticket(view.user.id, key)
            await view.refresh(interaction)
        return cb

    # ───────────── 🎓 유스 (2.8) ─────────────
    SCENE_DELAY = 1.2   # 입단 테스트 장면 사이 (초)

    async def _p_youth(self, view: HubView) -> discord.Embed:
        user, now = view.user, int(time.time())
        st = await self.youth.status(user.id, now)
        youth, items = st["youth"], st["items"]
        g311 = view.ctx.get("g311")   # 3.1.1 별(구단 전력 기준) · 스카우터 정확도
        if g311:
            team = await self.clubs.get_team(user.id)
            club, sc = (team["rating"] if team else 50), st["scouter"]
        ids = {y["id"] for y in youth}
        if view.ctx.get("yid") not in ids:
            view.ctx["yid"] = youth[0]["id"] if youth else None
        sel_y = next((y for y in youth if y["id"] == view.ctx["yid"]), None)
        head = (f"아카데미 Lv.{st['level']} · 슬롯 **{len(youth)}/{st['slots']}** · 1군 상한 OVR {st['cap']}\n"
                + " ".join([_check(st["tried"], "테스트"), _check(st["matched"], "연습경기"), _check(st["registered"], "1군 등록")]))
        if g311:
            s_ = Y.SCOUTERS.get(sc)
            head += (f"\n🕵️ {s_[0] + ' ' + s_[1] if s_ else '스카우터 없음'} · 정확도 **{Y.scout_acc(sc):.0%}** · "
                     f"별은 내 구단 전력 **{club}** 기준")
        lines = []
        for y in youth:
            pick = "▶ " if y["id"] == view.ctx["yid"] else ""
            grad = " 🎓졸업" if y["age"] >= Y.GRAD_AGE else ""
            if g311:
                ps, exact = Y.pot_stars(y, club, sc)
                pot = (f"현재 {Y.star_text(Y.cur_stars(y['ovr'], club))} · 잠재 {Y.star_text(ps)}"
                       + (f" ({y['pot']})" if exact else " (추정)"))
            else:
                pot = f"잠재력 {Y.pot_text(y)}"
            lines.append(f"{pick}**{y['name']}** {y['pos']} {y['age']}세{grad} · OVR **{y['ovr']:.1f}** · {pot} · "
                         f"{Y.PERSONALITIES[y['personality']][0]} · 훈련 {y['trains_left']}")
        body = head + "\n\n" + ("\n".join(lines) if lines else "유스 선수가 없어요. **지원자 보기**로 첫 선수를 뽑아 보세요!")
        if st["first"]:
            body += "\n\n**1군** " + " · ".join(f"🎓 {y['name']} OVR {y['first_ovr']}" for y in st["first"])
        apps = view.ctx.get("apps") if not st["tried"] else None
        if apps:
            body += ("\n\n**🔎 오늘의 지원자** (잠재력 별은 스카우터 추정)\n" if g311 else
                     "\n\n**🔎 오늘의 지원자** (잠재력은 비밀)\n") + "\n".join(
                f"{i}. **{a['name']}** {a['pos']} {a['age']}세 · OVR {a['ovr']} · "
                + (f"잠재 {Y.star_text(Y.applicant_stars(a, club, sc))} · " if g311 else "")
                + f"{Y.PERSONALITIES[a['personality']][0]} — \"{a['scout']}\"" for i, a in enumerate(apps, 1))
        g360 = view.ctx.get("g360")   # 3.6.0 2군
        if g360:
            body = await self._reserve_text(user, now) + "\n\n**🎓 유스**\n" + body
        e = ui.card("🎓 2군 · 유스" if g360 else "🎓 유스", body, ui.INFO, user, "🎓 유스")
        e.set_footer(text=f"테스트 {_won(Y.TRYOUT_FEE)}원(불합격 시 절반 환불) · 훈련 하루 {Y.TRAIN_DAILY}번 · "
                          f"{Y.GRAD_AGE}세 졸업 · 성격: 📚성실 ✨천재 🌱대기만성 🔥승부사")
        # 0줄: 선수 고르기
        if youth:
            pick = discord.ui.Select(placeholder="🎓 선수 고르기", row=0, options=[
                discord.SelectOption(label=f"{y['name']} · {y['pos']} · OVR {y['ovr']:.1f}", value=str(y["id"]),
                                     description=f"{y['age']}세 · {Y.personality_label(y['personality'])}",
                                     default=y["id"] == view.ctx["yid"]) for y in youth[:25]])

            async def picked(interaction: discord.Interaction):
                view.ctx["yid"] = int(pick.values[0])
                view.ctx.pop("confirm", None)
                await view.refresh(interaction)
            pick.callback = picked
            view.add_item(pick)
        # 1줄: 고른 선수에게
        confirm = view.ctx.get("confirm")
        acts = [("train", "훈련", "🏋️", discord.ButtonStyle.primary, sel_y and sel_y["trains_left"] > 0),
                ("ticket", f"특훈권 ({items['special_training']})", "🏋️", discord.ButtonStyle.secondary,
                 sel_y and items["special_training"] > 0),
                ("sell", f"정말 판매 ({sel_y['price']:,}원)" if sel_y and confirm == ("sell", sel_y["id"]) else "판매", "💰",
                 discord.ButtonStyle.danger if confirm and confirm[0] == "sell" else discord.ButtonStyle.secondary, bool(sel_y)),
                ("register", "정말 1군 등록" if sel_y and confirm == ("register", sel_y["id"]) else f"1군 등록 ({items['first_team']})",
                 "📝", discord.ButtonStyle.success, sel_y and items["first_team"] > 0 and not st["registered"])]
        for key, label, emoji, style, ok in acts:
            b = discord.ui.Button(label=label, emoji=emoji, style=style, row=1, disabled=not ok)
            b.callback = self._youth_act(view, key)
            view.add_item(b)
        # 2줄: 테스트 · 연습경기
        full = len(youth) >= st["slots"]
        for key, label, emoji, ok in (
                ("tryout", "지원자 보기" if not apps else "지원자 다시 보기", "🔎", not st["tried"] and not full),
                ("coupon", f"쿠폰으로 테스트 ({items['scout_coupon']})", "🎟️",
                 not st["tried"] and not full and not apps and items["scout_coupon"] > 0),
                ("reserve", "2군 경기", "⚽", True) if g360 else ("match", "주간 연습경기", "⚽", not st["matched"] and bool(youth))):
            b = discord.ui.Button(label=label, emoji=emoji, row=2, disabled=not ok,
                                  style=discord.ButtonStyle.primary if key in ("tryout", "reserve") else discord.ButtonStyle.secondary)
            b.callback = self._youth_act(view, key)
            view.add_item(b)
        # 3줄: 지원자 고르기 → 테스트
        if apps:
            sel = discord.ui.Select(placeholder=f"🔎 테스트할 지원자 고르기 ({'무료' if apps[0].get('free') else _won(Y.TRYOUT_FEE) + '원'})",
                                    row=3, options=[discord.SelectOption(
                                        label=f"{a['name']} \"{a['nick']}\" · {a['pos']} · OVR {a['ovr']}"[:100], value=str(i),
                                        description=a["scout"][:100]) for i, a in enumerate(apps)])

            async def go(interaction: discord.Interaction):
                await self._youth_tryout(view, interaction, int(sel.values[0]))
            sel.callback = go
            view.add_item(sel)
        return e

    def _youth_act(self, view: HubView, key: str):
        async def cb(interaction: discord.Interaction):
            user, now, yid = view.user, int(time.time()), view.ctx.get("yid")
            note = None
            if key in ("tryout", "coupon"):
                r = await self.youth.open_tryout(user.id, now, coupon=key == "coupon", v311=bool(view.ctx.get("g311")))
                if r["ok"]:
                    view.ctx["apps"] = r["applicants"]
                else:
                    note = ui.card("🙅 입단 테스트", {"done": "오늘은 이미 테스트했어요. 내일 다시!", "full": "유스 슬롯이 꽉 찼어요.",
                                                    "coupon": "🎟️ 스카우트 쿠폰이 없어요."}[r["reason"]], ui.LOSE, user, "🎓 유스")
            elif key in ("train", "ticket"):
                r = await self.youth.train(user.id, yid, now, ticket=key == "ticket")
                note = ui.card(f"🏋️ {r['name']} 훈련!", f"OVR {r['before']:.1f} → **{r['ovr']:.1f}** · 누적 훈련 {r['trains']}번"
                               + (f"\n잠재력 공개: {Y.REVEAL[0]}번 등급 · {Y.REVEAL[1]}번 ±5 · {Y.REVEAL[2]}번 정확한 값"
                                  if r["trains"] in Y.REVEAL else ""), ui.WIN, user, "🎓 유스") if r["ok"] else ui.card(
                    "🙅 훈련", {"limit": "오늘 훈련을 다 했어요.", "ticket": "🏋️ 특훈권이 없어요.", "none": "선수를 골라 주세요."}[r["reason"]],
                    ui.LOSE, user, "🎓 유스")
            elif key == "reserve":
                note = await self._reserve_match(user, now)
            elif key == "match":
                r = await self.youth.weekly_match(user.id, now)
                if r["ok"]:
                    gf, ga = r["score"]
                    note = ui.card(f"⚽ 유스 연습경기 {gf} : {ga} {'승리!' if r['won'] else '무승부' if gf == ga else '패배'}",
                                   "\n".join(f"{n} {a:.1f} → **{b:.1f}**" for n, a, b in r["grown"]),
                                   ui.WIN if r["won"] else ui.EVEN, user, "🎓 유스")
                else:
                    note = ui.card("🙅 연습경기", "이번 주 연습경기는 이미 했어요." if r["reason"] == "done" else "유스 선수가 없어요.",
                                   ui.LOSE, user, "🎓 유스")
            elif key in ("sell", "register"):
                if view.ctx.get("confirm") != (key, yid):   # 한 번 더 눌러야 실행 (되돌릴 수 없다)
                    view.ctx["confirm"] = (key, yid)
                    return await view.refresh(interaction)
                view.ctx.pop("confirm", None)
                if key == "sell":
                    r = await self.youth.sell(user.id, yid, now)
                    note = ui.card(f"💰 {r['name']} 판매", f"NPC 구단이 **{r['price']:,}원**에 데려갔어요.\n잔액 {r['balance']:,}원",
                                   ui.GOLD, user, "🎓 유스") if r["ok"] else ui.card("🙅 판매", "선수를 찾지 못했어요.", ui.LOSE, user, "🎓 유스")
                else:
                    r = await self.youth.register(user.id, yid, now)
                    note = ui.card(f"📝 {r['name']} 1군 등록!", f"OVR **{r['ovr']}**로 내 구단 명단에 들어왔어요. 선발에 넣어 보세요.\n"
                                   f"(내 구단 전용 · 팔거나 넘길 수 없어요 · 7일 = 1살 · 35세 은퇴)", ui.GOLD, user, "🎓 유스") \
                        if r["ok"] else ui.card("🙅 1군 등록", {
                            "cap": f"OVR이 상한 {r.get('cap')}을 넘어요. 유스 아카데미를 올리면 상한이 올라가요.",
                            "week": "1군 등록은 주 1회예요.", "item": "📝 1군 등록권이 없어요.",
                            "balance": f"등록비 {Y.REGISTER_FEE:,}원이 부족해요.", "club": "구단이 있어야 해요.",
                            "none": "선수를 찾지 못했어요."}[r["reason"]], ui.LOSE, user, "🎓 유스")
            await view.refresh(interaction, note)
        return cb

    # ───────────── ⚽ 2군 (3.6.0) ─────────────
    async def _reserve_text(self, user, now: int) -> str:
        t = await self.clubs.reserve_team(user.id, now)
        if not t:
            return "**⚽ 2군** — 구단이 있어야 해요. `/구단생성`"
        p = t["prospect_any"]
        star = (f"🌟 **{p['name']}** OVR {p['ovr']}" + (" (🚑 부상 — 복귀하면 다시 뛰어요)" if p["injured"] else "")
                + f" · OVR **{ENH_MIN_OVR}**이 되면 1군 승격 · 강화") if p else \
            f"🌟 2군 유망주 없음 (강화 전 · OVR {ENH_MIN_OVR} 밑 유망주가 여기서 뛰어요)"
        youth = [s["name"] for s in t["xi"] if s.get("youth_id")]
        ama = sum(bool(s.get("amateur")) for s in t["xi"])
        return (f"**⚽ 2군** · 전력 **{t['rating']}** · {t['formation']}\n{star}\n"
                f"🎓 유스 {len(youth)}명" + (f" ({', '.join(youth)})" if youth else "") + f" · 👥 2군 아마추어 {ama}명\n"
                f"-# 2군 경기는 다른 유저 2군과 · 판수 제한 없이 {RESERVE_COOLDOWN}초마다 · 유망주 경험치 ×{RESERVE_XP:g} · "
                f"유스는 하루 {Y.RESERVE_DAILY}판까지 훈련 1번만큼 자라요")

    async def _reserve_match(self, user, now: int) -> discord.Embed:
        """2군 경기 한 판 — 다른 유저 구단의 2군과 바로 결과. 양쪽 유망주 경험치 · 내 유스 성장."""
        from cogs.club import Club
        left = self._reserve_at.get(user.id, 0) + RESERVE_COOLDOWN - now
        if left > 0:
            return ui.card("⏳ 2군 선수들이 쉬는 중", f"**{left}초** 뒤에 다음 경기를 할 수 있어요.", ui.EVEN, user, "🎓 유스")
        t = await self.clubs.reserve_team(user.id, now)
        if not t:
            return ui.card("🙅 2군 경기", "구단이 있어야 해요. `/구단생성`", ui.LOSE, user, "🎓 유스")
        opp = await self.clubs.reserve_opponent(user.id, t["rating"], now)
        if not opp:
            return ui.card("😶 상대 2군이 없어요", "2군 경기를 할 다른 유저 구단이 아직 없습니다.", ui.EVEN, user, "🎓 유스")
        self._reserve_at[user.id] = now
        res = simulate_match(t, opp)
        gf, ga = res["home"], res["away"]
        hl = match_highlights(res, t, opp, v350=True)
        gap = t["rating"] - opp["rating"]
        stars = await self.clubs.record_prospects([(t["xi"], gf, ga, opp["uid"], gap), (opp["xi"], ga, gf, user.id, -gap)],
                                                  res["goals"], now, hl, v350=True, v360=True, xp_mult=RESERVE_XP,
                                                  xp_label="2군")
        grown = await self.youth.reserve_growth(user.id, t["youth_ids"], gf > ga, now)
        goals = [f"`{g['minute']:>2}'` ⚽ {g['scorer']}" + (f" (도움 {g['assist']})" if g.get("assist") else "")
                 + ("" if g["side"] == "home" else f" *({opp['name']})*") for g in res["goals"]]
        body = "\n".join(goals) or "*골 없이 끝났어요.*"
        body += "".join(Club._star_line(x) for x in stars)
        for x in stars:
            if x["ovr"] >= ENH_MIN_OVR > x["ovr0"]:
                body += f"\n\n🎉 **{x['name']}** OVR {ENH_MIN_OVR} 달성 — **1군 승격!** 이제 `/유망주`에서 강화할 수 있어요"
        if grown["grown"]:
            body += "\n\n🎓 " + " · ".join(f"{n} {a:.1f}→**{b:.1f}**" for n, a, b in grown["grown"])
            body += f"\n-# 오늘 유스 성장 {grown['left']}판 남음"
        elif t["youth_ids"]:
            body += f"\n-# 오늘 유스 성장은 {Y.RESERVE_DAILY}판을 다 채웠어요 (유망주 경험치는 계속)"
        color = ui.WIN if gf > ga else ui.LOSE if gf < ga else ui.EVEN
        return ui.card(f"⚽ {t['name']} {gf} : {ga} {opp['name']}", f"`전력` {t['rating']} vs {opp['rating']}\n\n" + body,
                       color, user, "🎓 유스")

    async def _youth_tryout(self, view: HubView, interaction: discord.Interaction, idx: int):
        user = view.user
        r = await self.youth.tryout(user.id, idx, int(time.time()))
        if not r["ok"]:
            msg = {"balance": f"테스트비 {Y.TRYOUT_FEE:,}원이 부족해요.", "full": "유스 슬롯이 꽉 찼어요.",
                   "done": "오늘은 이미 테스트했어요."}.get(r["reason"], "지원자를 다시 열어 주세요.")
            return await view.refresh(interaction, ui.card("🙅 입단 테스트", msg, ui.LOSE, user, "🎓 유스"))
        view.ctx.pop("apps", None)
        a = r["applicant"]
        title = f"🔎 {a['name']} \"{a['nick']}\" 입단 테스트"
        shown = []
        await interaction.response.defer()
        for i, (scene, ok) in enumerate(zip(Y.SCENES, r["scenes"])):
            shown.append(f"{scene} — {'✅ 통과' if ok else '❌ 실수'}")
            if i < len(Y.SCENES) - 1:
                await interaction.edit_original_response(embeds=[ui.card(title, "\n".join(shown) + "\n…", ui.INFO, user, "🎓 유스")],
                                                         view=view)
                await asyncio.sleep(self.SCENE_DELAY)
        if r["passed"]:
            res = (f"🎉 **합격!** 잠재력 **{a['grade']}등급** · {Y.personality_label(a['personality'])}\n"
                   f"테스트비 {r['fee']:,}원 · 잔액 {r['balance']:,}원")
            view.ctx["yid"] = r["youth"]["id"]
        else:
            res = f"😢 **불합격** — 테스트비 {r['fee']:,}원 중 {r['refund']:,}원 환불 · 잔액 {r['balance']:,}원"
        await view.refresh(interaction, ui.card(title, "\n".join(shown) + "\n\n" + res,
                                                ui.GOLD if r["passed"] else ui.LOSE, user, "🎓 유스"))

    async def _use_note(self, interaction: discord.Interaction, user):
        team = await self.clubs.get_team(user.id)
        if not team or not team["filled"]:
            return await interaction.response.send_message("🙅 선발 명단이 있어야 쓸 수 있어요.", ephemeral=True)
        r = await self.tac.use_note(user.id, [str(p["player_id"]) for p in team["lineup"] if p.get("player_id")],
                                    int(time.time()))
        if not r["ok"]:
            return await interaction.response.send_message("🙅 📓 전술 노트가 없어요.", ephemeral=True)
        await interaction.response.send_message(embed=ui.card(
            "📓 전술 노트 사용!", f"선발 11명의 **{T.label(r['tactic'])}** 적응도 **+{T.NOTE_GAIN}**", ui.GOLD, user, "🎒 아이템"))

    async def _use_report(self, interaction: discord.Interaction, user):
        if not await self.tac.use_report(user.id):
            return await interaction.response.send_message("🙅 📊 분석 리포트가 없어요.", ephemeral=True)
        await interaction.response.send_message(embed=ui.card(
            "📊 분석 리포트 준비!", "다음 경기 킥오프 때 상대 기본 전술과 하프타임 규칙을 알려 드려요.", ui.GOLD, user, "🎒 아이템"))

    async def _p_club(self, view: HubView) -> discord.Embed:
        from cogs.club import _team_embed
        g311 = view.ctx.get("g311")
        team = await self.clubs.get_team(view.user.id)
        e = _team_embed(team, view.user)
        b = discord.ui.Button(label="자동편성", emoji="🔁", style=discord.ButtonStyle.primary, row=0)

        async def auto(interaction: discord.Interaction):
            ok, msg = await self.clubs.auto_lineup(view.user.id, keep_prospect=g311)
            await view.refresh(interaction, ui.card("🔁 자동편성" if ok else "❌ 자동편성", msg,
                                                    ui.WIN if ok else ui.LOSE, view.user, "🏟️ 구단"))
        b.callback = auto
        view.add_item(b)
        if not g311:
            e.set_footer(text="/포메이션 · /선발 · /주장 으로 직접 바꿀 수 있어요")
            return e
        sure = view.ctx.get("confirm") == ("clear", 0)
        c = discord.ui.Button(label="정말 다 빼기" if sure else "선발 전부 빼기", emoji="🧹", row=0,
                              style=discord.ButtonStyle.danger if sure else discord.ButtonStyle.secondary,
                              disabled=not team["filled"] and not any(s.get("injured") for s in team["lineup"]))

        async def clear(interaction: discord.Interaction):
            if not sure:   # 한 번 더 눌러야 실행
                view.ctx["confirm"] = ("clear", 0)
                return await view.refresh(interaction)
            view.ctx.pop("confirm", None)
            n = await self.clubs.clear_lineup(view.user.id)
            await view.refresh(interaction, ui.card("🧹 선발 전부 빼기", f"선발 **{n}명**을 모두 뺐어요. (주장도 해제)\n"
                                                    "📋 편성 화면에서 다시 넣거나 [자동편성]을 눌러 주세요.",
                                                    ui.EVEN, view.user, "🏟️ 구단"))
        c.callback = clear
        view.add_item(c)
        e.set_footer(text="자동편성은 🌟 유망주를 언제나 선발에 넣어요 · 빈자리는 경기 전에 벤치 선수로 자동으로 채워요 · "
                          "▶ 📋 편성에서 포메이션 · 선발 · 주장")
        return e

    # ───────────── 📋 편성 (3.1.1: /포메이션 · /선발 · /주장 을 여기로) ─────────────
    async def _p_lineup(self, view: HubView) -> discord.Embed:
        from cogs.club import _slot_text
        from services.club_db import FORMATIONS, effective_ovr
        user = view.user
        team = await self.clubs.get_team(user.id)
        lineup = team["lineup"]
        idx = view.ctx.get("slot")
        if idx is None or idx >= len(lineup):
            idx = view.ctx["slot"] = next((s["index"] for s in lineup if not s.get("player_id")), 0)
        cur = lineup[idx]
        lines = [("▶ " if s["index"] == idx else "") + _slot_text(s, team["captain"]) for s in lineup]
        e = ui.card("📋 편성", f"`포메이션` **{team['formation']}** · `전력` **{team['rating']}** · `선발` {team['filled']}/11명\n\n"
                    + "\n".join(lines), ui.INFO, user, "🏟️ 구단")
        e.set_footer(text="① 포메이션 ② 바꿀 자리 ③ 넣을 선수 ④ 주장 순서로 골라요 · 같은 선수를 다른 자리에 넣으면 그 자리로 옮겨요")
        # 0줄: 포메이션
        form = discord.ui.Select(placeholder=f"① 포메이션 — 지금 {team['formation']}", row=0, options=[
            discord.SelectOption(label=f, value=f, default=f == team["formation"]) for f in list(FORMATIONS)[:25]])

        async def set_form(interaction: discord.Interaction):
            ok, msg = await self.clubs.set_formation(user.id, form.values[0])
            await view.refresh(interaction, ui.card("📋 포메이션" if ok else "❌ 포메이션", msg,
                                                    ui.WIN if ok else ui.LOSE, user, "🏟️ 구단"))
        form.callback = set_form
        view.add_item(form)
        # 1줄: 자리
        slot = discord.ui.Select(placeholder="② 바꿀 자리", row=1, options=[
            discord.SelectOption(label=f"{s['index'] + 1}번 {s['slot']} — "
                                       f"{s['name'] + ' (' + str(s['ovr']) + ')' if s.get('player_id') else '비어 있음'}"[:100],
                                 value=str(s["index"]), default=s["index"] == idx) for s in lineup])

        async def pick_slot(interaction: discord.Interaction):
            view.ctx["slot"] = int(slot.values[0])
            await view.refresh(interaction)
        slot.callback = pick_slot
        view.add_item(slot)
        # 2줄: 넣을 선수 (고른 자리에 잘 맞는 순)
        where = {s.get("player_id"): s["slot"] for s in lineup if s.get("player_id")}
        squad = sorted(await self.clubs.squad(user.id),
                       key=lambda p: (-effective_ovr(p["ovr"], p["pos"], cur["slot"]), -p["ovr"]))
        opts = [discord.SelectOption(label=f"{cur['slot']} 자리 비우기", value="-", emoji="➖")] if cur.get("player_id") else []
        for p in squad:
            if p["player_id"] == cur.get("player_id"):
                continue
            eff = effective_ovr(p["ovr"], p["pos"], cur["slot"])
            star = "🌟 " if p.get("prospect") else "🎓 " if p.get("youth") else ""
            opts.append(discord.SelectOption(
                label=f"{star}{p['name']} · {p['pos']} · OVR {p['ovr']}" + (f" → {eff}" if eff != p["ovr"] else ""),
                value=p["player_id"], description=f"지금 선발 {where[p['player_id']]}" if p["player_id"] in where else None))
            if len(opts) >= 25:
                break
        if opts:
            put = discord.ui.Select(placeholder=f"③ {idx + 1}번 {cur['slot']} 자리에 넣을 선수", row=2, options=opts)

            async def put_player(interaction: discord.Interaction):
                v = put.values[0]
                ok, msg = await self.clubs.set_slot(user.id, idx, None if v == "-" else v)
                await view.refresh(interaction, ui.card("📋 선발 변경" if ok else "❌ 선발", msg,
                                                        ui.WIN if ok else ui.LOSE, user, "🏟️ 구단"))
            put.callback = put_player
            view.add_item(put)
        # 3줄: 주장
        xi = [s for s in lineup if s.get("player_id")]
        if xi:
            cap = discord.ui.Select(placeholder="④ 주장 (전력 +1)", row=3, options=[
                discord.SelectOption(label=f"{s['slot']} {s['name']} ({s['ovr']})"[:100], value=s["player_id"],
                                     emoji="©️" if s["player_id"] == team["captain"] else None,
                                     default=s["player_id"] == team["captain"]) for s in xi])

            async def set_cap(interaction: discord.Interaction):
                ok, msg = await self.clubs.set_captain(user.id, cap.values[0])
                await view.refresh(interaction, ui.card("©️ 주장 임명" if ok else "❌ 주장", msg,
                                                        ui.WIN if ok else ui.LOSE, user, "🏟️ 구단"))
            cap.callback = set_cap
            view.add_item(cap)
        return e

    async def _p_prospect(self, view: HubView) -> discord.Embed:
        from cogs.prospect import prospect_embed
        now = int(time.time())
        data = await self.clubs.prospects(view.user.id, now)
        if data["active"]:
            return prospect_embed(data["active"], view.user, now)
        return ui.card("🌟 유망주", f"아직 현역 유망주가 없어요.\n`/유망주`에서 **{PROSPECT_PRICE:,}원**에 나만의 선수를 만들어 보세요!"
                       + (f"\n\n🏛️ 은퇴한 유망주 {len(data['retired'])}명 — `/유망주`" if data["retired"] else ""),
                       ui.EVEN, view.user, "🌟 유망주")

    async def _p_facility(self, view: HubView) -> discord.Embed:
        lv = await self.clubs.facilities(view.user.id)
        lines = []
        for k, (e_, n_, _, _) in FACILITIES.items():
            nxt = f"다음 **{_won(FACILITY_COSTS[lv[k]])}원** → {facility_effect(k, lv[k] + 1)}" if lv[k] < FACILITY_MAX else "최고 레벨"
            lines.append(f"{e_} **{n_}** Lv.{lv[k]}/{FACILITY_MAX} · {facility_effect(k, lv[k])}\n　{nxt}")
        e = ui.card("🏗️ 시설", "\n".join(lines), ui.INFO, view.user, "🏗️ 시설")
        if not view.ctx.get("g311"):
            e.set_footer(text="/시설 업그레이드:<시설> 로 한 단계 올려요")
            return e
        e.set_footer(text="버튼을 두 번 누르면 한 단계 올려요 · 시설은 구단을 지워도 남아요")
        for k, (e_, n_, _, _) in FACILITIES.items():   # 3.1.1: /시설 을 여기로
            sure = view.ctx.get("confirm") == ("facility", k)
            top = lv[k] >= FACILITY_MAX
            b = discord.ui.Button(label=f"{n_} 최고" if top else
                                  (f"정말 올리기 {_won(FACILITY_COSTS[lv[k]])}원" if sure else f"{n_} Lv.{lv[k] + 1}"),
                                  emoji=e_, row=0, disabled=top,
                                  style=discord.ButtonStyle.danger if sure else discord.ButtonStyle.primary)
            b.callback = self._facility_cb(view, k)
            view.add_item(b)
        return e

    def _facility_cb(self, view: HubView, key: str):
        async def cb(interaction: discord.Interaction):
            if view.ctx.get("confirm") != ("facility", key):   # 한 번 더 눌러야 실행 (비싸다)
                view.ctx["confirm"] = ("facility", key)
                return await view.refresh(interaction)
            view.ctx.pop("confirm", None)
            r = await self.clubs.upgrade_facility(view.user.id, key)
            e_, n_ = FACILITIES[key][:2]
            note = ui.card(f"🏗️ {e_} {n_} Lv.{r['level']} 완공!", f"`비용` **-{r['fee']:,}원** · `잔액` **{r['balance']:,}원**\n"
                           f"`효과` {facility_effect(key, r['level'])}", ui.GOLD, view.user, "🏗️ 시설") if r["ok"] else ui.card(
                "❌ 업그레이드 실패", {"no_club": "구단이 없어요.", "max": f"{e_} {n_}은(는) 이미 최고 레벨이에요.",
                                   "balance": f"업그레이드 비용 **{r.get('fee', 0):,}원**이 필요해요."}[r["reason"]],
                ui.LOSE, view.user, "🏗️ 시설")
            await view.refresh(interaction, note)
        return cb

    async def _p_staff(self, view: HubView) -> discord.Embed:
        team = await self.clubs.get_team(view.user.id)
        mg, md = team.get("manager"), team.get("medic")
        lines = ["**👔 감독** — 지금: " + (f"{MANAGERS[mg][0]} {MANAGERS[mg][1]} (+{team['manager_bonus']})" if mg in MANAGERS else "없음")]
        for k, (e_, n_, fav, base, extra, fee, _) in MANAGERS.items():
            lines.append(f"　{e_} {n_} · +{base}" + (f" (+{extra} {fav})" if fav else "") + f" · {_won(fee)}원")
        lines.append("\n**🩺 의료진** — 지금: " + (f"{MEDICS[md][0]} {MEDICS[md][1]}" if md in MEDICS else "없음"))
        for k, (e_, n_, heal, fee, _) in MEDICS.items():
            lines.append(f"　{e_} {n_} · 치료 {heal}% · {_won(fee)}원")
        e = ui.card("👔 감독 · 의료진", "\n".join(lines), ui.INFO, view.user, "🏟️ 구단")
        if not view.ctx.get("g311"):
            e.set_footer(text="/감독 · /의료진 으로 영입해요")
            return e
        e.set_footer(text="고른 뒤 [영입]을 누르면 영입비를 내요 · 영입비는 한 번만 · 바꾸면 새 영입비 · 부상은 유망주만 당해요")
        pick = view.ctx.get("hire")   # 3.1.1: /감독 · /의료진 을 여기로 — (col, key)
        sc = await self.youth.scouter(view.user.id)
        e.description += ("\n\n**🕵️ 유스 스카우터** — 지금: " + (f"{Y.SCOUTERS[sc][0]} {Y.SCOUTERS[sc][1]}" if sc in Y.SCOUTERS else "없음")
                          + f" (정확도 {Y.scout_acc(sc):.0%})\n" + "\n".join(
                              f"　{v[0]} {v[1]} · 정확도 {v[2]:.0%} · {_won(v[3])}원" for v in Y.SCOUTERS.values())
                          + "\n-# 스카우터가 정확할수록 🎓 유스 지원자 · 선수의 잠재력 별과 한마디가 잘 맞아요")
        fees = {"manager": lambda v: v[5], "medic": lambda v: v[3], "scouter": lambda v: v[3]}
        tables = {"manager": MANAGERS, "medic": MEDICS, "scouter": Y.SCOUTERS}
        for row, col, label in ((0, "manager", "👔 감독 고르기"), (1, "medic", "🩺 의료진 고르기"), (3, "scouter", "🕵️ 유스 스카우터 고르기")):
            cur = sc if col == "scouter" else team.get(col)
            sel = discord.ui.Select(placeholder=label, row=row, options=[
                discord.SelectOption(label=f"{v[1]} · {_won(fees[col](v))}원" + (" (지금)" if k == cur else ""), value=k,
                                     emoji=v[0], default=pick == (col, k)) for k, v in tables[col].items()])
            sel.callback = self._hire_pick(view, col, sel)
            view.add_item(sel)
        if pick:
            col, k = pick
            v = tables[col][k]
            cur = sc if col == "scouter" else team.get(col)
            b = discord.ui.Button(label=f"{v[1]} {'고용' if col == 'scouter' else '영입'} ({_won(fees[col](v))}원)", emoji="🤝",
                                  style=discord.ButtonStyle.success, row=2, disabled=cur == k)
            b.callback = self._hire_go(view)
            view.add_item(b)
        return e

    def _hire_pick(self, view: HubView, col: str, sel):
        async def cb(interaction: discord.Interaction):
            view.ctx["hire"] = (col, sel.values[0])
            await view.refresh(interaction)
        return cb

    def _hire_go(self, view: HubView):
        async def cb(interaction: discord.Interaction):
            col, k = view.ctx.pop("hire")
            user = view.user
            if col == "scouter":
                r = await self.clubs.hire_scouter(user.id, k)
                e_, n_, acc, fee = Y.SCOUTERS[k]
                desc, eff = "좋은 재능은 제가 먼저 알아봅니다", f"유스 잠재력 별 · 한마디 정확도 **{acc:.0%}**"
                title = f"🤝 {e_} {n_} 합류!"
            elif col == "manager":
                r = await self.clubs.hire_manager(user.id, k)
                e_, n_, fav, base, extra, fee, desc = MANAGERS[k]
                eff = f"전력 +{base}" + (f" · **{fav}** 포메이션이면 +{extra} 추가" if fav else "")
                title = f"🤝 {e_} {n_} 감독 부임!"
            else:
                r = await self.clubs.hire_medic(user.id, k)
                e_, n_, heal, fee, desc = MEDICS[k]
                eff = f"유망주 부상 결장 기간 **-{heal}%**"
                title = f"🤝 {e_} {n_} 합류!"
            note = ui.card(title, f"> 💬 *\"{desc}\"*\n\n`영입비` **-{fee:,}원** · `잔액` **{r['balance']:,}원**\n`효과` {eff}",
                           ui.WIN, user, "🏟️ 구단") if r["ok"] else ui.card(
                "❌ 영입 실패", {"no_club": "구단이 없어요.", "same": f"이미 {e_} {n_}와(과) 함께하고 있어요.",
                               "balance": f"영입비 **{fee:,}원**이 필요해요. (잔액 {r.get('balance', 0):,}원)"}[r["reason"]],
                ui.LOSE, user, "🏟️ 구단")
            await view.refresh(interaction, note)
        return cb

    # ───────────── /시장 허브 (2.8 서버) ─────────────
    def market_view(self, user, page: int = 0) -> HubView:
        pages = [Page("🟢", "시장 상태", self._p_status, "운영 시간 · 할 수 있는 것"),
                 Page("🏟️", "이적시장", self._p_listings, "유저 매물 최신 10개"),
                 Page("🃏", "선수팩", self._p_packs, "팩 가격"),
                 Page("💼", "내 선수", self._p_holdings, "보유 카드 · 시세")]
        return HubView(user, "시장", "📈 시장", pages, "/시장", page)

    async def _p_status(self, view: HubView) -> discord.Embed:
        st = await self.pm.market_status(int(time.time()))
        return ui.card("📈 선수 시장", f"{'🟢' if st.is_open else '🔴'} **{st.reason}**\n"
                       f"다음 변경: <t:{st.next_change_ts}:f>\n\n"
                       "`/선수` 검색 · `/시세` 그래프 · `/선수뉴스`\n`/판매` → 12시간 뒤 메뉴에서 매각(70%) · `/즉시판매`(50%)\n"
                       "`/이적시장` · `/구매` · `/트레이드` · `/선수팩상점` · `/아이템상점`", ui.INFO, view.user, "📈 시장")

    async def _p_listings(self, view: HubView) -> discord.Embed:
        rows = await self.pm.get_listings("", limit=10, offset=0)
        lines = [f"`#{lid}` **{name}** · {pos} OVR **{ovr}** · **{int(price):,}원** × {qty}장"
                 for lid, _, _, qty, price, _, _, _, name, _, pos, _, ovr, *_ in rows]
        return ui.card("🏟️ 이적시장 · 최신 매물", "\n".join(lines) or "현재 등록된 매물이 없습니다.", ui.INFO, view.user, "📈 시장")

    async def _p_packs(self, view: HubView) -> discord.Embed:
        lines = [f"**{k}** — {_won(v['price'])}원" + (" · 잭팟" if v.get("jackpot") else "") + (f" · {v['pos']}만" if v.get("pos") else "")
                 for k, v in PACKS.items()]
        return ui.card("🃏 선수팩", "\n".join(lines) + "\n\n`/선수팩상점`에서 팩을 고르고 몇 장 살지 정해요 · `/팩정보`",
                       ui.INFO, view.user, "📈 시장")

    async def _p_holdings(self, view: HubView) -> discord.Embed:
        rows = await self.pm.list_holdings(view.user.id, limit=15)
        lines = [f"**{name}** · {pos} OVR {ovr}" + (" · 💤 은퇴" if retired else "") + f" · {qty}장 · {int(price):,}원"
                 for pid, name, _n, pos, _a, ovr, _g, retired, qty, price in rows if not str(pid).startswith("AMT_")]
        return ui.card("💼 내 선수 카드", "\n".join(lines) or "아마추어 말고는 아직 선수 카드가 없어요. `/선수팩상점`!",
                       ui.INFO, view.user, "📈 시장")


async def setup(bot: commands.Bot):
    await bot.add_cog(Hubs(bot))
