# cogs/sponsor.py — /스폰서 · /스폰서계약 · /스폰서해지 · /스폰서재계약 + 만기 자동 정산(DM 알림)
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


def _range_text(key: str) -> str:
    lo, hi = SPONSORS[key][3:5]
    if lo == hi:
        return "기본 수익 그대로"
    return f"기본 수익의 {lo:g}~{hi:g}배" + (" · ⚠️ 원금 손실 가능" if lo < 0 else "")


def _grade_text(rep: int) -> str:
    idx, (_, gname, limit, gbonus) = grade_of(rep)
    nxt = f" · 다음 등급까지 {GRADES[idx + 1][0] - rep}일" if idx + 1 < len(GRADES) else ""
    bonus = f" · 수익 +{_pct(gbonus)}" if gbonus else ""
    return f"{gname} · 한도 {_man(limit)}{bonus}{nxt}"


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
    @app_commands.command(name="스폰서", description="스폰서 목록 · 내 등급 · 계약 현황 (만기된 계약은 자동 정산)")
    async def overview(self, interaction: discord.Interaction):
        await interaction.response.defer()
        user, now = interaction.user, int(time.time())
        settled = (await self.db.settle(now, user.id)).get(user.id)
        contracts = await self.db.active(user.id)
        rep = await self.db.reputation(user.id)
        rating = await self._club_rating(user.id)

        if contracts:
            mine = "\n".join(
                f"`#{c['id']}` {_name(c['sponsor'])} · `{c['days']}일` · **{c['amount']:,}원**{' 🔁' if c['auto'] else ''}\n"
                f"　 {ui.bar(now - c['start_ts'], c['end_ts'] - c['start_ts'])} 만기 <t:{c['end_ts']}:R>"
                for c in contracts)
        else:
            mine = "아직 계약이 없어요. `/스폰서계약`으로 시작해 보세요!"
        catalog = "\n".join(f"{e} **{n}** · {kind} · {_range_text(k)}\n　 {_grade_text(rep.get(k, 0))}"
                            for k, (e, n, kind, *_) in SPONSORS.items())
        cb = club_bonus(rating)
        club = (f"`전력` **{rating}** → 수익 **+{_pct(cb)}**" if cb else
                f"`전력` **{rating}** · {CLUB_BONUS[-1][0]} 이상이면 수익 보너스" if rating is not None else
                f"구단이 없어요 · 전력 {CLUB_BONUS[-1][0]} 이상이면 수익 보너스")
        e = ui.card("🤝 스폰서 계약 현황",
                    f"**📋 진행 중인 계약** ({len(contracts)}/{MAX_ACTIVE}) · 🔁 = 자동 재계약\n{mine}\n\n"
                    f"**🏢 스폰서 · 내 등급**\n{catalog}\n\n"
                    f"**🏟️ 구단 보너스**\n{club}\n\n**📅 기간별 기본 수익**\n{_terms()}",
                    ui.INFO, user, SECTION)
        e.set_footer(text=f"등급은 {_man(REP_MIN_AMOUNT)} 이상 계약을 만기까지 채운 일수로 올라요 · "
                          f"중도 해지 시 이자 없이 원금의 {_pct(CANCEL_FEE)} 수수료")
        embeds = ([settle_embed(settled, user)] if settled else []) + [e]
        await interaction.followup.send(embeds=embeds)

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
                    f"> 💬 *\"{name}입니다. 함께하게 되어 영광이에요!\"*\n\n"
                    f"`계약` `#{r['id']}` · {kind} · `{기간}일`{' · 🔁 자동 재계약' if 자동재계약 else ''}\n"
                    f"`등급` {r['grade']}\n"
                    f"`원금` **{금액:,}원**\n"
                    f"`보너스` {bonus_txt}\n"
                    f"`예상 수익` {expect}\n"
                    f"`만기` <t:{r['end_ts']}:f> (<t:{r['end_ts']}:R>)\n"
                    f"`잔액` **{r['balance']:,}원**",
                    ui.WIN, user, SECTION)
        e.set_thumbnail(url=ui.emoji_url(emoji))
        e.set_footer(text="만기가 되면 자동 정산 · /알림설정 에서 만기 DM 을 켤 수 있어요")
        await interaction.followup.send(embed=e)

    async def contract_autocomplete(self, interaction: discord.Interaction, current: str):
        out = []
        for c in await self.db.active(interaction.user.id):
            label = f"#{c['id']} {SPONSORS[c['sponsor']][1]} {c['days']}일 · {c['amount']:,}원{' 🔁' if c['auto'] else ''}"
            if current in label:
                out.append(app_commands.Choice(name=label, value=str(c["id"])))
        return out[:25]

    async def _find(self, user_id: int, cid: str) -> Optional[dict]:
        return next((c for c in await self.db.active(user_id) if str(c["id"]) == cid.lstrip("#")), None)

    @app_commands.command(name="스폰서해지", description=f"진행 중인 계약을 중도 해지합니다 (이자 없음 · 원금의 {_pct(CANCEL_FEE)} 수수료)")
    @app_commands.describe(계약="해지할 계약")
    @app_commands.autocomplete(계약=contract_autocomplete)
    async def cancel_contract(self, interaction: discord.Interaction, 계약: str):
        user = interaction.user
        c = await self._find(user.id, 계약)
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

    @app_commands.command(name="스폰서재계약", description="진행 중인 계약의 자동 재계약을 켜거나 끕니다")
    @app_commands.describe(계약="대상 계약", 켜기="켜기(True) / 끄기(False)")
    @app_commands.autocomplete(계약=contract_autocomplete)
    async def toggle_auto(self, interaction: discord.Interaction, 계약: str, 켜기: bool):
        user = interaction.user
        c = await self._find(user.id, 계약)
        if c is None or not await self.db.set_auto(user.id, c["id"], 켜기):
            return await interaction.response.send_message(
                embed=ui.card("❌ 계약을 찾을 수 없어요", "`/스폰서`에서 진행 중인 계약 번호를 확인해 주세요.", ui.LOSE, user, SECTION))
        msg = ("만기 때 지급액에서 같은 원금(최대 이전 원금 · 등급 한도)을 떼어 **같은 스폰서 · 같은 기간**으로 다시 계약해요."
               if 켜기 else "만기 때 정산만 하고 계약을 끝내요.")
        await interaction.response.send_message(embed=ui.card(
            f"🔁 자동 재계약 {'ON' if 켜기 else 'OFF'} — {_name(c['sponsor'])} `#{c['id']}`", msg,
            ui.WIN if 켜기 else ui.EVEN, user, SECTION))


async def setup(bot: commands.Bot):
    await bot.add_cog(Sponsor(bot))
