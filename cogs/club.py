# cogs/club.py
# 구단: 생성·삭제·이름 / 포메이션·선발·자동편성·주장 / 구단 보기 / 친선경기
import asyncio
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from services import ui
from services.club_db import (
    CLUB_NAME_MAX, FORMATIONS, SLOT_GROUP, ClubDB, effective_ovr, simulate_match,
)
from services.economy_db import EconomyDB
from services.player_market_db import PlayerMarketDB

_LINES = [("FW", "⚽ 공격"), ("MF", "🎯 미드필드"), ("DF", "🛡️ 수비"), ("GK", "🧤 골키퍼")]
_NO_CLUB = "아직 구단이 없습니다. `/구단생성`으로 먼저 만들어 주세요."


def _slot_text(s: dict, captain: Optional[str]) -> str:
    if not s.get("player_id"):
        return f"{s['slot']} —"
    eff = effective_ovr(s["ovr"], s["pos"], s["slot"])
    power = f"{s['ovr']}" if eff == s["ovr"] else f"{s['ovr']}→{eff}⚠️"
    cap = " ©️" if s["player_id"] == captain else ""
    return f"{s['slot']} **{s['name']}** {power}{cap}"


def _team_embed(team: dict, owner: discord.abc.User) -> discord.Embed:
    captain = team["captain"]
    # 빈 자리(player_id None)와 주장 없음(None)이 같다고 판정되지 않게 captain 부터 확인한다.
    cap_name = next((s["name"] for s in team["lineup"] if captain and s.get("player_id") == captain), None)
    head = (
        f"`포메이션` **{team['formation']}** · `전력` **{team['rating']}**\n"
        f"`선발` {team['filled']}/11명 · `주장` {cap_name or '없음'}"
        + (f" · `케미` +{team['chem']}" if team["chem"] else "") + "\n"
        f"`전적` {team['wins']}승 {team['draws']}무 {team['losses']}패"
    )
    body = []
    for group, label in _LINES:
        row = [_slot_text(s, captain) for s in team["lineup"] if SLOT_GROUP[s["slot"]] == group]
        body.append(f"**{label}**\n" + " · ".join(row))
    desc = head + "\n\n" + "\n".join(body)
    if team["filled"] == 0:
        desc += "\n\n선발 명단이 비어 있습니다. `/자동편성`으로 바로 채울 수 있어요."
    e = ui.card(f"🏟️ {team['name']}", desc, ui.INFO, owner, "🏟️ 구단")
    e.set_thumbnail(url=owner.display_avatar.url)
    return e


class DeleteConfirm(discord.ui.View):
    def __init__(self, cog: "Club", owner_id: int):
        super().__init__(timeout=30)
        self.cog, self.owner_id = cog, owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("구단주만 누를 수 있습니다.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🗑️ 구단 삭제", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        ok = await self.cog.clubs.delete_club(self.owner_id)
        msg = ("구단을 삭제했습니다. 돈과 선수 카드는 그대로 남아 있고, 아마추어 스쿼드와 선발 명단은 사라졌습니다."
               if ok else "이미 삭제된 구단입니다.")
        await interaction.response.edit_message(
            embed=ui.card("🗑️ 구단 삭제 완료" if ok else "❌ 삭제 실패", msg, ui.EVEN if ok else ui.LOSE,
                          interaction.user, "🏟️ 구단"),
            view=None,
        )

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            embed=ui.card("구단 삭제 취소", "아무것도 바뀌지 않았습니다.", ui.EVEN, interaction.user, "🏟️ 구단"),
            view=None,
        )


class Club(commands.Cog):
    BONUS = 50000          # 구단 생성 보너스 — 계정당 한 번
    MATCH_COOLDOWN = 30    # 친선경기 연타 방지(초)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.clubs = ClubDB()
        self.money = EconomyDB()
        self.pm = PlayerMarketDB()
        self._last_match: dict[int, float] = {}

    # ───────────── 자동완성 ─────────────
    async def slot_autocomplete(self, interaction: discord.Interaction, current: str):
        team = await self.clubs.get_team(interaction.user.id)
        if not team:
            return []
        out = []
        for s in team["lineup"]:
            who = f"{s['name']} ({s['ovr']})" if s.get("player_id") else "비어 있음"
            label = f"{s['index'] + 1}번 {s['slot']} — {who}"
            if current and current.lower() not in label.lower():
                continue
            out.append(app_commands.Choice(name=label[:100], value=str(s["index"])))
        return out[:25]

    async def squad_autocomplete(self, interaction: discord.Interaction, current: str):
        """선발할 선수 — 고른 자리에 맞는 선수가 위로 오도록 유효 능력치 순."""
        team = await self.clubs.get_team(interaction.user.id)
        if not team:
            return []
        try:
            slot = FORMATIONS[team["formation"]][int(getattr(interaction.namespace, "자리", "") or 0)]
        except (ValueError, IndexError):
            slot = None
        squad = await self.clubs.squad(interaction.user.id)
        rows = []
        for p in squad:
            if current and current.lower() not in p["name"].lower():
                continue
            eff = effective_ovr(p["ovr"], p["pos"], slot) if slot else p["ovr"]
            fit = "" if eff == p["ovr"] else f" → {eff}"
            rows.append((eff, app_commands.Choice(name=f"{p['name']} · {p['pos']} · OVR {p['ovr']}{fit}"[:100],
                                                  value=p["player_id"])))
        rows.sort(key=lambda r: -r[0])
        return [c for _, c in rows[:25]]

    async def xi_autocomplete(self, interaction: discord.Interaction, current: str):
        team = await self.clubs.get_team(interaction.user.id)
        if not team:
            return []
        return [
            app_commands.Choice(name=f"{s['slot']} {s['name']} ({s['ovr']})"[:100], value=s["player_id"])
            for s in team["lineup"]
            if s.get("player_id") and (not current or current.lower() in s["name"].lower())
        ][:25]

    # ───────────── 생성 · 삭제 · 이름 ─────────────
    @app_commands.command(name="구단생성", description=f"내 구단을 만듭니다. 이름은 자유 (최대 {CLUB_NAME_MAX}자, 비우면 '닉네임 FC')")
    @app_commands.describe(이름="구단 이름 (비우면 '닉네임 FC')")
    async def create_club(self, interaction: discord.Interaction, 이름: Optional[str] = None):
        await interaction.response.defer()
        user = interaction.user
        club_name = (이름 or f"{user.display_name} FC").strip()

        ok, msg, bonus = await self.clubs.create_club(user.id, club_name, int(time.time()))
        if not ok:
            return await interaction.followup.send(embed=ui.card("❌ 구단 생성 실패", msg, ui.LOSE, user, "🏟️ 구단"))

        await self.pm.give_amateur_squad(user.id)
        await self.clubs.auto_lineup(user.id)
        lines = [f"**{club_name}** 창단을 축하합니다! 🎉",
                 "아마추어 스쿼드 18명이 입단했고, 선발 11명은 자동으로 편성해 두었습니다."]
        if bonus:
            new_bal = await self.money.add_balance(user.id, self.BONUS)
            lines.append(f"\n`보너스` **+{self.BONUS:,}원** · `잔액` **{new_bal:,}원**")
        e = ui.card("🏟️ 구단 창단", "\n".join(lines), ui.WIN, user, "🏟️ 구단")
        e.set_footer(text="/구단 으로 스쿼드를 보고 /선발 · /포메이션 · /주장 으로 꾸며 보세요")
        await interaction.followup.send(embed=e)

    @app_commands.command(name="구단명변경", description=f"내 구단 이름을 변경합니다. (최대 {CLUB_NAME_MAX}자)")
    @app_commands.describe(이름="새 구단 이름")
    async def rename_club(self, interaction: discord.Interaction, 이름: str):
        await interaction.response.defer()
        ok, msg = await self.clubs.rename_club(interaction.user.id, 이름)
        text = f"구단명이 **{이름.strip()}**(으)로 바뀌었습니다." if ok else msg
        await interaction.followup.send(embed=ui.card("✏️ 구단명 변경" if ok else "❌ 변경 실패", text,
                                                      ui.WIN if ok else ui.LOSE, interaction.user, "🏟️ 구단"))

    @app_commands.command(name="구단삭제", description="내 구단을 삭제합니다. (돈·선수 카드는 유지)")
    async def delete_club(self, interaction: discord.Interaction):
        club = await self.clubs.get_club(interaction.user.id)
        if not club:
            return await interaction.response.send_message(_NO_CLUB, ephemeral=True)
        e = ui.card(
            f"⚠️ {club['name']} 을(를) 삭제할까요?",
            "구단 이름·포메이션·선발 명단·전적과 **아마추어 스쿼드**가 사라집니다.\n"
            "돈과 일반 선수 카드는 그대로 남습니다. 다시 창단해도 창단 보너스는 다시 지급되지 않습니다.",
            ui.DOOM, interaction.user, "🏟️ 구단",
        )
        await interaction.response.send_message(embed=e, view=DeleteConfirm(self, interaction.user.id))

    # ───────────── 보기 ─────────────
    @app_commands.command(name="구단", description="구단 정보와 선발 명단을 봅니다. (다른 유저 구단도 가능)")
    @app_commands.describe(유저="볼 구단의 주인 (비우면 내 구단)")
    async def show_club(self, interaction: discord.Interaction, 유저: Optional[discord.Member] = None):
        await interaction.response.defer()
        owner = 유저 or interaction.user
        team = await self.clubs.get_team(owner.id)
        if not team:
            text = _NO_CLUB if owner.id == interaction.user.id else f"{owner.display_name}님은 아직 구단이 없습니다."
            return await interaction.followup.send(embed=ui.card("🏟️ 구단", text, ui.EVEN, interaction.user, "🏟️ 구단"))
        await interaction.followup.send(embed=_team_embed(team, owner))

    # ───────────── 편성 ─────────────
    @app_commands.command(name="포메이션", description="구단 포메이션을 바꿉니다. 선발 11명은 새 자리에 맞게 다시 배치됩니다.")
    @app_commands.describe(포메이션="사용할 포메이션")
    @app_commands.choices(포메이션=[app_commands.Choice(name=f, value=f) for f in FORMATIONS])
    async def formation(self, interaction: discord.Interaction, 포메이션: app_commands.Choice[str]):
        await interaction.response.defer()
        ok, msg = await self.clubs.set_formation(interaction.user.id, 포메이션.value)
        if not ok:
            return await interaction.followup.send(embed=ui.card("❌ 포메이션", msg, ui.LOSE, interaction.user, "🏟️ 구단"))
        team = await self.clubs.get_team(interaction.user.id)
        e = _team_embed(team, interaction.user)
        e.title = f"📋 {msg.replace('**', '')}"
        if any(s.get("player_id") and effective_ovr(s["ovr"], s["pos"], s["slot"]) != s["ovr"] for s in team["lineup"]):
            e.description += "\n\n⚠️ 제 포지션이 아닌 선수가 있습니다. `/자동편성`으로 보유 선수 전체에서 다시 뽑을 수 있어요."
        await interaction.followup.send(embed=e)

    @app_commands.command(name="선발", description="선발 명단의 한 자리에 선수를 넣습니다. (선수를 비우면 그 자리를 비움)")
    @app_commands.describe(자리="바꿀 자리", 선수="넣을 선수 (비우면 자리를 비움)")
    @app_commands.autocomplete(자리=slot_autocomplete, 선수=squad_autocomplete)
    async def set_slot(self, interaction: discord.Interaction, 자리: str, 선수: Optional[str] = None):
        await interaction.response.defer()
        try:
            idx = int(자리)
        except ValueError:
            return await interaction.followup.send(
                embed=ui.card("❌ 선발", "자동완성에서 자리를 골라 주세요.", ui.LOSE, interaction.user, "🏟️ 구단"))
        ok, msg = await self.clubs.set_slot(interaction.user.id, idx, 선수)
        if not ok:
            return await interaction.followup.send(embed=ui.card("❌ 선발", msg, ui.LOSE, interaction.user, "🏟️ 구단"))
        team = await self.clubs.get_team(interaction.user.id)
        e = _team_embed(team, interaction.user)
        e.title = "📋 선발 변경"
        e.description = msg + "\n\n" + e.description
        await interaction.followup.send(embed=e)

    @app_commands.command(name="자동편성", description="보유 선수 중 가장 강한 11명을 현재 포메이션에 맞게 자동 배치합니다.")
    async def auto_lineup(self, interaction: discord.Interaction):
        await interaction.response.defer()
        ok, msg = await self.clubs.auto_lineup(interaction.user.id)
        if not ok:
            return await interaction.followup.send(embed=ui.card("❌ 자동편성", msg, ui.LOSE, interaction.user, "🏟️ 구단"))
        team = await self.clubs.get_team(interaction.user.id)
        e = _team_embed(team, interaction.user)
        e.title = "🤖 자동 편성 완료"
        await interaction.followup.send(embed=e)

    @app_commands.command(name="주장", description="선발 11명 중 주장을 정합니다. (전력 +1)")
    @app_commands.describe(선수="주장으로 임명할 선수")
    @app_commands.autocomplete(선수=xi_autocomplete)
    async def captain(self, interaction: discord.Interaction, 선수: str):
        await interaction.response.defer()
        ok, msg = await self.clubs.set_captain(interaction.user.id, 선수)
        await interaction.followup.send(embed=ui.card("©️ 주장 임명" if ok else "❌ 주장", msg,
                                                      ui.WIN if ok else ui.LOSE, interaction.user, "🏟️ 구단"))

    # ───────────── 친선경기 ─────────────
    @staticmethod
    def _caster_line(home: str, away: str, hg: int, ag: int) -> str:
        if hg == ag:
            return "양 팀 모두 골문을 열지 못했습니다. 0 대 0 무승부!" if hg == 0 else "팽팽한 접전! 끝내 승부를 가리지 못했습니다."
        winner = home if hg > ag else away
        if abs(hg - ag) >= 3:
            return f"일방적인 경기였습니다! 오늘의 주인공은 {winner}!"
        return f"치열한 승부 끝에 웃은 쪽은 {winner}!"

    @app_commands.command(name="친선경기", description="다른 유저의 구단과 친선경기를 치릅니다. (돈은 걸리지 않음, 전적 기록)")
    @app_commands.describe(상대="상대 구단의 주인")
    async def friendly(self, interaction: discord.Interaction, 상대: discord.Member):
        user = interaction.user
        if 상대.id == user.id or 상대.bot:
            return await interaction.response.send_message("다른 유저의 구단을 골라 주세요.", ephemeral=True)
        now = time.monotonic()
        if now - self._last_match.get(user.id, 0.0) < self.MATCH_COOLDOWN:
            return await interaction.response.send_message(
                "선수들이 아직 회복 중이에요. 잠시 후 다시 경기를 잡아 주세요!", ephemeral=True)
        await interaction.response.defer()

        home, away = await self.clubs.get_team(user.id), await self.clubs.get_team(상대.id)
        if not home or not away:
            who = "내" if not home else f"{상대.display_name}님의"
            return await interaction.followup.send(
                embed=ui.card("❌ 친선경기", f"{who} 구단이 없습니다.", ui.LOSE, user, "🎙️ 친선경기 중계"))
        if not home["filled"] or not away["filled"]:
            who = "내" if not home["filled"] else f"{상대.display_name}님의"
            return await interaction.followup.send(
                embed=ui.card("❌ 친선경기", f"{who} 선발 명단이 비어 있습니다. `/자동편성`을 먼저 해 주세요.",
                              ui.LOSE, user, "🎙️ 친선경기 중계"))
        self._last_match[user.id] = now

        def side(team):
            return {"name": team["name"], "rating": team["rating"],
                    "xi": [s for s in team["lineup"] if s.get("player_id")]}
        result = simulate_match(side(home), side(away))
        hg, ag = result["home"], result["away"]
        await self.clubs.record_match(user.id, 상대.id, hg, ag)

        kickoff = ui.card(f"🏟️ {home['name']} vs {away['name']}",
                          f"> 🎙️ *\"전력 {home['rating']} 대 {away['rating']}! 주심의 휘슬과 함께 킥오프!\"*",
                          ui.DARK, user, "🎙️ 친선경기 중계")
        goals = "\n".join(
            f"⚽ {g['minute']}' **{g['scorer']}** ({home['name'] if g['side'] == 'home' else away['name']})"
            for g in result["goals"]
        ) or "골 없음"
        color = ui.WIN if hg > ag else (ui.LOSE if hg < ag else ui.EVEN)
        final = ui.card(
            f"🏟️ {home['name']} {hg} : {ag} {away['name']}",
            f"> 🎙️ *\"{self._caster_line(home['name'], away['name'], hg, ag)}\"*\n\n{goals}\n\n"
            f"`전력` {home['rating']} vs {away['rating']}",
            color, user, "🎙️ 친선경기 중계",
        )
        try:
            msg = await interaction.followup.send(embed=kickoff, wait=True)
            await asyncio.sleep(1.5)
            await msg.edit(embed=final)
        except discord.HTTPException:
            await interaction.followup.send(embed=final)


async def setup(bot: commands.Bot):
    await bot.add_cog(Club(bot))
