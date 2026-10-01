# cogs/prospect.py — 2.4 유망주: 나만의 선수 생성 · 성장 · 경기 기록 · 은퇴(전성기 커리어) · 영구결번
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import release
from services import ui
from services.club_db import (
    PROSPECT_DAILY_GROWTH, PROSPECT_FEET, PROSPECT_HEIGHT, PROSPECT_NAME_MAX, PROSPECT_POSITIONS, PROSPECT_PRICE,
    PROSPECT_PRIME_END, PROSPECT_RETIRE_AGE, PROSPECT_YEAR, ClubDB, hidden_tier, kst_day, prospect_attrs,
    prospect_input, prospect_xp_need,
)
from services.player_market_db import NATIONS

SECTION = "🌟 유망주"
RETIRE_WHY = {"self": "👋 은퇴 선언", "age": f"👴 {PROSPECT_RETIRE_AGE}세 은퇴", "steroid": "💉 부작용 은퇴"}
RULES = (f"1살 = 실제 {PROSPECT_YEAR // 86400}일 · {PROSPECT_PRIME_END}세까지 경기로 성장(하루 {PROSPECT_DAILY_GROWTH}경기) · "
         f"{PROSPECT_PRIME_END + 1}세부터 노쇠 · {PROSPECT_RETIRE_AGE}세 은퇴")


def _bday(b: str) -> str:
    m, d = b.split("-")
    return f"{int(m)}월 {int(d)}일"


def _profile(p: dict) -> str:
    return (f"`국적` {p['nation']} · `포지션` {p['position']} {PROSPECT_POSITIONS[p['position']]} · `등번호` **#{p['number']}**\n"
            f"`생일` {_bday(p['birthday'])} · `주발` {p['foot']} · `키` {p['height']}cm · `몸무게` {p['weight']}kg")


def prospect_embed(p: dict, owner, now: int) -> discord.Embed:
    """현역이면 지금 능력치 · 성장, 은퇴했으면 전성기 커리어 카드."""
    retired = bool(p["retired_ts"])
    if retired:
        title = f"🏛️ {p['name']} #{p['number']} — 전성기 커리어"
        body = (f"`전성기` OVR **{p['peak_ovr']}** ({p['peak_age']}세) · `잠재력` {p['pot']} ({p['pot_grade']})\n"
                f"`은퇴` {RETIRE_WHY.get(p['retire_reason'], '은퇴')} · {p['age']}세 · <t:{p['retired_ts']}:D>"
                + (" · 🏅 **영구결번**" if p["retired_number"] else ""))
    else:
        title = f"🌟 {p['name']} #{p['number']}"
        need = prospect_xp_need(p["ovr"])
        if p["age"] > PROSPECT_PRIME_END:
            grow = f"📉 {PROSPECT_PRIME_END + 1}세부터는 성장이 멈추고 해마다 OVR이 1~3 떨어져요 ({PROSPECT_RETIRE_AGE}세 은퇴)"
        elif p["ovr"] >= p["pot"]:
            grow = "✨ 잠재력에 도달했어요 — 💉 스테로이드로 잠재력을 더 올릴 수 있어요"
        else:
            grow = f"`성장` {ui.bar(p['xp'], need)} {p['xp']}/{need} → OVR {p['ovr'] + 1}"
        played = p["day_n"] if p["day_key"] == kst_day(now) else 0
        body = (f"`나이` **{p['age']}세** · `OVR` **{p['ovr']}** · `잠재력` **{p['pot']}** ({p['pot_grade']}) · "
                f"`최고` {p['peak_ovr']}\n{grow}\n"
                f"`오늘 성장 경기` {min(played, PROSPECT_DAILY_GROWTH)}/{PROSPECT_DAILY_GROWTH}"
                + (f"\n🚑 **부상** — {p['injury']} · 복귀 <t:{p['injured_until']}:R> (그때까지 경기에 못 나가요)"
                   if p["injured"] else ""))
    e = ui.card(title, _profile(p) + "\n\n" + body, ui.GOLD if retired else ui.INFO, owner, SECTION)
    ovr = p["peak_ovr"] if retired else p["ovr"]
    e.add_field(name="📊 능력치" + (" (전성기)" if retired else ""), inline=True,
                value="\n".join(f"`{n}` **{v}** {ui.bar(v, 99, 6)}" for n, v in prospect_attrs(p["pid"], p["group"], ovr)))
    apps = p["apps"]
    e.add_field(name="📈 커리어", inline=True, value=(
        f"`출전` **{apps:,}**경기\n`골` **{p['goals']:,}** · `도움` **{p['assists']:,}**\n"
        f"`경기당 공격포인트` {(p['goals'] + p['assists']) / apps:.2f}") if apps
        else "아직 출전 기록이 없어요.\n`/선발`로 내 구단에 넣고\n`/친선경기` `/공식경기`에 내보내 보세요!")
    e.add_field(name="🔒 히든 능력치", inline=True, value=(
        f"`자신감` {hidden_tier(p['confidence'])}\n`부상 빈도` {hidden_tier(p['proneness'])}\n"
        f"`프로 의식` {hidden_tier(p['pro'])}" + (f"\n`부상 이력` {p['injuries']}회" if p["injuries"] else "")))
    e.set_footer(text=RULES)
    return e


def _legend_line(p: dict) -> str:
    return (f"{'🏅' if p['retired_number'] else '▫️'} **#{p['number']} {p['name']}** · 전성기 OVR {p['peak_ovr']} "
            f"({p['peak_age']}세) · {p['apps']:,}경기 {p['goals']:,}골 {p['assists']:,}도움")


class _OwnerView(discord.ui.View):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("🙅 본인만 누를 수 있어요.", ephemeral=True)
            return False
        return True


class CreateConfirm(_OwnerView):
    """생성 확인 (본인에게만) → 입단 소식은 채널에 공개."""

    def __init__(self, cog: "Prospect", user, info: dict, price: int = PROSPECT_PRICE):
        super().__init__(timeout=180)
        self.cog, self.user, self.info = cog, user, info
        self.confirm.label = f"{price // 10_000:,}만원에 생성"

    @discord.ui.button(label=f"{PROSPECT_PRICE // 10_000:,}만원에 생성", emoji="✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        now = int(time.time())
        r = await self.cog.clubs.create_prospect(self.user.id, self.info, now,
                                                 rookie=release.preview(interaction.guild_id))
        if not r["ok"]:
            msg = {"exists": "이미 현역 유망주가 있어요. 한 번에 한 명만 키울 수 있어요.",
                   "retired_number": f"**#{self.info['number']}**은(는) 영구결번이에요.",
                   "balance": f"**{r.get('price', PROSPECT_PRICE):,}원**이 필요해요. (잔액 {r.get('balance', 0):,}원)"
                   }[r["reason"]]
            return await interaction.response.edit_message(
                embed=ui.card("❌ 유망주 생성 실패", msg, ui.LOSE, self.user, SECTION), view=None)
        await interaction.response.edit_message(
            embed=ui.card("✅ 생성 완료", "입단 소식은 채널에 올라갔어요.", ui.DARK, self.user, SECTION), view=None)
        e = prospect_embed(r, self.user, now)
        e.title = f"🎉 유망주 {r['name']} #{r['number']} 입단!"
        half = " (🚀 신인 반값)" if r["price"] < PROSPECT_PRICE else ""
        e.description = f"`비용` **-{r['price']:,}원**{half} · `잔액` **{r['balance']:,}원**\n\n" + e.description
        e.color = ui.GOLD
        await interaction.followup.send(embed=e)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            embed=ui.card("유망주 생성 취소", "돈은 빠져나가지 않았어요.", ui.EVEN, self.user, SECTION), view=None)


class RetireConfirm(_OwnerView):
    """은퇴 확인 — [🏅 은퇴 + 영구결번] [👋 은퇴만] [취소]."""

    def __init__(self, cog: "Prospect", user, p: dict):
        super().__init__(timeout=120)
        self.cog, self.user, self.p = cog, user, p
        for label, emoji, style, number in ((f"은퇴 + #{p['number']} 영구결번", "🏅", discord.ButtonStyle.primary, True),
                                            ("은퇴만", "👋", discord.ButtonStyle.danger, False)):
            b = discord.ui.Button(label=label, emoji=emoji, style=style)
            b.callback = self._retire(number)
            self.add_item(b)
        cancel = discord.ui.Button(label="취소", style=discord.ButtonStyle.secondary)
        cancel.callback = self._cancel
        self.add_item(cancel)

    def _retire(self, number: bool):
        async def cb(interaction: discord.Interaction):
            self.stop()
            now = int(time.time())
            r = await self.cog.clubs.retire_prospect(self.user.id, self.p["id"], now, number)
            if not r:
                return await interaction.response.edit_message(
                    embed=ui.card("❌ 은퇴 처리 불가", "이미 은퇴한 선수예요.", ui.LOSE, self.user, SECTION), view=None)
            e = prospect_embed(r, self.user, now)
            e.title = f"👋 {r['name']} #{r['number']} 은퇴 — 전성기 커리어로 저장"
            e.description = ((f"🏅 **#{r['number']}**은(는) 이제 영구결번이에요.\n" if number else "")
                             + "`/유망주생성`으로 새 유망주를 만들 수 있어요.\n\n" + e.description)
            await interaction.response.edit_message(embed=e, view=None)
        return cb

    async def _cancel(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(
            embed=ui.card("은퇴 취소", f"**{self.p['name']}**은(는) 계속 뜁니다! 💪", ui.EVEN, self.user, SECTION), view=None)


class DeleteConfirm(_OwnerView):
    """유망주 삭제 확인 — 기록 없이 사라진다."""

    def __init__(self, cog: "Prospect", user, p: dict):
        super().__init__(timeout=60)
        self.cog, self.user, self.p = cog, user, p

    @discord.ui.button(label="유망주 삭제", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        r = await self.cog.clubs.delete_prospect(self.user.id, self.p["id"], int(time.time()))
        e = (ui.card(f"🗑️ {r['name']} #{r['number']} 삭제 완료",
                     "기록 없이 사라졌어요. `/유망주생성`으로 새 유망주를 만들 수 있어요.", ui.EVEN, self.user, SECTION)
             if r else ui.card("❌ 삭제 불가", "이미 은퇴했거나 삭제된 선수예요.", ui.LOSE, self.user, SECTION))
        await interaction.response.edit_message(embed=e, view=None)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            embed=ui.card("유망주 삭제 취소", "아무것도 바뀌지 않았어요.", ui.EVEN, self.user, SECTION), view=None)


class Prospect(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.clubs = ClubDB()

    # ───────────── 자동완성 ─────────────
    async def nation_autocomplete(self, interaction: discord.Interaction, current: str):
        names = [n for n, _ in NATIONS if current in n]
        out = [app_commands.Choice(name=n, value=n) for n in names]
        if current and current not in names:
            out.insert(0, app_commands.Choice(name=f"{current} (직접 입력)"[:100], value=current[:100]))
        return out[:25]

    async def retired_autocomplete(self, interaction: discord.Interaction, current: str):
        data = await self.clubs.prospects(interaction.user.id, int(time.time()))
        return [app_commands.Choice(name=f"#{r['number']} {r['name']} · 전성기 OVR {r['peak_ovr']}"[:100], value=str(r["id"]))
                for r in data["retired"]
                if not r["retired_number"] and (not current or current in f"#{r['number']} {r['name']}")][:25]

    # ───────────── 보기 ─────────────
    @app_commands.command(name="유망주", description="나만의 유망주 — 세부 능력치 · 성장 · 커리어 기록 · 명예의 전당 (다른 유저도 가능)")
    @app_commands.describe(유저="볼 유저 (비우면 나)")
    async def show(self, interaction: discord.Interaction, 유저: Optional[discord.Member] = None):
        await interaction.response.defer()
        owner, now = 유저 or interaction.user, int(time.time())
        mine = owner.id == interaction.user.id
        data = await self.clubs.prospects(owner.id, now)
        if data["active"]:
            e = prospect_embed(data["active"], owner, now)
        else:
            text = (f"아직 현역 유망주가 없어요.\n`/유망주생성`으로 **{PROSPECT_PRICE:,}원**에 나만의 선수를 만들어 보세요!\n"
                    "이름 · 국적 · 포지션 · 등번호 · 생일까지 직접 정할 수 있어요.") if mine \
                else f"{owner.display_name}님은 현역 유망주가 없어요."
            e = ui.card("🌟 유망주", text, ui.EVEN, owner, SECTION)
            e.set_footer(text=RULES)
        if data["retired"]:
            lines = [_legend_line(r) for r in data["retired"][:10]]
            if mine and any(not r["retired_number"] for r in data["retired"]):
                lines.append("*`/영구결번`으로 은퇴한 선수의 등번호를 남길 수 있어요*")
            e.add_field(name="🏛️ 명예의 전당", value="\n".join(lines), inline=False)
        await interaction.followup.send(embed=e)

    # ───────────── 생성 ─────────────
    @app_commands.command(name="유망주생성",
                          description=f"나만의 유망주를 만듭니다 ({PROSPECT_PRICE // 10_000:,}만원) — 이름 · 국적 · 포지션 · 등번호 · 생일을 직접")
    @app_commands.describe(이름=f"선수 이름 (최대 {PROSPECT_NAME_MAX}자)", 국적="국적 (목록에서 고르거나 직접 입력)",
                           포지션="세부 포지션", 등번호="등번호 1~99", 생일="생일 (예: 3-15, 3월 15일)",
                           주발="주로 쓰는 발 (기본: 오른발)", 키="키 cm (기본: 180)")
    @app_commands.choices(포지션=[app_commands.Choice(name=f"{k} — {v}", value=k) for k, v in PROSPECT_POSITIONS.items()],
                          주발=[app_commands.Choice(name=f, value=f) for f in PROSPECT_FEET])
    @app_commands.autocomplete(국적=nation_autocomplete)
    async def create(self, interaction: discord.Interaction, 이름: str, 국적: str, 포지션: str,
                     등번호: app_commands.Range[int, 1, 99], 생일: str, 주발: str = "오른발",
                     키: app_commands.Range[int, PROSPECT_HEIGHT[0], PROSPECT_HEIGHT[1]] = 180):
        user = interaction.user
        info, err = prospect_input(이름, 국적, 포지션, 등번호, 생일, 주발, 키)
        data = await self.clubs.prospects(user.id, int(time.time()))
        if info and data["active"]:
            err = (f"이미 현역 유망주 **{data['active']['name']}**이(가) 있어요. 한 번에 한 명만 키울 수 있어요.\n"
                   "`/유망주은퇴` 후에 새로 만들 수 있어요.")
        elif info and info["number"] in {r["number"] for r in data["retired"] if r["retired_number"]}:
            err = f"**#{info['number']}**은(는) 영구결번이에요. 다른 번호를 골라 주세요."
        if err:
            return await interaction.response.send_message(
                embed=ui.card("❌ 유망주 생성 불가", err, ui.LOSE, user, SECTION), ephemeral=True)
        price = await self.clubs.prospect_price(user.id, int(time.time()), release.preview(interaction.guild_id))
        half = f" ~~{PROSPECT_PRICE:,}원~~ 🚀 신인 반값" if price < PROSPECT_PRICE else ""
        e = ui.card(f"🌟 {info['name']} #{info['number']} — 이 선수로 만들까요?",
                    _profile({**info, "weight": round(info["height"] ** 2 * 22.5 / 10_000)}) + "\n\n"
                    f"`비용` **{price:,}원**{half} · 17세 · OVR 50~58 · 잠재력 75~94 중 랜덤으로 태어나요\n"
                    "🔒 히든 능력치(자신감 · 부상 빈도 · 프로 의식)도 태어날 때 정해져요\n"
                    "이름 · 등번호 같은 정보는 나중에 바꿀 수 없어요.\n\n"
                    "🔒 이적시장 · 판매 · 트레이드 불가 — 오직 내 구단에서만 뛰어요", ui.INFO, user, SECTION)
        await interaction.response.send_message(embed=e, view=CreateConfirm(self, user, info, price), ephemeral=True)

    # ───────────── 은퇴 · 영구결번 ─────────────
    @app_commands.command(name="유망주은퇴", description="내 유망주를 은퇴시킵니다 — 전성기 커리어로 저장 · 등번호 영구결번 가능")
    async def retire(self, interaction: discord.Interaction):
        user, now = interaction.user, int(time.time())
        p = (await self.clubs.prospects(user.id, now))["active"]
        if not p:
            return await interaction.response.send_message(
                embed=ui.card("🙅 현역 유망주가 없어요", "`/유망주생성`으로 먼저 만들어 주세요.", ui.LOSE, user, SECTION),
                ephemeral=True)
        e = prospect_embed(p, user, now)
        e.title = f"👋 {p['name']} #{p['number']}을(를) 은퇴시킬까요?"
        e.color = ui.DOOM
        e.description = ("은퇴는 되돌릴 수 없어요. **전성기 커리어**로 명예의 전당에 남고, 새 유망주를 만들 수 있어요.\n\n"
                         + e.description)
        await interaction.response.send_message(embed=e, view=RetireConfirm(self, user, p))

    @app_commands.command(name="유망주삭제", description="내 현역 유망주를 삭제합니다 (기록 · 명예의 전당에 남지 않고 환불 없음)")
    async def delete(self, interaction: discord.Interaction):
        user, now = interaction.user, int(time.time())
        p = (await self.clubs.prospects(user.id, now))["active"]
        if not p:
            return await interaction.response.send_message(
                embed=ui.card("🙅 현역 유망주가 없어요", "삭제할 유망주가 없어요.", ui.LOSE, user, SECTION), ephemeral=True)
        e = ui.card(f"⚠️ {p['name']} #{p['number']}을(를) 삭제할까요?",
                    f"`OVR` **{p['ovr']}** · `잠재력` **{p['pot']}** ({p['pot_grade']}) · {p['age']}세 · "
                    f"{p['apps']:,}경기 {p['goals']:,}골 {p['assists']:,}도움\n\n"
                    f"삭제하면 **기록이 모두 사라지고** 명예의 전당에도 남지 않아요. 생성비 {PROSPECT_PRICE:,}원은 돌려받지 못해요.\n"
                    "기록을 남기려면 `/유망주은퇴`를 써 주세요.", ui.DOOM, user, SECTION)
        await interaction.response.send_message(embed=e, view=DeleteConfirm(self, user, p))

    @app_commands.command(name="영구결번", description="은퇴한 내 유망주의 등번호를 영구결번으로 남깁니다 (그 번호는 다시 못 써요)")
    @app_commands.describe(선수="은퇴한 유망주")
    @app_commands.autocomplete(선수=retired_autocomplete)
    async def retire_number(self, interaction: discord.Interaction, 선수: str):
        user, now = interaction.user, int(time.time())
        r = await self.clubs.retire_number(user.id, int(선수), now) if 선수.isdigit() else {"ok": False, "reason": "none"}
        if not r["ok"]:
            num = f"#{r.get('number')}"
            msg = {"none": "은퇴한 내 유망주를 자동완성에서 골라 주세요.", "done": f"**{num}**은(는) 이미 영구결번이에요.",
                   "taken": f"**{num}**은(는) 이미 다른 선수로 영구결번돼 있어요.",
                   "wearing": f"지금 현역 유망주가 **{num}**을(를) 달고 있어요."}[r["reason"]]
            return await interaction.response.send_message(
                embed=ui.card("❌ 영구결번 불가", msg, ui.LOSE, user, SECTION), ephemeral=True)
        e = prospect_embed(r, user, now)
        e.title = f"🏅 #{r['number']} 영구결번 — {r['name']}"
        e.description = f"이제 **#{r['number']}**은(는) 누구도 달 수 없어요. `/구단`에도 걸려요.\n\n" + e.description
        await interaction.response.send_message(embed=e)


async def setup(bot: commands.Bot):
    await bot.add_cog(Prospect(bot))
