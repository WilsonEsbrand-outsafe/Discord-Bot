# cogs/sponsor.py — /스폰서 · /스폰서계약 · /스폰서해지
import time

import discord
from discord import app_commands
from discord.ext import commands

from services import ui
from services.sponsor_db import (
    CANCEL_FEE, MAX_ACTIVE, MAX_AMOUNT, MIN_AMOUNT, SPONSORS, TERMS, SponsorDB, cancel_refund,
)

SECTION = "🤝 스폰서"


def _name(key: str) -> str:
    emoji, name, *_ = SPONSORS[key]
    return f"{emoji} {name}"


def _pct(x: float) -> str:
    return f"{x * 100:g}%"


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


def _range_text(key: str) -> str:
    lo, hi = SPONSORS[key][3:5]
    if lo == hi:
        return "기본 수익 그대로"
    return f"기본 수익의 {lo:g}~{hi:g}배" + (" · ⚠️ 원금 손실 가능" if lo < 0 else "")


def _catalog() -> str:
    return "\n".join(f"{e} **{n}** · {kind} · {_range_text(k)}\n　 *{blurb}*"
                     for k, (e, n, kind, _, _, blurb) in SPONSORS.items())


def _terms() -> str:
    return " · ".join(f"`{d}일` **+{_pct(r)}**" for d, r in TERMS.items())


class CancelConfirm(discord.ui.View):
    def __init__(self, cog: "Sponsor", user, cid: int):
        super().__init__(timeout=30)
        self.cog, self.user, self.cid = cog, user, cid

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("🙅 계약한 본인만 누를 수 있어요.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="해지하기", style=discord.ButtonStyle.danger)
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


class Sponsor(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = SponsorDB()

    async def _settle_embed(self, user) -> discord.Embed | None:
        """만기 지난 계약을 정산하고 결과 카드를 만든다 (없으면 None)."""
        done, bal = await self.db.settle(user.id, int(time.time()))
        if not done:
            return None
        lines, total = [], 0
        for c in done:
            gain = c["payout"] - c["amount"]
            total += gain
            lines.append(f"{_name(c['sponsor'])} `{c['days']}일` · {_perf_label(c['sponsor'], c['perf'])}\n"
                         f"　 원금 {c['amount']:,}원 → **{c['payout']:,}원** ({ui.won(gain)})")
        best = max(done, key=lambda c: c["perf"])
        color = ui.GOLD if best["perf"] >= 2 and total > 0 else ui.tone(total)
        e = ui.card("💰 만기 정산", "\n".join(lines) + f"\n\n`합계` **{ui.won(total)}**\n`잔액` **{bal:,}원**",
                    color, user, SECTION)
        e.set_thumbnail(url=ui.emoji_url("💰"))
        return e

    @app_commands.command(name="스폰서", description="스폰서 목록과 내 계약 현황 (만기된 계약은 자동 정산)")
    async def overview(self, interaction: discord.Interaction):
        await interaction.response.defer()
        user, now = interaction.user, int(time.time())
        settled = await self._settle_embed(user)
        contracts = await self.db.active(user.id)
        if contracts:
            mine = "\n".join(
                f"`#{c['id']}` {_name(c['sponsor'])} · `{c['days']}일` · **{c['amount']:,}원**\n"
                f"　 {ui.bar(now - c['start_ts'], c['end_ts'] - c['start_ts'])} 만기 <t:{c['end_ts']}:R>"
                for c in contracts)
        else:
            mine = "아직 계약이 없어요. `/스폰서계약`으로 시작해 보세요!"
        e = ui.card("🤝 스폰서 계약 현황",
                    f"**📋 진행 중인 계약** ({len(contracts)}/{MAX_ACTIVE})\n{mine}\n\n"
                    f"**🏢 스폰서**\n{_catalog()}\n\n**📅 기간별 기본 수익**\n{_terms()}",
                    ui.INFO, user, SECTION)
        e.set_footer(text=f"계약당 {MIN_AMOUNT:,}~{MAX_AMOUNT:,}원 · 중도 해지 시 이자 없이 원금의 {_pct(CANCEL_FEE)} 수수료")
        await interaction.followup.send(embeds=[x for x in (settled, e) if x])

    @app_commands.command(name="스폰서계약", description="스폰서에 돈을 맡기고 기간이 끝나면 수익과 함께 돌려받습니다")
    @app_commands.describe(스폰서="계약할 스폰서", 기간="계약 기간", 금액=f"맡길 금액 ({MIN_AMOUNT:,}~{MAX_AMOUNT:,}원)")
    @app_commands.choices(
        스폰서=[app_commands.Choice(name=f"{e} {n} ({kind})", value=k) for k, (e, n, kind, *_) in SPONSORS.items()],
        기간=[app_commands.Choice(name=f"{d}일 (기본 수익 +{_pct(r)})", value=d) for d, r in TERMS.items()],
    )
    async def open_contract(self, interaction: discord.Interaction, 스폰서: str, 기간: int,
                            금액: app_commands.Range[int, MIN_AMOUNT, MAX_AMOUNT]):
        await interaction.response.defer()
        user = interaction.user
        r = await self.db.open(user.id, 스폰서, 기간, int(금액), int(time.time()))
        if not r["ok"]:
            msg = (f"진행 중인 계약이 이미 {MAX_ACTIVE}건이에요. 만기나 해지 후 다시 계약해 주세요."
                   if r["reason"] == "full" else f"`금액` **{금액:,}원**\n`잔액` **{r['balance']:,}원**")
            title = "📋 계약 슬롯이 가득 찼어요" if r["reason"] == "full" else "🙅 잔액이 부족해요"
            return await interaction.followup.send(embed=ui.card(title, msg, ui.LOSE, user, SECTION))

        emoji, name, kind, lo, hi, _ = SPONSORS[스폰서]
        base = TERMS[기간]
        worst, best = max(-금액, round(금액 * base * lo)), round(금액 * base * hi)
        expect = f"**{ui.won(worst)}**" if lo == hi else f"**{ui.won(worst)}** ~ **{ui.won(best)}**"
        e = ui.card(f"✍️ {emoji} {name} 계약 체결!",
                    f"> 💬 *\"{name}입니다. 함께하게 되어 영광이에요!\"*\n\n"
                    f"`계약` `#{r['id']}` · {kind} · `{기간}일`\n"
                    f"`원금` **{금액:,}원**\n"
                    f"`예상 수익` {expect}\n"
                    f"`만기` <t:{r['end_ts']}:f> (<t:{r['end_ts']}:R>)\n"
                    f"`잔액` **{r['balance']:,}원**",
                    ui.WIN, user, SECTION)
        e.set_thumbnail(url=ui.emoji_url(emoji))
        e.set_footer(text="만기가 지나면 /스폰서 를 열 때 자동으로 정산됩니다")
        await interaction.followup.send(embed=e)

    async def contract_autocomplete(self, interaction: discord.Interaction, current: str):
        out = []
        for c in await self.db.active(interaction.user.id):
            label = f"#{c['id']} {SPONSORS[c['sponsor']][1]} {c['days']}일 · {c['amount']:,}원"
            if current in label:
                out.append(app_commands.Choice(name=label, value=str(c["id"])))
        return out[:25]

    @app_commands.command(name="스폰서해지", description=f"진행 중인 계약을 중도 해지합니다 (이자 없음 · 원금의 {_pct(CANCEL_FEE)} 수수료)")
    @app_commands.describe(계약="해지할 계약")
    @app_commands.autocomplete(계약=contract_autocomplete)
    async def cancel_contract(self, interaction: discord.Interaction, 계약: str):
        user = interaction.user
        c = next((c for c in await self.db.active(user.id) if str(c["id"]) == 계약.lstrip("#")), None)
        if c is None:
            return await interaction.response.send_message(
                embed=ui.card("❌ 계약을 찾을 수 없어요", "`/스폰서`에서 진행 중인 계약 번호를 확인해 주세요.", ui.LOSE, user, SECTION))
        refund = cancel_refund(c["amount"])
        e = ui.card(f"⚠️ {_name(c['sponsor'])} 계약을 해지할까요?",
                    f"`계약` `#{c['id']}` · `{c['days']}일` · 만기 <t:{c['end_ts']}:R>\n"
                    f"`원금` **{c['amount']:,}원**\n"
                    f"`수수료` **{ui.won(refund - c['amount'])}** ({_pct(CANCEL_FEE)}) · 이자 없음\n"
                    f"`돌려받음` **{refund:,}원**",
                    ui.LOSE, user, SECTION)
        await interaction.response.send_message(embed=e, view=CancelConfirm(self, user, c["id"]))


async def setup(bot: commands.Bot):
    await bot.add_cog(Sponsor(bot))
