# cogs/sponsor.py — /스폰서(현황 · 계약 관리 메뉴) · /스폰서계약 + 만기 자동 정산(DM 알림)
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from services import ui
from services.club_db import ClubDB
from services.notifier import send_notify
from services.sponsor_db import (
    CANCEL_FEE, CLUB_BONUS, GRADES, MAX_ACTIVE, MAX_AMOUNT, MIN_AMOUNT, REP_MIN_AMOUNT, SPONSORS, TERMS,
    SponsorDB, cancel_refund, club_bonus, grade_of,
)

SECTION = "🤝 스폰서"


def _name(key: str) -> str:
    emoji, name, *_ = SPONSORS[key]
    return f"{emoji} {name}"


def _pct(x: float) -> str:
    return f"{x * 100:g}%"


def _man(n: int) -> str:
    return f"{n // 10_000:,}만원"


def _perf_label(key: str, perf: float) -> str:
    lo, hi = SPONSORS[key][3:5]
    if lo == hi:
        return "✅ 약속 이행"
    if perf >= 2:
        return "🎉 대박 실적"
    if perf >= 1:
        return "📈 호실적"
    if perf >= 0:
        return "📉 부진"
    return "💥 스캔들"


def _terms() -> str:
    return " · ".join(f"`{d}일` **+{_pct(r)}**" for d, r in TERMS.items())


def settle_embed(result: dict, user=None) -> discord.Embed:
    """정산 결과 카드 — /스폰서 화면과 만기 DM 에서 같이 쓴다."""
    lines, total = [], 0
    for c in result["done"]:
        gain = c["payout"] - c["amount"]
        total += gain
        line = (f"{_name(c['sponsor'])} `{c['days']}일` · {_perf_label(c['sponsor'], c['perf'])}\n"
                f"　 원금 {c['amount']:,}원 → **{c['payout']:,}원** ({ui.won(gain)})")
        if c["grade_up"]:
            line += f"\n　 🆙 등급 상승 → **{c['grade_up']}**"
        if c["renewed"]:
            line += f"\n　 🔁 {c['renew_amount']:,}원으로 재계약 (`#{c['renewed']}`)"
        lines.append(line)
    best = max(c["perf"] for c in result["done"])
    color = ui.GOLD if best >= 2 and total > 0 else ui.tone(total)
    e = ui.card("💰 스폰서 만기 정산", "\n".join(lines) + f"\n\n`합계` **{ui.won(total)}**\n`잔액` **{result['balance']:,}원**",
                color, user, SECTION)
    e.set_thumbnail(url=ui.emoji_url("💰"))
    return e


def _range_short(key: str) -> str:
    lo, hi = SPONSORS[key][3:5]
    if lo == hi:
        return "수익 ×1 · 원금 보장"
    return f"수익 ×{lo:g}~{hi:g}" + (" · ⚠️ 원금 손실 가능" if lo < 0 else " · 원금 보장")


def _contract_embed(c: dict, user, now: int) -> discord.Embed:
    refund = cancel_refund(c["amount"])
    return ui.card(f"{_name(c['sponsor'])} · `{c['days']}일` 계약 `#{c['id']}`",
                   f"`원금` **{c['amount']:,}원**\n"
                   f"`진행` {ui.bar(now - c['start_ts'], c['end_ts'] - c['start_ts'])} 만기 <t:{c['end_ts']}:R>\n"
                   f"`자동 재계약` **{'ON 🔁' if c['auto'] else 'OFF'}**\n"
                   f"`중도 해지 시` **{refund:,}원** 돌려받음 (수수료 {_pct(CANCEL_FEE)} · 이자 없음)",
                   ui.INFO, user, SECTION)


class CancelConfirm(discord.ui.View):
    def __init__(self, cog: "Sponsor", user, cid: int):
        super().__init__(timeout=60)
        self.cog, self.user, self.cid = cog, user, cid

    @discord.ui.button(label="해지 확정", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        r = await self.cog.db.cancel(self.user.id, self.cid, int(time.time()))
        if r["ok"]:
            e = ui.card(f"📝 {_name(r['sponsor'])} 계약 해지",
                        f"`원금` **{r['amount']:,}원** · `{r['days']}일` 계약\n"
                        f"`수수료` **{ui.won(r['refund'] - r['amount'])}** ({_pct(CANCEL_FEE)})\n"
                        f"`돌려받음` **{r['refund']:,}원**\n`잔액` **{r['balance']:,}원**",
                        ui.EVEN, self.user, SECTION)
        elif r["reason"] == "matured":
            e = ui.card("⏰ 이미 만기된 계약이에요", "`/스폰서`를 열면 수수료 없이 정산됩니다.", ui.INFO, self.user, SECTION)
        else:
            e = ui.card("❌ 해지할 수 없어요", "이미 정산됐거나 해지된 계약입니다.", ui.LOSE, self.user, SECTION)
        await interaction.response.edit_message(embed=e, view=None)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            embed=ui.card("계약 유지", "아무것도 바뀌지 않았습니다.", ui.EVEN, self.user, SECTION), view=None)


class ContractActions(discord.ui.View):
    """계약 하나를 고른 뒤(본인에게만 보임): 자동 재계약 켜기/끄기 · 중도 해지."""

    def __init__(self, cog: "Sponsor", user, c: dict):
        super().__init__(timeout=180)
        self.cog, self.user, self.c = cog, user, c
        auto = discord.ui.Button(label="🔁 자동 재계약 끄기" if c["auto"] else "🔁 자동 재계약 켜기",
                                 style=discord.ButtonStyle.primary)
        auto.callback = self._toggle
        stop = discord.ui.Button(label="📝 중도 해지", style=discord.ButtonStyle.danger)
        stop.callback = self._cancel
        self.add_item(auto)
        self.add_item(stop)

    async def _toggle(self, interaction: discord.Interaction):
        on = not self.c["auto"]
        if not await self.cog.db.set_auto(self.user.id, self.c["id"], on):
            return await interaction.response.edit_message(
                embed=ui.card("❌ 계약을 찾을 수 없어요", "이미 정산됐거나 해지된 계약입니다.", ui.LOSE, self.user, SECTION),
                view=None)
        self.c["auto"] = int(on)
        e = _contract_embed(self.c, self.user, int(time.time()))
        e.description += ("\n\n🔁 만기 때 지급액에서 같은 원금(최대 이전 원금 · 등급 한도)을 떼어 **같은 조건으로 다시 계약**해요."
                          if on else "\n\n만기 때 정산만 하고 계약을 끝내요.")
        await interaction.response.edit_message(embed=e, view=ContractActions(self.cog, self.user, self.c))

    async def _cancel(self, interaction: discord.Interaction):
        refund = cancel_refund(self.c["amount"])
        e = ui.card(f"⚠️ {_name(self.c['sponsor'])} 계약을 해지할까요?",
                    f"`원금` **{self.c['amount']:,}원** → `돌려받음` **{refund:,}원**\n"
                    f"(수수료 {_pct(CANCEL_FEE)} · 이자 없음)", ui.LOSE, self.user, SECTION)
        await interaction.response.edit_message(embed=e, view=CancelConfirm(self.cog, self.user, self.c["id"]))


class ManageMenu(discord.ui.View):
    """/스폰서 아래 '계약 관리' 메뉴 — 계약한 본인만."""

    def __init__(self, cog: "Sponsor", user, contracts: list[dict]):
        super().__init__(timeout=600)
        self.cog, self.user = cog, user
        self.select = discord.ui.Select(placeholder="⚙️ 계약 관리 (자동 재계약 · 중도 해지)", options=[
            discord.SelectOption(label=f"#{c['id']} {SPONSORS[c['sponsor']][1]} · {c['days']}일 · {c['amount']:,}원"[:100],
                                 value=str(c["id"]), emoji=SPONSORS[c["sponsor"]][0],
                                 description="🔁 자동 재계약 ON" if c["auto"] else "자동 재계약 OFF")
            for c in contracts[:25]])
        self.select.callback = self._chosen
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("🙅 계약한 본인만 관리할 수 있어요.", ephemeral=True)
            return False
        return True

    async def _chosen(self, interaction: discord.Interaction):
        cid = int(self.select.values[0])
        c = next((c for c in await self.cog.db.active(self.user.id) if c["id"] == cid), None)
        if c is None:
            return await interaction.response.send_message("❌ 이미 정산됐거나 해지된 계약이에요.", ephemeral=True)
        await interaction.response.send_message(embed=_contract_embed(c, self.user, int(time.time())),
                                                view=ContractActions(self.cog, self.user, c), ephemeral=True)


class Sponsor(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = SponsorDB()
        self.clubs = ClubDB()
        self.settle_task.start()

    def cog_unload(self):
        self.settle_task.cancel()

    # ───────────── 만기 자동 정산 + DM ─────────────
    @tasks.loop(minutes=5)
    async def settle_task(self):
        try:
            results = await self.db.settle(int(time.time()))
        except Exception as ex:
            print(f"[SPONSOR] 자동 정산 실패: {type(ex).__name__}: {ex}")
            return
        for uid, result in results.items():
            await send_notify(self.bot, self.db, uid, "스폰서_만기", settle_embed(result))

    @settle_task.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def _club_rating(self, user_id: int) -> Optional[float]:
        team = await self.clubs.get_team(user_id)
        return team["rating"] if team else None

    # ───────────── 명령어 ─────────────
    @app_commands.command(name="스폰서", description="내 계약 · 스폰서 등급 · 계약 관리 (만기된 계약은 자동 정산)")
    async def overview(self, interaction: discord.Interaction):
        await interaction.response.defer()
        user, now = interaction.user, int(time.time())
        settled = (await self.db.settle(now, user.id)).get(user.id)
        contracts = await self.db.active(user.id)
        rep = await self.db.reputation(user.id)
        rating = await self._club_rating(user.id)
        cb = club_bonus(rating)
        club = (f"+{_pct(cb)} (전력 {rating})" if cb else
                f"없음 (전력 {rating} · {CLUB_BONUS[-1][0]} 이상부터)" if rating is not None else
                f"없음 (구단 없음 · 전력 {CLUB_BONUS[-1][0]} 이상부터)")

        mine = ui.card("🤝 내 스폰서 계약",
                       f"`진행 중` **{len(contracts)}/{MAX_ACTIVE}건** · `맡긴 돈` **{sum(c['amount'] for c in contracts):,}원**\n"
                       f"`구단 보너스` {club}"
                       + ("" if contracts else "\n\n아직 계약이 없어요. `/스폰서계약`으로 시작해 보세요!"),
                       ui.INFO, user, SECTION)
        for c in contracts:
            mine.add_field(
                name=f"{_name(c['sponsor'])} · {c['days']}일{' 🔁' if c['auto'] else ''}",
                value=f"`#{c['id']}` **{c['amount']:,}원**\n{ui.bar(now - c['start_ts'], c['end_ts'] - c['start_ts'], 8)} "
                      f"<t:{c['end_ts']}:R>",
                inline=True)

        guide = discord.Embed(title="🏢 스폰서 · 내 등급", color=ui.DARK)
        for k, (e, n, kind, *_rest) in SPONSORS.items():
            r = rep.get(k, 0)
            idx, (_, gname, limit, gbonus) = grade_of(r)
            nxt = f"다음 등급까지 {GRADES[idx + 1][0] - r}일" if idx + 1 < len(GRADES) else "최고 등급"
            guide.add_field(name=f"{e} {n}",
                            value=f"{kind} · {_range_short(k)}\n{gname} · 한도 {_man(limit)}"
                                  + (f" · +{_pct(gbonus)}" if gbonus else "") + f"\n*{nxt}*",
                            inline=True)
        guide.add_field(name="📅 기간별 기본 수익", value=_terms(), inline=False)
        guide.set_footer(text=f"만기 수익 = 원금 × 기본 수익 × 스폰서 실적 · 등급은 {_man(REP_MIN_AMOUNT)} 이상 계약을 "
                              "만기까지 채운 일수로 올라요 · 🔁 = 자동 재계약")

        embeds = ([settle_embed(settled, user)] if settled else []) + [mine, guide]
        view = ManageMenu(self, user, contracts) if contracts else discord.utils.MISSING
        await interaction.followup.send(embeds=embeds, view=view)

    @app_commands.command(name="스폰서계약", description="스폰서에 돈을 맡기고 기간이 끝나면 수익과 함께 돌려받습니다")
    @app_commands.describe(스폰서="계약할 스폰서", 기간="계약 기간", 금액="맡길 금액 (한도는 스폰서 등급에 따라 다름)",
                           자동재계약="만기 때 같은 조건으로 자동 재계약")
    @app_commands.choices(
        스폰서=[app_commands.Choice(name=f"{e} {n} ({kind})", value=k) for k, (e, n, kind, *_) in SPONSORS.items()],
        기간=[app_commands.Choice(name=f"{d}일 (기본 수익 +{_pct(r)})", value=d) for d, r in TERMS.items()],
    )
    async def open_contract(self, interaction: discord.Interaction, 스폰서: str, 기간: int,
                            금액: app_commands.Range[int, MIN_AMOUNT, MAX_AMOUNT], 자동재계약: bool = False):
        await interaction.response.defer()
        user = interaction.user
        cb = club_bonus(await self._club_rating(user.id))
        r = await self.db.open(user.id, 스폰서, 기간, int(금액), int(time.time()), cb, 자동재계약)
        if not r["ok"]:
            title, msg = {
                "full": ("📋 계약 슬롯이 가득 찼어요", f"진행 중인 계약이 이미 {MAX_ACTIVE}건이에요. 만기나 해지 후 다시 계약해 주세요."),
                "limit": ("🔒 계약 한도 초과", f"{_name(스폰서)} 등급 **{r.get('grade')}** 의 한도는 **{r.get('limit', 0):,}원**이에요.\n"
                                            "만기까지 계약을 채우면 등급과 한도가 올라갑니다."),
                "balance": ("🙅 잔액이 부족해요", f"`금액` **{금액:,}원**\n`잔액` **{r.get('balance', 0):,}원**"),
            }[r["reason"]]
            return await interaction.followup.send(embed=ui.card(title, msg, ui.LOSE, user, SECTION))

        emoji, name, kind, lo, hi, _ = SPONSORS[스폰서]
        base, bonus = TERMS[기간], r["grade_bonus"] + cb
        worst = max(-금액, round(금액 * base * lo * (1 + bonus if lo > 0 else 1)))
        best = round(금액 * base * hi * (1 + bonus))
        expect = f"**{ui.won(best)}**" if lo == hi else f"**{ui.won(worst)}** ~ **{ui.won(best)}**"
        bonus_txt = " · ".join(t for t in (f"등급 +{_pct(r['grade_bonus'])}" if r["grade_bonus"] else "",
                                          f"구단 +{_pct(cb)}" if cb else "") if t) or "없음"
        e = ui.card(f"✍️ {emoji} {name} 계약 체결!",
                    f"> 💬 *\"{name}입니다. 함께하게 되어 영광이에요!\"*",
                    ui.WIN, user, SECTION)
        e.add_field(name="📄 계약", value=f"`#{r['id']}` · {kind} · **{기간}일**{chr(10) + '🔁 자동 재계약' if 자동재계약 else ''}", inline=True)
        e.add_field(name="💰 원금", value=f"**{금액:,}원**", inline=True)
        e.add_field(name="📈 예상 수익", value=expect, inline=True)
        e.add_field(name="🎖️ 등급 · 보너스", value=f"{r['grade']}\n{bonus_txt}", inline=True)
        e.add_field(name="⏰ 만기", value=f"<t:{r['end_ts']}:f>\n<t:{r['end_ts']}:R>", inline=True)
        e.add_field(name="👛 잔액", value=f"**{r['balance']:,}원**", inline=True)
        e.set_thumbnail(url=ui.emoji_url(emoji))
        e.set_footer(text="만기가 되면 자동 정산 · 관리(해지·재계약)는 /스폰서 메뉴 · /알림설정 에서 만기 DM")
        await interaction.followup.send(embed=e)


async def setup(bot: commands.Bot):
    await bot.add_cog(Sponsor(bot))
