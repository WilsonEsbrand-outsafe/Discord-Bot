# cogs/hub.py — 2.8 허브: /오늘 · /게임 (새 명령어) + /구단 · /시장 허브 화면 (2.8 서버에서 내 구단 · 시장을 열 때)
# 한 메시지를 계속 고쳐 쓰고 맨 아래 줄은 ◀ 🏠 ▶ (services/hub.py). 예전 명령어(/출석 /시설 /이적시장 …)는 그대로 남는다.
import time

import discord
from discord import app_commands
from discord.ext import commands

from services import ui
from services.club_db import (
    FACILITIES, FACILITY_COSTS, FACILITY_MAX, MANAGERS, MEDICS, PROSPECT_PRICE, ClubDB, facility_effect,
)
from services.dungeon import THEMES, DungeonDB, theme_of
from services.economy_db import (
    ROOKIE_GRIND_MULT, SCOUT_DAILY_LIMIT, TRAIN_DAILY_LIMIT, WATCH_DAILY_LIMIT, EconomyDB, rookie_until,
)
from services.fishing import FishingDB
from services.hub import HubView, Page
from services import tactics as T
from services.tactics import TacticsDB
from services.player_market_db import PACKS, PlayerMarketDB

ATTEND_REWARD = 30_000   # cogs.economy.Economy.ATTEND_REWARD 와 같다


def _check(done: bool, text: str) -> str:
    return f"{'✅' if done else '⬜'} {text}"


def _won(n: int) -> str:
    return f"{n // 100_000_000}억" if n >= 100_000_000 and n % 100_000_000 == 0 else (
        f"{n // 10_000:,}만" if n >= 10_000 and n % 10_000 == 0 else f"{n:,}")


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
    ("🏹", "양궁", "바늘을 가운데에 맞춰 6발", (
        "`/양궁 베팅액` — 궁수와 함께 6발. 조준 바늘이 가운데일 때 [쏘기!]\n"
        "합계 57점 이상 +1.5배 · X(정중앙) 3개 이상 보너스\n"
        "🏹 고급 활 · 🧥 바람막이 · 🧪 집중 물약은 던전에서 얻어요")),
    ("🏆", "리그", "내 구단으로 한 시즌", (
        "`/리그` — 내 구단을 랜덤 리그에 넣고 한 시즌\n우승 20배 ~ 강등 6배 손실")),
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

    async def cog_load(self):
        from cogs.economy import ITEM_HANDLERS
        ITEM_HANDLERS.update({"tactic_note": self._use_note, "analysis_report": self._use_report})

    async def cog_unload(self):
        from cogs.economy import ITEM_HANDLERS
        for k in ("tactic_note", "analysis_report"):
            ITEM_HANDLERS.pop(k, None)

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
        pages = [Page(e, t, render(i), hint) for i, (e, t, hint, _) in enumerate(GAMES)]
        return HubView(user, "게임", "🎮 게임", pages, "/게임", page, home_note="해 보고 싶은 게임을 골라 보세요.")

    @app_commands.command(name="게임", description="🎮 미니게임 모음 — 레이스 · 한 방 게임 · 양궁 · 리그 · 낚시 · 던전 · 토토")
    async def games(self, interaction: discord.Interaction):
        await self.games_view(interaction.user).send(interaction)

    # ───────────── /구단 허브 (2.8 서버 · 내 구단) ─────────────
    def club_view(self, user, page: int = 0) -> HubView:
        pages = [Page("🏟️", "구단", self._p_club, "선발 명단 · 전력 · 전적"),
                 Page("🌟", "유망주", self._p_prospect, "나만의 선수"),
                 Page("🏗️", "시설", self._p_facility, "경기장 · 훈련장 · 유스 · 메디컬"),
                 Page("👔", "감독 · 의료진", self._p_staff, "전력 보너스 · 부상 치료"),
                 Page("⚔️", "전술", self._p_tactics, "기본 전술 · 적응도 · 하프타임 규칙"),
                 Page("🔍", "상대 분석", self._p_scout, "상대 최근 5경기 전술")]
        return HubView(user, "내 구단", "🏟️ 구단", pages, "/구단", page)

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
            comp = T.composition(xi, t)
            mark = " ✅ **기본 전술**" if t == s["tactic"] else ""
            lines.append(f"{e_} **{n_}** — {T.label(T.BEATS[t])}에 강함 · 중요 능력치 {attr}{mark}\n"
                         f"　`적응도` {ui.bar(fit, 100, 8)} {fit:.0f} · `구성` ×{comp:.2f} · `효과` {T.effect(fit, comp):.0%}")
        rules = "\n".join(f"　{n} → **{T.label(s['rules'][k]) if s['rules'].get(k) else '유지'}**" for k, n in T.RULE_STATES)
        e = ui.card("⚔️ 전술", "🔥 압박 > 🎯 점유 > ⚡ 역습 > 🔥 압박 · 유리하면 기대 득점 최대 +6%, 불리하면 -6%\n"
                    "모든 경기(친선 · 공식)에 기본 전술로 나가요. 상대 전술은 하프타임에 공개돼요.\n\n"
                    + "\n".join(lines) + f"\n\n**하프타임 규칙** (내가 고르지 않을 때 · 상대 쪽일 때)\n{rules}",
                    ui.INFO, user, "⚔️ 전술")
        e.set_footer(text=f"적응도: 그 전술로 뛰면 +{T.FIT_GAIN} · {T.FIT_IDLE_DAYS}일 넘게 안 쓰면 하루 -{T.FIT_DECAY} · "
                          "효과 = (0.5 + 0.5 × 적응도) × 구성")
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
        body = "상대를 고르면 **최근 5경기** 전술을 보여 드려요. 기본 전술 자체는 경기 전에 공개되지 않아요.\n" \
               "📊 분석 리포트(아이템)를 쓰면 다음 경기 킥오프 때 상대 기본 전술과 하프타임 규칙이 보여요."
        if opp_id:
            rows = await self.tac.recent(opp_id, 5)
            m = self.bot.get_user(opp_id) if self.bot else None
            name = m.display_name if m else f"<@{opp_id}>"
            if rows:
                cnt = {t: sum(s == t for s, _ in rows) for t in T.TACTICS}
                sw = sum(s != e for s, e in rows)
                body += (f"\n\n**{name}** 최근 {len(rows)}경기 — 시작 전술 "
                         + " · ".join(f"{T.label(t)} {c}" for t, c in cnt.items() if c)
                         + f"\n하프타임에 바꾼 경기 **{sw}번**\n"
                         + "\n".join(f"　{i}. {T.label(s)}" + (f" → {T.label(e)}" if e != s else "")
                                      for i, (s, e) in enumerate(rows, 1)))
            else:
                body += f"\n\n**{name}** — 2.8 전술 경기 기록이 아직 없어요."
        e = ui.card("🔍 상대 분석", body, ui.INFO, user, "⚔️ 전술")
        pick = discord.ui.UserSelect(placeholder="🔍 분석할 상대 고르기", row=0)

        async def chosen(interaction: discord.Interaction):
            view.ctx["scout"] = pick.values[0].id
            await view.refresh(interaction)
        pick.callback = chosen
        view.add_item(pick)
        return e

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
        team = await self.clubs.get_team(view.user.id)
        e = _team_embed(team, view.user)
        b = discord.ui.Button(label="자동편성", emoji="🔁", style=discord.ButtonStyle.primary, row=0)

        async def auto(interaction: discord.Interaction):
            ok, msg = await self.clubs.auto_lineup(view.user.id)
            await view.refresh(interaction, ui.card("🔁 자동편성" if ok else "❌ 자동편성", msg,
                                                    ui.WIN if ok else ui.LOSE, view.user, "🏟️ 구단"))
        b.callback = auto
        view.add_item(b)
        e.set_footer(text="/포메이션 · /선발 · /주장 으로 직접 바꿀 수 있어요")
        return e

    async def _p_prospect(self, view: HubView) -> discord.Embed:
        from cogs.prospect import prospect_embed
        now = int(time.time())
        data = await self.clubs.prospects(view.user.id, now)
        if data["active"]:
            return prospect_embed(data["active"], view.user, now)
        return ui.card("🌟 유망주", f"아직 현역 유망주가 없어요.\n`/유망주생성`으로 **{PROSPECT_PRICE:,}원**에 나만의 선수를 만들어 보세요!"
                       + (f"\n\n🏛️ 은퇴한 유망주 {len(data['retired'])}명 — `/유망주`" if data["retired"] else ""),
                       ui.EVEN, view.user, "🌟 유망주")

    async def _p_facility(self, view: HubView) -> discord.Embed:
        lv = await self.clubs.facilities(view.user.id)
        lines = []
        for k, (e_, n_, _, _) in FACILITIES.items():
            nxt = f"다음 **{_won(FACILITY_COSTS[lv[k]])}원** → {facility_effect(k, lv[k] + 1)}" if lv[k] < FACILITY_MAX else "최고 레벨"
            lines.append(f"{e_} **{n_}** Lv.{lv[k]}/{FACILITY_MAX} · {facility_effect(k, lv[k])}\n　{nxt}")
        e = ui.card("🏗️ 시설", "\n".join(lines), ui.INFO, view.user, "🏗️ 시설")
        e.set_footer(text="/시설 업그레이드:<시설> 로 한 단계 올려요")
        return e

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
        e.set_footer(text="/감독 · /의료진 으로 영입해요")
        return e

    # ───────────── /시장 허브 (2.8 서버) ─────────────
    def market_view(self, user, page: int = 0) -> HubView:
        pages = [Page("🟢", "시장 상태", self._p_status, "운영 시간 · 할 수 있는 것"),
                 Page("🏟️", "이적시장", self._p_listings, "유저 매물 최신 10개"),
                 Page("🃏", "선수팩", self._p_packs, "팩 가격"),
                 Page("💼", "내 선수", self._p_holdings, "보유 카드 · 시세")]
        return HubView(user, "시장", "📈 시장", pages, "/시장", page)

    async def _p_status(self, view: HubView) -> discord.Embed:
        st = await self.pm.market_status(int(time.time()))
        return ui.card("📈 선수 시장", f"{'🟢' if st.is_open else '🔴'} **{st.reason}**\n운영 시간: 매일 **09:00 ~ 23:00 KST**\n"
                       f"다음 변경: <t:{st.next_change_ts}:f>\n\n"
                       "`/선수` 검색 · `/시세` 그래프 · `/선수뉴스`\n`/판매` → 12시간 뒤 `/매각`(70%) · `/즉시판매`(50%)\n"
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
