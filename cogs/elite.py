# cogs/elite.py — 2.5 명문 구단(스쿼드 B) · 시설 업그레이드 · 구단 꾸미기
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from services import ui
from services.club_db import (
    ELITE_CLUBS, ELITE_SIZES, ELITE_TAKEOVER, EMBLEM_PRICE, EMBLEMS, FACILITIES, FACILITY_COSTS, FACILITY_MAX,
    SLOT_GROUP, STADIUM_NAME_MAX, STADIUM_PRICE, ClubDB, elite_team, facility_effect,
)

SECTION = "🏟️ 구단"
_NO_CLUB = "아직 구단이 없습니다. `/구단생성`으로 먼저 만들어 주세요."


def _eok(n: int) -> str:
    """1,500,000,000 → '15억', 250,000,000 → '2.5억', 30,000,000 → '3,000만'."""
    if n >= 100_000_000:
        return f"{n / 100_000_000:,.2f}".rstrip("0").rstrip(".") + "억"
    return f"{n // 10_000:,}만"


class BuyConfirm(discord.ui.View):
    def __init__(self, cog: "Elite", user, club: dict):
        super().__init__(timeout=120)
        self.cog, self.user, self.club = cog, user, club
        self.buy.label = f"{_eok(club['cost'])}원에 인수"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("🙅 명령어를 쓴 사람만 누를 수 있어요.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="인수", emoji="🤝", style=discord.ButtonStyle.success)
    async def buy(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        c = self.club
        r = await self.cog.clubs.buy_elite(self.user.id, c["key"], int(time.time()))
        if not r["ok"]:
            msg = {"no_club": _NO_CLUB, "mine": "이미 내 구단이에요.",
                   "one": "명문 구단은 한 사람에 하나만 가질 수 있어요.",
                   "balance": f"인수 금액 **{r.get('cost', 0):,}원**이 필요해요."}[r["reason"]]
            return await interaction.response.edit_message(
                embed=ui.card("❌ 인수 실패", msg, ui.LOSE, self.user, SECTION), view=None)
        prev = f"\n`전 구단주` <@{r['prev_owner']}> — 인수 금액을 받았어요" if r["prev_owner"] else ""
        e = ui.card(f"{c['emblem']} {c['name']} 인수 완료!",
                    f"`인수 금액` **{r['cost']:,}원** · `잔액` **{r['balance']:,}원**{prev}\n"
                    f"`하루 수입` **{c['income']:,}원** (매일 00시)\n\n"
                    "`/스쿼드 선택:B`로 바꾸면 이 구단으로 경기해요.", ui.GOLD, self.user, SECTION)
        await interaction.response.edit_message(embed=e, view=None)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            embed=ui.card("인수 취소", "아무것도 바뀌지 않았어요.", ui.EVEN, self.user, SECTION), view=None)


class Elite(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.clubs = ClubDB()
        self.income_loop.start()

    def cog_unload(self):
        self.income_loop.cancel()

    @tasks.loop(minutes=10)
    async def income_loop(self):
        for x in await self.clubs.settle_elite_income(int(time.time())):
            print(f"[명문] {x['key']} → {x['owner_id']} +{x['amount']:,} ({x['days']}일)")

    @income_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ───────────── 명문 구단 ─────────────
    @app_commands.command(name="명문구단", description="명문 구단 10개 — 인수하면 스쿼드 B로 경기 · 매일 수입 (비우면 목록)")
    @app_commands.describe(구단="자세히 보거나 인수할 구단")
    @app_commands.choices(구단=[app_commands.Choice(name=f"{e} {n} ({s})", value=k) for k, (e, n, s) in ELITE_CLUBS.items()])
    async def elite(self, interaction: discord.Interaction, 구단: Optional[str] = None):
        user = interaction.user
        clubs = await self.clubs.elite_list()
        if not 구단:
            lines = []
            for c in clubs:
                owner = f"<@{c['owner_id']}>" if c["owner_id"] else "주인 없음"
                lines.append(f"{c['emblem']} **{c['name']}** · {c['size']} · 전력 **{c['rating']}**\n"
                             f"　{owner} · 인수 **{_eok(c['cost'])}원** · 하루 +{_eok(c['income'])}원")
            e = ui.card("🏰 명문 구단", "\n".join(lines), ui.GOLD, user, SECTION)
            e.set_footer(text=f"주인 없음은 표시 가격 · 주인 있으면 지금 가격 ×{ELITE_TAKEOVER} (전 주인에게) · "
                              "한 사람 한 구단 · /명문구단 구단:<이름>")
            return await interaction.response.send_message(embed=e)

        c = next(x for x in clubs if x["key"] == 구단)
        team = elite_team(구단)
        rows = []
        for group, label in (("FW", "⚽ 공격"), ("MF", "🎯 미드필드"), ("DF", "🛡️ 수비"), ("GK", "🧤 골키퍼")):
            ps = [f"{s['name']} {s['ovr']}" for s in team["lineup"] if SLOT_GROUP[s["slot"]] == group]
            rows.append(f"**{label}** " + " · ".join(ps))
        owner = f"<@{c['owner_id']}>" if c["owner_id"] else "없음"
        e = ui.card(f"{c['emblem']} {c['name']}",
                    f"`규모` {c['size']} · `전력` **{c['rating']}** · `포메이션` {team['formation']}\n"
                    f"`구단주` {owner} · `하루 수입` **{c['income']:,}원**\n"
                    f"`인수 금액` **{c['cost']:,}원**" + (f" (지금 가격 {c['price']:,}원 ×{ELITE_TAKEOVER})" if c["owner_id"] else "")
                    + "\n\n" + "\n".join(rows) + "\n\n-# 선수 능력치는 바뀌지 않아요 (성장 · 노화 · 부상 없음)",
                    ui.INFO, user, SECTION)
        view = None if c["owner_id"] == user.id else BuyConfirm(self, user, c)
        await interaction.response.send_message(embed=e, view=view)

    @app_commands.command(name="스쿼드", description="경기에 쓸 스쿼드 — A: 내 구단 · B: 내 명문 구단 (비우면 보기)")
    @app_commands.describe(선택="A 또는 B")
    @app_commands.choices(선택=[app_commands.Choice(name="A — 내 구단", value="A"),
                              app_commands.Choice(name="B — 명문 구단", value="B")])
    async def squad(self, interaction: discord.Interaction, 선택: Optional[str] = None):
        user = interaction.user
        if 선택:
            r = await self.clubs.set_squad(user.id, 선택 == "B")
            if not r["ok"]:
                msg = _NO_CLUB if r["reason"] == "no_club" else "명문 구단이 없어요. `/명문구단`에서 인수해 보세요."
                return await interaction.response.send_message(
                    embed=ui.card("❌ 스쿼드", msg, ui.LOSE, user, SECTION), ephemeral=True)
        a = await self.clubs.get_team(user.id)
        if not a:
            return await interaction.response.send_message(_NO_CLUB, ephemeral=True)
        use_b = a["squad_b"] and a["elite"]
        lines = [f"{'▶️' if not use_b else '▫️'} **A** {a.get('emblem') or '🏟️'} {a['name']} · 전력 **{a['rating']}**"]
        if a["elite"]:
            b = elite_team(a["elite"])
            lines.append(f"{'▶️' if use_b else '▫️'} **B** {b['emblem']} {b['name']} · 전력 **{b['rating']}**")
        else:
            lines.append("▫️ **B** 없음 — `/명문구단`에서 인수하면 생겨요")
        e = ui.card("📋 스쿼드" + (f" — {선택}로 변경" if 선택 else ""),
                    "\n".join(lines) + "\n\n▶️ 스쿼드로 `/친선경기` `/공식경기`에 나가요 (상대가 걸어올 때도).\n"
                    "-# 유망주 · 감독 · 머플러 효과는 스쿼드 A에만 · 선발 편성은 A만 바꿀 수 있어요",
                    ui.WIN if 선택 else ui.INFO, user, SECTION)
        await interaction.response.send_message(embed=e)

    # ───────────── 시설 ─────────────
    @app_commands.command(name="시설", description="경기장 · 훈련장 · 유스 아카데미 · 메디컬 센터 업그레이드 (비우면 보기)")
    @app_commands.describe(업그레이드="한 단계 올릴 시설")
    @app_commands.choices(업그레이드=[app_commands.Choice(name=f"{e} {n}", value=k) for k, (e, n, _, _) in FACILITIES.items()])
    async def facility(self, interaction: discord.Interaction, 업그레이드: Optional[str] = None):
        user = interaction.user
        title, color = "🏗️ 시설", ui.INFO
        if 업그레이드:
            r = await self.clubs.upgrade_facility(user.id, 업그레이드)
            e_, n_ = FACILITIES[업그레이드][:2]
            if not r["ok"]:
                msg = {"no_club": _NO_CLUB, "max": f"{e_} {n_}은(는) 이미 최고 레벨이에요.",
                       "balance": f"업그레이드 비용 **{r.get('fee', 0):,}원**이 필요해요."}[r["reason"]]
                return await interaction.response.send_message(
                    embed=ui.card("❌ 업그레이드 실패", msg, ui.LOSE, user, SECTION), ephemeral=True)
            title, color = f"🏗️ {e_} {n_} Lv.{r['level']} 완공! (-{r['fee']:,}원)", ui.GOLD
        lv = await self.clubs.facilities(user.id)
        lines = []
        for k, (e_, n_, _, _) in FACILITIES.items():
            nxt = (f"다음 **{_eok(FACILITY_COSTS[lv[k]])}원** → {facility_effect(k, lv[k] + 1)}"
                   if lv[k] < FACILITY_MAX else "최고 레벨")
            lines.append(f"{e_} **{n_}** Lv.{lv[k]}/{FACILITY_MAX} · {facility_effect(k, lv[k])}\n　{nxt}")
        e = ui.card(title, "\n".join(lines), color, user, SECTION)
        e.set_footer(text="/시설 업그레이드:<시설> · 시설은 구단을 지워도 남아요")
        await interaction.response.send_message(embed=e)

    # ───────────── 꾸미기 ─────────────
    @app_commands.command(name="구단꾸미기",
                          description=f"엠블럼({EMBLEM_PRICE // 10_000:,}만원) · 경기장 이름({STADIUM_PRICE // 10_000:,}만원) 바꾸기")
    @app_commands.describe(엠블럼="구단 이름 앞에 붙는 엠블럼", 경기장="경기장 이름 (경기 중계에 나와요)")
    @app_commands.choices(엠블럼=[app_commands.Choice(name=e, value=e) for e in EMBLEMS])
    async def decorate(self, interaction: discord.Interaction, 엠블럼: Optional[str] = None,
                       경기장: Optional[app_commands.Range[str, 1, STADIUM_NAME_MAX]] = None):
        user = interaction.user
        if not 엠블럼 and not 경기장:
            team = await self.clubs.get_team(user.id)
            if not team:
                return await interaction.response.send_message(_NO_CLUB, ephemeral=True)
            e = ui.card("🎨 구단 꾸미기",
                        f"`엠블럼` {team.get('emblem') or '없음'} · {EMBLEM_PRICE:,}원\n"
                        f"`경기장` {team.get('stadium') or '없음'} · {STADIUM_PRICE:,}원\n\n"
                        "`/구단꾸미기 엠블럼: 경기장:`으로 바꿀 수 있어요.", ui.INFO, user, SECTION)
            return await interaction.response.send_message(embed=e, ephemeral=True)
        await interaction.response.defer()
        done, fail = [], []
        for col, value, label in (("emblem", 엠블럼, "엠블럼"), ("stadium", (경기장 or "").strip() or None, "경기장")):
            if not value:
                continue
            r = await self.clubs.decorate(user.id, col, value)
            if r["ok"]:
                done.append(f"`{label}` **{value}** (-{r['fee']:,}원)")
            else:
                fail.append({"no_club": _NO_CLUB, "same": f"{label}이(가) 이미 **{value}**이에요.",
                             "balance": f"{label} 변경에 **{r.get('fee', 0):,}원**이 필요해요."}[r["reason"]])
        team = await self.clubs.get_team(user.id)
        name = f"{team.get('emblem') or '🏟️'} {team['name']}" if team else ""
        e = ui.card(f"🎨 {name}" if done else "❌ 꾸미기 실패", "\n".join(done + fail), ui.GOLD if done else ui.LOSE,
                    user, SECTION)
        await interaction.followup.send(embed=e)


async def setup(bot: commands.Bot):
    await bot.add_cog(Elite(bot))
