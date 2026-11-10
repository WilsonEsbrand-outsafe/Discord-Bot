# cogs/elite.py — 2.5 명문 구단(스쿼드 B) · 시설 업그레이드 · 구단 꾸미기
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import release
from services import ui
from services.management import MgmtDB
from services.club_db import (
    ELITE_OFFER_HOURS, ELITE_CLUBS, elite_ask_range, ELITE_SELL_RATE, ELITE_SIZES, ELITE_TAKEOVER, EMBLEM_PRICE, EMBLEMS, FACILITIES, FACILITY_COSTS,
    FACILITY_MAX, SLOT_GROUP, STADIUM_NAME_MAX, STADIUM_PRICE, ClubDB, elite_team, facility_effect,
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
        r = await self.cog.clubs.buy_elite(self.user.id, c["key"], int(time.time()),
                                           v381=release.preview_381(interaction.guild_id))
        if not r["ok"]:
            msg = {"no_club": _NO_CLUB, "mine": "이미 내 구단이에요.",
                   "one": "명문 구단은 한 사람에 하나만 가질 수 있어요.",
                   "balance": f"인수 금액 **{r.get('cost', 0):,}원**이 필요해요.",
                   "offer": "주인이 있는 구단이에요. `/명문구단`에서 다시 열어 인수 제안을 해 주세요."}[r["reason"]]
            return await interaction.response.edit_message(
                embed=ui.card("❌ 인수 실패", msg, ui.LOSE, self.user, SECTION), view=None)
        prev = f"\n`전 구단주` <@{r['prev_owner']}> — 인수 금액을 받았어요" if r["prev_owner"] else ""
        e = ui.card(f"{c['emblem']} {c['name']} 인수 완료!",
                    f"`인수 금액` **{r['cost']:,}원** · `잔액` **{r['balance']:,}원**{prev}\n"
                    f"`하루 수입` **{c['income']:,}원** "
                    + ("(매일 쌓이고 `/명문구단`에서 수령)" if release.preview_370(interaction.guild_id) else "(매일 00시)") + "\n\n"
                    "`/스쿼드 선택:B`로 바꾸면 이 구단으로 경기해요.", ui.GOLD, self.user, SECTION)
        await interaction.response.edit_message(embed=e, view=None)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            embed=ui.card("인수 취소", "아무것도 바뀌지 않았어요.", ui.EVEN, self.user, SECTION), view=None)


_OFFER_FAIL = {"no_club": _NO_CLUB, "mine": "이미 내 구단이에요.", "one": "명문 구단은 한 사람에 하나만 가질 수 있어요.",
               "free": "주인이 없는 구단이에요. 그냥 인수할 수 있어요.", "amount": "제안 금액은 1원 이상이어야 해요.",
               "gone": "없어졌거나 만료된 제안이에요."}


def _offer_fail(r: dict) -> str:
    if r["reason"] == "balance":
        return f"제안 금액 **{r.get('cost', r.get('amount', 0)):,}원**만큼 잔액이 있어야 해요."
    return _OFFER_FAIL.get(r["reason"], "인수할 수 없어요.")


class OfferModal(discord.ui.Modal):
    """3.8.1 인수 금액 제안 — 적정 인수 금액 이상이면 바로 인수."""

    def __init__(self, cog: "Elite", user, club: dict):
        super().__init__(title=f"🤝 {club['name']} 인수 제안"[:45])
        self.cog, self.user, self.club = cog, user, club
        self.amount = discord.ui.TextInput(label="제안 금액 (원)", max_length=15,
                                           placeholder=f"적정 인수 금액 {club['fair']:,}원 이상이면 바로 인수")
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.amount.value).replace(",", "").replace("원", "").strip()
        if not raw.isdigit():
            return await interaction.response.send_message("제안 금액은 숫자로 적어 주세요.", ephemeral=True)
        await self.cog.make_offer(interaction, self.user, self.club, int(raw))


class AskModal(discord.ui.Modal):
    """3.8.1 구단주가 적정 인수 금액을 정한다 — 이 금액 이상 제안이 오면 바로 인수된다."""

    def __init__(self, cog: "Elite", user, club: dict):
        super().__init__(title="🏷️ 적정 인수 금액 정하기")
        self.cog, self.user, self.club = cog, user, club
        lo, hi = elite_ask_range(club["price"])
        self.amount = discord.ui.TextInput(label=f"{_eok(lo)} ~ {_eok(hi)}원 (이 이상 제안이 오면 바로 인수)"[:45],
                                           default=str(club["fair"]), max_length=15)
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.amount.value).replace(",", "").replace("원", "").strip()
        if not raw.isdigit():
            return await interaction.response.send_message("금액은 숫자로 적어 주세요.", ephemeral=True)
        r = await self.cog.clubs.set_elite_ask(self.user.id, int(raw))
        c = self.club
        if r["ok"]:
            e = ui.card(f"🏷️ {c['emblem']} {c['name']} 적정 인수 금액", f"**{r['ask']:,}원**으로 정했어요.\n"
                        "이 금액 이상을 제안하면 바로 인수되고, 그보다 적은 제안은 수락 · 거절할 수 있어요.",
                        ui.WIN, self.user, SECTION)
        else:
            e = ui.card("🙅 적정 인수 금액", "가진 명문 구단이 없어요." if r["reason"] == "none" else
                        f"**{r['lo']:,}원 ~ {r['hi']:,}원** 사이로 정해 주세요.", ui.LOSE, self.user, SECTION)
        await interaction.response.send_message(embed=e, ephemeral=not r["ok"])


class OfferStart(discord.ui.View):
    """3.8.1 주인 있는 명문 구단: 적정 인수 금액으로 바로 인수하거나, 금액을 제안한다."""

    def __init__(self, cog: "Elite", user, club: dict):
        super().__init__(timeout=180)
        self.cog, self.user, self.club = cog, user, club
        self.now.label = f"{_eok(club['fair'])}원에 바로 인수"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("🙅 명령어를 쓴 사람만 누를 수 있어요.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="바로 인수", emoji="🤝", style=discord.ButtonStyle.success)
    async def now(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await self.cog.make_offer(interaction, self.user, self.club, self.club["fair"], edit=True)

    @discord.ui.button(label="금액 제안", emoji="✍️", style=discord.ButtonStyle.primary)
    async def propose(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(OfferModal(self.cog, self.user, self.club))


class OwnerOffers(discord.ui.View):
    """3.8.1 내 명문 구단에 들어온 인수 제안 — 골라서 [수락] · [거절] (+ 2.10.1 매각)."""

    def __init__(self, cog: "Elite", user, club: dict, offers: list[dict], sell: bool):
        super().__init__(timeout=300)
        self.cog, self.user, self.club, self.offers = cog, user, club, offers
        self.pick: Optional[int] = offers[0]["id"] if offers else None
        if offers:
            sel = discord.ui.Select(placeholder="📨 받은 인수 제안 고르기", row=0, options=[
                discord.SelectOption(label=f"{o['amount']:,}원 — {o['name']}"[:100], value=str(o["id"]),
                                     default=o["id"] == self.pick) for o in offers[:25]])
            sel.callback = self._picked
            self.add_item(sel)
            for label, emoji, style, ok in (("수락", "✅", discord.ButtonStyle.success, True),
                                            ("거절", "❌", discord.ButtonStyle.danger, False)):
                b = discord.ui.Button(label=label, emoji=emoji, style=style, row=1)
                b.callback = self._answer(ok)
                self.add_item(b)
        ask = discord.ui.Button(label="적정 인수 금액 정하기", emoji="🏷️", style=discord.ButtonStyle.primary, row=2)
        ask.callback = self._ask
        self.add_item(ask)
        if sell:
            b = discord.ui.Button(label=f"{_eok(int(club['price'] * ELITE_SELL_RATE))}원에 매각", emoji="💸",
                                  style=discord.ButtonStyle.secondary, row=2)
            b.callback = self._sell
            self.add_item(b)

    async def _ask(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AskModal(self.cog, self.user, self.club))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("🙅 구단주만 누를 수 있어요.", ephemeral=True)
            return False
        return True

    async def _picked(self, interaction: discord.Interaction):
        self.pick = int(interaction.data["values"][0])
        await interaction.response.defer()

    def _answer(self, accept: bool):
        async def cb(interaction: discord.Interaction):
            self.stop()
            await self.cog.answer_offer(interaction, self.user, self.club, self.pick, accept)
        return cb

    async def _sell(self, interaction: discord.Interaction):
        self.stop()
        c = self.club
        e = ui.card(f"💸 {c['emblem']} {c['name']} 매각",
                    f"정말 매각할까요? 낸 인수 금액의 **{ELITE_SELL_RATE:.0%}**(**{int(c['price'] * ELITE_SELL_RATE):,}원**)을 받아요.",
                    ui.INFO, self.user, SECTION)
        await interaction.response.edit_message(embed=e, view=SellConfirm(self.cog, self.user, c))


class ClaimView(discord.ui.View):
    """3.7.0 명문 구단 하루 수입 직접 수령 (자동 지급 대신)."""

    def __init__(self, cog: "Elite", user, amount: int):
        super().__init__(timeout=300)
        self.cog, self.user = cog, user
        self.claim.disabled = amount <= 0

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("🙅 구단주만 누를 수 있어요.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="수입 수령", emoji="💰", style=discord.ButtonStyle.success)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        r = await self.cog.clubs.claim_elite(self.user.id, int(time.time()))
        if not r["ok"]:
            msg = "가진 명문 구단이 없어요." if r["reason"] == "none" else "지금은 받을 수입이 없어요. 내일 00시에 다시 쌓여요."
            return await interaction.response.send_message(
                embed=ui.card("🙅 수령할 수 없어요", msg, ui.LOSE, self.user, SECTION), ephemeral=True)
        fan = await self.cog.mgmt.elite_bonus(self.user.id, r["amount"])   # 팬 만족도 보너스
        emblem, name, _ = ELITE_CLUBS[r["key"]]
        days = f" ({r['days']}일치)" if r["days"] > 1 else ""
        e = ui.card(f"{emblem} {name} 수입 수령!",
                    f"`수입` **+{r['amount']:,}원**{days}" + (f"\n`팬 만족도 보너스` **+{fan:,}원**" if fan else "")
                    + f"\n`잔액` **{r['balance'] + fan:,}원**", ui.WIN, self.user, "🏰 명문 구단")
        button.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(embed=e)


class SellConfirm(discord.ui.View):
    """2.10.1 내 명문 구단 혼자 매각 — 낸 인수 금액의 70%."""

    def __init__(self, cog: "Elite", user, club: dict):
        super().__init__(timeout=120)
        self.cog, self.user, self.club = cog, user, club
        self.sell.label = f"{_eok(int(club['price'] * ELITE_SELL_RATE))}원에 매각"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("🙅 구단주만 누를 수 있어요.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="매각", emoji="💸", style=discord.ButtonStyle.danger)
    async def sell(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        c = self.club
        r = await self.cog.clubs.sell_elite(self.user.id)
        if not r["ok"]:
            return await interaction.response.edit_message(
                embed=ui.card("❌ 매각 실패", "가진 명문 구단이 없어요.", ui.LOSE, self.user, SECTION), view=None)
        e = ui.card(f"{c['emblem']} {c['name']} 매각 완료",
                    f"`받은 돈` **+{r['pay']:,}원** (인수 금액 {r['paid']:,}원의 {ELITE_SELL_RATE:.0%}) · `잔액` **{r['balance']:,}원**\n"
                    + (f"`밀린 수입` **+{r['income']:,}원**도 함께 받았어요.\n" if r.get("income") else "") +
                    "스쿼드는 **A**(내 구단)로 돌아갔어요. 이제 다른 명문 구단을 인수할 수 있어요.",
                    ui.WIN, self.user, SECTION)
        await interaction.response.edit_message(embed=e, view=None)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            embed=ui.card("매각 취소", "아무것도 바뀌지 않았어요.", ui.EVEN, self.user, SECTION), view=None)


class Elite(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.clubs = ClubDB()
        self.mgmt = MgmtDB()
        self.income_loop.start()

    def cog_unload(self):
        self.income_loop.cancel()

    @tasks.loop(minutes=10)
    async def income_loop(self):
        if release.released_370():   # 3.7.0: 자동 지급 대신 /명문구단 [수령] 으로 직접 받는다
            return
        for x in await self.clubs.settle_elite_income(int(time.time())):
            x["fan"] = await self.mgmt.elite_bonus(x["owner_id"], x["amount"])   # 2.8 팬 만족도 70↑ +5% · 85↑ +10%
            print(f"[명문] {x['key']} → {x['owner_id']} +{x['amount']:,} ({x['days']}일)")
            await self._income_dm(x)

    async def _income_dm(self, x: dict):
        """하루 수입 DM — 알림 설정과 상관없이 꼭 보낸다 (2.6)."""
        emblem, name, size = ELITE_CLUBS[x["key"]]
        days = f" ({x['days']}일치)" if x["days"] > 1 else ""
        fan = f"\n`팬 만족도 보너스` **+{x['fan']:,}원**" if x.get("fan") else ""
        e = ui.card(f"{emblem} {name} 하루 수입", f"`수입` **+{x['amount']:,}원**{days}{fan}\n`규모` {size}",
                    ui.WIN, None, "🏰 명문 구단")
        try:
            user = self.bot.get_user(x["owner_id"]) or await self.bot.fetch_user(x["owner_id"])
            await user.send(embed=e)
        except discord.HTTPException:   # DM 막힘 · 없는 유저 — 돈은 이미 들어갔다
            pass

    @income_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ───────────── 3.8.1 인수 제안 ─────────────
    async def _dm(self, uid: int, e: discord.Embed):
        try:
            user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
            await user.send(embed=e)
        except discord.HTTPException:
            pass

    async def make_offer(self, interaction: discord.Interaction, user, c: dict, amount: int, edit: bool = False):
        r = await self.clubs.offer_elite(user.id, c["key"], amount, int(time.time()))
        if not r["ok"]:
            e = ui.card("❌ 인수 제안 실패", _offer_fail(r), ui.LOSE, user, SECTION)
        elif r["auto"]:
            e = ui.card(f"{c['emblem']} {c['name']} 인수 완료!",
                        f"`인수 금액` **{r['cost']:,}원** (적정 인수 금액 {r['fair']:,}원 이상이라 바로 인수) · `잔액` **{r['balance']:,}원**\n"
                        f"`전 구단주` <@{r['prev_owner']}> — 인수 금액을 받았어요\n\n"
                        "`/스쿼드 선택:B`로 바꾸면 이 구단으로 경기해요.", ui.GOLD, user, SECTION)
            await self._dm(r["prev_owner"], ui.card(f"{c['emblem']} {c['name']}이(가) 인수됐어요",
                                                    f"{user.mention}님이 적정 인수 금액 이상(**{r['cost']:,}원**)을 제안해 바로 인수했어요.\n"
                                                    f"인수 금액 **+{r['cost']:,}원**을 받았어요.", ui.INFO, None, "🏰 명문 구단"))
        else:
            e = ui.card("📨 인수 제안을 보냈어요",
                        f"{c['emblem']} **{c['name']}** · `제안 금액` **{r['amount']:,}원** (적정 인수 금액 {r['fair']:,}원)\n"
                        f"구단주 <@{r['owner_id']}>님이 수락하면 인수돼요. 제안은 <t:{r['expires']}:R> 만료돼요.\n"
                        "-# 돈은 수락될 때 빠져나가요 · 그때 잔액이 모자라면 인수되지 않아요", ui.INFO, user, SECTION)
            await self._dm(r["owner_id"], ui.card(f"📨 {c['emblem']} {c['name']} 인수 제안",
                                                  f"{user.mention}님이 **{r['amount']:,}원**에 인수하고 싶어 해요.\n"
                                                  f"`/명문구단 구단:{c['name']}`에서 수락하거나 거절할 수 있어요 (<t:{r['expires']}:R> 만료).",
                                                  ui.INFO, None, "🏰 명문 구단"))
        if edit:
            await interaction.response.edit_message(embed=e, view=None)
        else:
            await interaction.response.send_message(embed=e)

    async def answer_offer(self, interaction: discord.Interaction, user, c: dict, offer_id: Optional[int], accept: bool):
        r = await self.clubs.answer_elite_offer(user.id, offer_id or 0, accept, int(time.time()))
        if r["ok"] and r["accepted"]:
            e = ui.card(f"✅ {c['emblem']} {c['name']} 매각 완료",
                        f"<@{r['buyer_id']}>님의 제안을 수락했어요. `받은 돈` **+{r['amount']:,}원** · `잔액` **{r['balance']:,}원**\n"
                        "스쿼드는 **A**(내 구단)로 경기해요.", ui.WIN, user, SECTION)
            await self._dm(r["buyer_id"], ui.card(f"{c['emblem']} {c['name']} 인수 완료!",
                                                  f"구단주가 **{r['amount']:,}원** 제안을 수락했어요.\n"
                                                  "`/스쿼드 선택:B`로 바꾸면 이 구단으로 경기해요.", ui.GOLD, None, "🏰 명문 구단"))
        elif r["ok"]:
            e = ui.card("❌ 인수 제안 거절", f"<@{r['buyer_id']}>님의 **{r['amount']:,}원** 제안을 거절했어요.", ui.EVEN, user, SECTION)
            await self._dm(r["buyer_id"], ui.card(f"{c['emblem']} {c['name']} 인수 제안 거절",
                                                  f"구단주가 **{r['amount']:,}원** 제안을 거절했어요.", ui.EVEN, None, "🏰 명문 구단"))
        else:
            msg = _OFFER_FAIL["gone"] if r["reason"] == "gone" else "제안한 사람이 지금 인수할 수 없어서 제안이 닫혔어요. (" + _offer_fail(r) + ")"
            e = ui.card("🙅 처리할 수 없어요", msg, ui.LOSE, user, SECTION)
        await interaction.response.edit_message(embed=e, view=None)

    # ───────────── 명문 구단 ─────────────
    @app_commands.command(name="명문구단", description="명문 구단 10개 — 인수하면 스쿼드 B로 경기 · 매일 수입 (비우면 목록)")
    @app_commands.describe(구단="자세히 보거나 인수할 구단")
    @app_commands.choices(구단=[app_commands.Choice(name=f"{e} {n} ({s})", value=k) for k, (e, n, s) in ELITE_CLUBS.items()])
    async def elite(self, interaction: discord.Interaction, 구단: Optional[str] = None):
        user = interaction.user
        clubs = await self.clubs.elite_list()
        v381 = release.preview_381(interaction.guild_id)
        if not 구단:
            lines = []
            for c in clubs:
                owner = f"<@{c['owner_id']}>" if c["owner_id"] else "주인 없음"
                lines.append(f"{c['emblem']} **{c['name']}** · {c['size']} · 전력 **{c['rating']}**\n"
                             f"　{owner} · 인수 **{_eok(c['fair'] if v381 else c['cost'])}원** · 하루 +{_eok(c['income'])}원")
            e = ui.card("🏰 명문 구단", "\n".join(lines), ui.GOLD, user, SECTION)
            e.set_footer(text=("주인 있는 구단은 인수 제안 — 적정 인수 금액(구단주가 정함) 이상이면 바로 인수, "
                               "아니면 구단주가 수락 · 거절" if v381 else
                               f"주인 없음은 표시 가격 · 주인 있으면 지금 가격 ×{ELITE_TAKEOVER} (전 주인에게)")
                         + " · 한 사람 한 구단 · /명문구단 구단:<이름> · 내 구단은 거기서 매각(낸 돈의 70%)")
            if v381:
                offers = await self.clubs.elite_offers(user.id, int(time.time()))
                if offers:
                    e.description += f"\n\n📨 내 명문 구단에 인수 제안 **{len(offers)}건** — `/명문구단 구단:`에서 내 구단을 골라 확인하세요."

            view = None
            if release.preview_370(interaction.guild_id):   # 3.7.0 수입 직접 수령
                p = await self.clubs.elite_pending(user.id, int(time.time()))
                if p:
                    emblem, name, _ = ELITE_CLUBS[p["key"]]
                    e.description += (f"\n\n💰 **{emblem} {name}** 받을 수입 **{p['amount']:,}원** ({p['days']}일치)" if p["amount"]
                                      else f"\n\n💰 **{emblem} {name}** 오늘 수입은 받았어요. 내일 00시에 다시 쌓여요.")
                    view = ClaimView(self, user, p["amount"])
            return await interaction.response.send_message(embed=e, view=view) if view else \
                await interaction.response.send_message(embed=e)

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
                    + (f"`적정 인수 금액` **{c['fair']:,}원**" + ("" if c["ask"] else " (구단주가 아직 안 정해서 기본값)")
                       if c["owner_id"] and v381 else f"`인수 금액` **{c['cost']:,}원**"
                       + (f" (지금 가격 {c['price']:,}원 ×{ELITE_TAKEOVER})" if c["owner_id"] else ""))
                    + "\n\n" + "\n".join(rows) + "\n\n-# 선수 능력치는 바뀌지 않아요 (성장 · 노화 · 부상 없음)",
                    ui.INFO, user, SECTION)
        mine = c["owner_id"] == user.id
        sell = mine and release.preview_2101(interaction.guild_id)   # 2.10.1 혼자 매각 (테스트 서버 먼저)
        if sell:   # 2.10.1: 남이 인수하지 않아도 혼자 매각
            e.description += f"\n\n💸 매각하면 낸 인수 금액의 **{ELITE_SELL_RATE:.0%}**(**{int(c['price'] * ELITE_SELL_RATE):,}원**)을 받아요."
        if release.preview_381(interaction.guild_id) and c["owner_id"]:   # 3.8.1 인수 제안
            if mine:
                offers = await self.clubs.elite_offers(user.id, int(time.time()))
                for o in offers:
                    m = interaction.guild.get_member(o["buyer_id"]) if interaction.guild else None
                    o["name"] = m.display_name if m else str(o["buyer_id"])
                e.description += ("\n\n📨 **받은 인수 제안**\n" + "\n".join(
                    f"<@{o['buyer_id']}> **{o['amount']:,}원** · <t:{o['ts'] + ELITE_OFFER_HOURS * 3600}:R> 만료" for o in offers[:10])
                                  if offers else "\n\n📨 받은 인수 제안이 없어요.")
                lo, hi = elite_ask_range(c["price"])
                e.description += (f"\n🏷️ 적정 인수 금액 **{c['fair']:,}원** — [적정 인수 금액 정하기]로 "
                                  f"{_eok(lo)} ~ {_eok(hi)}원 사이에서 바꿀 수 있어요. 이 금액 이상 제안은 바로 인수돼요.")
                view = OwnerOffers(self, user, c, offers, sell)
            else:
                e.description += ("\n\n🤝 **적정 인수 금액** 이상을 내면 바로 인수돼요. 그보다 적게 제안하면 구단주가 수락할 때 인수돼요.")
                view = OfferStart(self, user, c)
        else:
            view = SellConfirm(self, user, c) if sell else (None if mine else BuyConfirm(self, user, c))
        await interaction.response.send_message(embed=e, view=view) if view else await interaction.response.send_message(embed=e)

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

    # ───────────── 꾸미기 (2.10.1: /구단 화면으로 옮김 — 공개 전까지 다른 서버에만, release.LEGACY_2101) ─────────────
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
