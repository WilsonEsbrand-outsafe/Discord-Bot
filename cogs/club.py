# cogs/club.py
# 구단: 생성·삭제·이름 / 포메이션·선발·자동편성·주장 / 구단 보기 / 친선경기
import asyncio
import random
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from services import ui
from services.club_db import (
    CLUB_NAME_MAX, ELITE_CLUBS, FORMATIONS, MANAGERS, MEDICS, OFFICIAL_MIN_BET, PROSPECT_XP_LABEL, SLOT_GROUP, ClubDB, effective_ovr,
    match_highlights, official_odds, season_key, simulate_match, win_probs,
)
from services.economy_db import MUFFLER_BONUS, EconomyDB
from services.player_market_db import PlayerMarketDB

_LINES = [("FW", "⚽ 공격"), ("MF", "🎯 미드필드"), ("DF", "🛡️ 수비"), ("GK", "🧤 골키퍼")]
_NO_CLUB = "아직 구단이 없습니다. `/구단생성`으로 먼저 만들어 주세요."


def _slot_text(s: dict, captain: Optional[str]) -> str:
    if not s.get("player_id"):
        return f"{s['slot']} 🚑 {s['injured']}(부상)" if s.get("injured") else f"{s['slot']} —"
    eff = effective_ovr(s["ovr"], s["pos"], s["slot"])
    power = f"{s['ovr']}" if eff == s["ovr"] else f"{s['ovr']}→{eff}⚠️"
    cap = " ©️" if s["player_id"] == captain else ""
    name = f"🌟**{s['name']}** #{s['number']}" if s.get("prospect") else f"**{s['name']}**"
    return f"{s['slot']} {name} {power}{cap}"


def _team_embed(team: dict, owner: discord.abc.User) -> discord.Embed:
    captain = team["captain"]
    # 빈 자리(player_id None)와 주장 없음(None)이 같다고 판정되지 않게 captain 부터 확인한다.
    cap_name = next((s["name"] for s in team["lineup"] if captain and s.get("player_id") == captain), None)
    head = (
        f"`포메이션` **{team['formation']}** · `전력` **{team['rating']}**\n"
        f"`선발` {team['filled']}/11명 · `주장` {cap_name or '없음'}"
        + (f" · `케미` +{team['chem']}" if team["chem"] else "") + "\n"
        f"`전적` {team['wins']}승 {team['draws']}무 {team['losses']}패"
        + (f"\n`감독` {MANAGERS[team['manager']][0]} {MANAGERS[team['manager']][1]} (+{team['manager_bonus']})"
           if team.get("manager") in MANAGERS else "")
        + (f"\n`의료진` {MEDICS[team['medic']][0]} {MEDICS[team['medic']][1]} (치료 {MEDICS[team['medic']][2]}%)"
           if team.get("medic") in MEDICS else "")
        + ("\n`영구결번` 🏅 " + " · ".join(f"#{n}" for n in team["retired_numbers"])
           if team.get("retired_numbers") else "")
        + (f"\n`경기장` 🏟️ {team['stadium']}" if team.get("stadium") else "")
        + (f"\n`스쿼드 B` {ELITE_CLUBS[team['elite']][0]} {ELITE_CLUBS[team['elite']][1]}"
           + (" · **경기에 사용 중**" if team["squad_b"] else "") if team.get("elite") in ELITE_CLUBS else "")
    )
    body = []
    for group, label in _LINES:
        row = [_slot_text(s, captain) for s in team["lineup"] if SLOT_GROUP[s["slot"]] == group]
        body.append(f"**{label}**\n" + " · ".join(row))
    desc = head + "\n\n" + "\n".join(body)
    if team["filled"] == 0:
        desc += "\n\n선발 명단이 비어 있습니다. `/자동편성`으로 바로 채울 수 있어요."
    if any(s.get("injured") for s in team["lineup"]):
        desc += "\n\n🚑 부상 중인 유망주는 복귀할 때까지 빈자리로 계산돼요. (자리는 그대로 지켜요)"
    e = ui.card(f"{team.get('emblem') or '🏟️'} {team['name']}", desc, ui.INFO, owner, "🏟️ 구단")
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

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.clubs = ClubDB()
        self.money = EconomyDB()
        self.pm = PlayerMarketDB()
        self._playing: set[int] = set()   # 경기 중인 유저 (중계가 겹치지 않게 — 끝나면 바로 다시 가능)

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
            star = "🌟 유망주 " if p.get("prospect") else ""
            rows.append((eff, app_commands.Choice(name=f"{star}{p['name']} · {p['pos']} · OVR {p['ovr']}{fit}"[:100],
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

    # ───────────── 경기 (친선 · 공식 공용) ─────────────
    BROADCAST_MINUTES = (15, 30, 45, 60, 75, 90)   # 문자중계 장면 (킥오프 다음부터)
    BROADCAST_GAP = 3.5                             # 장면 사이 간격(초) — 읽을 시간을 준다

    @staticmethod
    def _caster_line(home: str, away: str, hg: int, ag: int) -> str:
        if hg == ag:
            return "양 팀 모두 골문을 열지 못했습니다. 0 대 0 무승부!" if hg == 0 else "팽팽한 접전! 끝내 승부를 가리지 못했습니다."
        winner = home if hg > ag else away
        if abs(hg - ag) >= 3:
            return f"일방적인 경기였습니다! 오늘의 주인공은 {winner}!"
        return f"치열한 승부 끝에 웃은 쪽은 {winner}!"

    @staticmethod
    def _star_line(x: dict) -> str:
        """경기 결과 카드의 유망주 줄: 골 · 도움 · 경험치(감점 사유) · OVR 상승 · 부상."""
        line = f"\n🌟 **{x['name']}** #{x['number']} · {x['goals']}골 {x['assists']}도움"
        if x["xp"] is not None:
            why = ", ".join(PROSPECT_XP_LABEL[k] for k in x["minus"])
            line += f" · 경험치 **{x['xp']:+d}**" + (f" ({why})" if why else "")
        line += f" · OVR {x['ovr0']} → **{x['ovr']}** ⬆️" if x["ovr"] > x["ovr0"] else ""
        j = x.get("injury")
        if j:
            line += (f"\n　🚑 **{j['name']}** ({j['grade']}) · {j['hours']}시간 결장"
                     + (f" (의료진 -{j['heal']}%)" if j["heal"] else ""))
            if j["ovr"] < j["ovr0"] or j["pot"] < j["pot0"]:
                line += (f" · {'고질병 ' if j['chronic'] else ''}OVR {j['ovr0']}→{j['ovr']} · "
                         f"잠재력 {j['pot0']}→{j['pot']}")
        return line

    async def _load_sides(self, interaction, user, home_id: int, away_id: int, away_label: str, section: str):
        """두 구단을 불러오고 문제가 있으면 안내 후 None."""
        home, away = await self.clubs.match_team(home_id), await self.clubs.match_team(away_id)
        if not home or not away:
            who = "내" if not home else f"{away_label}의"
            await interaction.followup.send(embed=ui.card("❌ 경기 불가", f"{who} 구단이 없습니다.", ui.LOSE, user, section))
            return None
        if not home["filled"] or not away["filled"]:
            who = "내" if not home["filled"] else f"{away_label}의"
            await interaction.followup.send(embed=ui.card(
                "❌ 경기 불가", f"{who} 선발 명단이 비어 있습니다. `/자동편성`을 먼저 해 주세요.", ui.LOSE, user, section))
            return None
        return home, away

    async def _sides(self, home_id: int, away_id: int, home: dict, away: dict) -> list[dict]:
        """경기에 나설 두 팀 — 응원 머플러를 쓰는 중이면 1회 소모하고 전력 +3 (스쿼드 B 는 머플러 없음)."""
        sides = []
        for uid, team in ((home_id, home), (away_id, away)):
            muffler = team.get("squad") != "B" and await self.money.consume_buff(uid, "muffler")
            rating = team["rating"] + (MUFFLER_BONUS if muffler else 0)
            name = (f"{team['emblem']} " if team.get("emblem") else "") + team["name"]
            sides.append({"name": name + (" 🧣" if muffler else ""), "rating": rating, "stadium": team.get("stadium"),
                          "xi": [s for s in team["lineup"] if s.get("player_id")]})
        return sides

    async def _play_match(self, interaction, user, h: dict, a: dict, section: str, title_tag: str,
                          after=None, view=None, note: str = "") -> dict:
        """경기 시뮬레이션 → 90분 하이라이트 문자중계. after(result) 가 돌려준 문자열을 결과 카드에,
        note 는 킥오프 카드에 붙인다."""
        result = simulate_match(h, a)
        hg, ag = result["home"], result["away"]
        highlights = match_highlights(result, h, a)
        pw, pd, pl = win_probs(h["rating"], a["rating"])
        extra = await after(result) if after else ""
        # 양 팀 유망주 기록(출전 · 골 · 도움) + 성장
        stars = await self.clubs.record_prospects([(h["xi"], hg, ag), (a["xi"], ag, hg)], result["goals"],
                                                  int(time.time()), highlights)
        if stars:
            extra = "\n" + "".join(self._star_line(x) for x in stars) + extra

        def log_until(minute: int) -> str:
            lines = [f"`{x['minute']:>2}'` {x['icon']} "
                     + (f"**{x['text']}**" if x["goal"] else x["text"])
                     + f" *({h['name'] if x['side'] == 'home' else a['name']})*"
                     for x in highlights if x["minute"] <= minute]
            return "\n".join(lines[-8:]) or "*아직 큰 장면은 없습니다.*"

        def score(minute: int) -> tuple[int, int]:
            return (sum(g["side"] == "home" and g["minute"] <= minute for g in result["goals"]),
                    sum(g["side"] == "away" and g["minute"] <= minute for g in result["goals"]))

        venue = f"🏟️ **{h['stadium']}**\n" if h.get("stadium") else ""
        kickoff = ui.card(f"{title_tag} {h['name']} vs {a['name']}",
                          f"{venue}> 🎙️ *\"전력 {h['rating']} 대 {a['rating']}! 주심의 휘슬과 함께 킥오프!\"*\n\n"
                          f"`예상 승률` {h['name']} **{pw:.0%}** · 무 **{pd:.0%}** · {a['name']} **{pl:.0%}**" + note,
                          ui.DARK, user, section)
        color = ui.WIN if hg > ag else (ui.LOSE if hg < ag else ui.EVEN)
        final = ui.card(f"{title_tag} {h['name']} {hg} : {ag} {a['name']}",
                        f"> 🎙️ *\"{self._caster_line(h['name'], a['name'], hg, ag)}\"*\n\n{log_until(90)}\n\n"
                        f"`전력` {h['rating']} vs {a['rating']} · `예상 승률` {pw:.0%} / {pd:.0%} / {pl:.0%}" + extra,
                        color, user, section)
        try:
            msg = await interaction.followup.send(embed=kickoff, wait=True)
            for minute in self.BROADCAST_MINUTES[:-1]:
                await asyncio.sleep(self.BROADCAST_GAP)
                sh, sa = score(minute)
                label = "하프타임" if minute == 45 else f"{minute}'"
                await msg.edit(embed=ui.card(f"{title_tag} {h['name']} {sh} : {sa} {a['name']} · {label}",
                                             log_until(minute), ui.DARK, user, section))
            await asyncio.sleep(self.BROADCAST_GAP)
            await msg.edit(embed=final, view=view)
        except discord.HTTPException:
            await interaction.followup.send(embed=final, view=view)
        return result

    @app_commands.command(name="친선경기", description="다른 유저의 구단과 친선경기 (90분 문자중계 · 연속 가능 · 돈 없이 전적만 기록)")
    @app_commands.describe(상대="상대 구단의 주인")
    async def friendly(self, interaction: discord.Interaction, 상대: discord.Member):
        user = interaction.user
        if 상대.id == user.id or 상대.bot:
            return await interaction.response.send_message("다른 유저의 구단을 골라 주세요.", ephemeral=True)
        await self._friendly(interaction, user, 상대)

    async def _friendly(self, interaction, user, opp):
        if user.id in self._playing:
            return await interaction.response.send_message("⏳ 지금 경기가 진행 중이에요. 끝나면 바로 다시 붙을 수 있어요!",
                                                           ephemeral=True)
        await interaction.response.defer()
        self._playing.add(user.id)
        try:
            sides = await self._load_sides(interaction, user, user.id, opp.id, f"{opp.display_name}님", "🎙️ 친선경기 중계")
            if not sides:
                return
            h, a = await self._sides(user.id, opp.id, *sides)

            async def record(result):
                await self.clubs.record_match(user.id, opp.id, result["home"], result["away"])
                return ""
            await self._play_match(interaction, user, h, a, "🎙️ 친선경기 중계", "🤝",
                                   after=record, view=RematchView(self, user, opp))
        finally:
            self._playing.discard(user.id)

    PICKS = {"W": "승", "D": "무", "L": "패"}

    @app_commands.command(name="공식경기", description="돈을 걸고 공식경기! 전력으로 정한 배당 · 승/무/패 예측이 맞으면 배당 지급 (횟수 제한 없음)")
    @app_commands.rename(amount="베팅액")
    @app_commands.describe(amount="베팅 금액 (최소 1,000원)", 예측="내 구단 경기 결과 예측 (기본: 승)",
                           상대="상대 구단의 주인 (비우면 비슷한 전력 구단과 자동 매칭)")
    @app_commands.choices(예측=[app_commands.Choice(name=n, value=k) for k, n in
                              (("W", "승 — 내 구단 승리"), ("D", "무 — 무승부"), ("L", "패 — 내 구단 패배"))])
    async def official(self, interaction: discord.Interaction, amount: app_commands.Range[int, OFFICIAL_MIN_BET],
                       예측: str = "W", 상대: Optional[discord.Member] = None):
        user, now, amount = interaction.user, int(time.time()), int(amount)
        if 상대 is not None and (상대.id == user.id or 상대.bot):
            return await interaction.response.send_message("다른 유저의 구단을 골라 주세요.", ephemeral=True)
        if user.id in self._playing:
            return await interaction.response.send_message("⏳ 지금 경기가 진행 중이에요.", ephemeral=True)
        await interaction.response.defer()
        self._playing.add(user.id)
        try:
            await self._official(interaction, user, now, amount, 예측, 상대)
        finally:
            self._playing.discard(user.id)

    async def _official(self, interaction, user, now: int, amount: int, pick: str, opp_user):
        sec = "🏆 공식경기 중계"
        bal = await self.money.get_balance(user.id)
        if bal < amount:
            return await interaction.followup.send(embed=ui.card(
                "🙅 베팅할 돈이 부족해요", f"`베팅` **{amount:,}원**\n`잔액` **{bal:,}원**", ui.LOSE, user, sec))
        if opp_user is not None:
            sides = await self._load_sides(interaction, user, user.id, opp_user.id, f"{opp_user.display_name}님", sec)
            if not sides:
                return
            (me, opp), opp_id = sides, opp_user.id
        else:
            me = await self.clubs.match_team(user.id)
            if not me or not me["filled"]:
                return await interaction.followup.send(embed=ui.card(
                    "❌ 경기 불가", _NO_CLUB if not me else "선발 명단이 비어 있습니다. `/자동편성`을 먼저 해 주세요.",
                    ui.LOSE, user, sec))
            # 상대: 선발이 있는 다른 유저 구단 중 전력이 가장 비슷한 5팀에서 무작위
            pool = []
            for uid in await self.clubs.official_opponents(user.id):
                t = await self.clubs.match_team(uid)
                if t and t["filled"]:
                    pool.append((abs(t["rating"] - me["rating"]), uid, t))
            if not pool:
                return await interaction.followup.send(embed=ui.card(
                    "😶 상대 구단이 없어요", "선발 명단이 있는 다른 유저 구단이 아직 없습니다.", ui.EVEN, user, sec))
            pool.sort(key=lambda x: x[0])
            _, opp_id, opp = random.choice(pool[:5])

        h, a = await self._sides(user.id, opp_id, me, opp)
        odds = official_odds(win_probs(h["rating"], a["rating"]))
        board = " · ".join((f"**{self.PICKS[k]} {o:.2f}배** 👈" if k == pick else f"{self.PICKS[k]} {o:.2f}배")
                           for k, o in odds.items())
        note = f"\n`배당` {board}\n`베팅` **{amount:,}원** · 적중하면 **{ui.won(round(amount * (odds[pick] - 1)))}**"

        async def record(result):
            r = await self.clubs.record_official(user.id, opp_id, result["home"], result["away"], now, amount, pick,
                                                 odds[pick])
            mark = {"W": "승리", "D": "무승부", "L": "패배"}[r["result"]]
            hit = "🎯 적중!" if r["result"] == pick else "❌ 빗나감"
            return (f"\n\n`예측` **{self.PICKS[pick]}** @ {odds[pick]:.2f}배 · `결과` **{mark}** → {hit}\n"
                    f"`정산` **{ui.won(r['delta'])}** · `잔액` **{r['balance']:,}원**\n"
                    f"`이번 시즌` {r['points']}점 · {r['w']}승 {r['d']}무 {r['l']}패 · 득실 {r['gf'] - r['ga']:+d} · "
                    "순위는 `/공식순위`")
        await self._play_match(interaction, user, h, a, sec, "🏆", after=record, note=note)

    @app_commands.command(name="공식순위", description="이번 달 공식경기 시즌 순위")
    async def official_table(self, interaction: discord.Interaction):
        await interaction.response.defer()
        now = int(time.time())
        rows = await self.clubs.official_table(now, limit=10)
        season = season_key(now)
        medals = ("🥇", "🥈", "🥉")
        lines = [f"{medals[i] if i < 3 else f'`{i + 1}`'} **{r['name']}** · **{r['points']}점** · "
                 f"{r['w']}승 {r['d']}무 {r['l']}패 · 득실 {r['gf'] - r['ga']:+d}" for i, r in enumerate(rows)]
        e = ui.card(f"🏆 {season // 100}년 {season % 100}월 공식경기 순위",
                    "\n".join(lines) or "아직 이번 시즌 공식경기가 없어요. `/공식경기`로 첫 경기를 치러 보세요!",
                    ui.GOLD, interaction.user, "🏆 공식경기")
        e.set_footer(text="승 3점 · 무 1점 · 건 쪽과 상대 모두 기록 · 매달 1일 새 시즌")
        await interaction.followup.send(embed=e)

    # ───────────── 감독 ─────────────
    @app_commands.command(name="감독", description="감독을 영입합니다 — 전력 보너스, 선호 포메이션이면 추가 보너스 (비우면 목록)")
    @app_commands.describe(영입="영입할 감독 (비우면 현재 감독과 목록)")
    @app_commands.choices(영입=[app_commands.Choice(name=f"{e} {n} · {fee // 10_000:,}만원"[:100], value=k)
                              for k, (e, n, _f, _b, _x, fee, _d) in MANAGERS.items()])
    async def manager(self, interaction: discord.Interaction, 영입: Optional[str] = None):
        await interaction.response.defer()
        user = interaction.user
        if 영입:
            r = await self.clubs.hire_manager(user.id, 영입)
            e_, n_, fav, base, extra, fee, desc = MANAGERS[영입]
            if not r["ok"]:
                msg = {"no_club": _NO_CLUB, "same": f"이미 {e_} {n_} 감독과 함께하고 있어요.",
                       "balance": f"영입비 **{fee:,}원**이 필요해요. (잔액 {r.get('balance', 0):,}원)"}[r["reason"]]
                return await interaction.followup.send(embed=ui.card("❌ 감독 영입 실패", msg, ui.LOSE, user, "🏟️ 구단"))
            team = await self.clubs.get_team(user.id)
            e = ui.card(f"🤝 {e_} {n_} 감독 부임!",
                        f"> 💬 *\"{desc}\"*\n\n`영입비` **-{fee:,}원** · `잔액` **{r['balance']:,}원**\n"
                        f"`효과` 전력 +{base}" + (f" · **{fav}** 포메이션이면 +{extra} 추가" if fav else "") + "\n"
                        f"`지금 전력` **{team['rating']}** (감독 +{team['manager_bonus']})",
                        ui.WIN, user, "🏟️ 구단")
            return await interaction.followup.send(embed=e)

        team = await self.clubs.get_team(user.id)
        cur = team and team.get("manager")
        lines = []
        for k, (e_, n_, fav, base, extra, fee, desc) in MANAGERS.items():
            eff = f"전력 +{base}" + (f" · {fav} 이면 +{base + extra}" if fav else "")
            lines.append(f"{'✅' if k == cur else '▫️'} {e_} **{n_}** · {fee:,}원 · {eff}\n　 *{desc}*")
        head = (f"`현재 감독` {MANAGERS[cur][0]} **{MANAGERS[cur][1]}** · 보너스 +{team['manager_bonus']}"
                if cur else "`현재 감독` 없음") if team else _NO_CLUB
        e = ui.card("🧑‍💼 감독", head + "\n\n" + "\n".join(lines), ui.INFO, user, "🏟️ 구단")
        e.set_footer(text="/감독 영입:<감독> 으로 영입 · 영입비는 한 번만 · 감독을 바꾸면 새 영입비")
        await interaction.followup.send(embed=e)

    # ───────────── 의료진 ─────────────
    @app_commands.command(name="의료진", description="의료진을 영입합니다 — 치료능력만큼 유망주 부상 결장 기간 단축 (비우면 목록)")
    @app_commands.describe(영입="영입할 의료진 (비우면 현재 의료진과 목록)")
    @app_commands.choices(영입=[app_commands.Choice(name=f"{e} {n} · 치료 {h}% · {fee // 10_000:,}만원"[:100], value=k)
                              for k, (e, n, h, fee, _d) in MEDICS.items()])
    async def medic(self, interaction: discord.Interaction, 영입: Optional[str] = None):
        await interaction.response.defer()
        user = interaction.user
        if 영입:
            r = await self.clubs.hire_medic(user.id, 영입)
            e_, n_, heal, fee, desc = MEDICS[영입]
            if not r["ok"]:
                msg = {"no_club": _NO_CLUB, "same": f"이미 {e_} {n_}이(가) 함께하고 있어요.",
                       "balance": f"영입비 **{fee:,}원**이 필요해요. (잔액 {r.get('balance', 0):,}원)"}[r["reason"]]
                return await interaction.followup.send(embed=ui.card("❌ 의료진 영입 실패", msg, ui.LOSE, user, "🏟️ 구단"))
            return await interaction.followup.send(embed=ui.card(
                f"🤝 {e_} {n_} 합류!",
                f"> 💬 *\"{desc}\"*\n\n`영입비` **-{fee:,}원** · `잔액` **{r['balance']:,}원**\n"
                f"`치료능력` **{heal}%** — 유망주가 다치면 결장 기간이 {heal}% 줄어요", ui.WIN, user, "🏟️ 구단"))

        team = await self.clubs.get_team(user.id)
        cur = team and team.get("medic")
        lines = [f"{'✅' if k == cur else '▫️'} {e_} **{n_}** · {fee:,}원 · 치료 **{heal}%**\n　 *{desc}*"
                 for k, (e_, n_, heal, fee, desc) in MEDICS.items()]
        head = (f"`현재 의료진` {MEDICS[cur][0]} **{MEDICS[cur][1]}** · 치료 {MEDICS[cur][2]}%"
                if cur in MEDICS else "`현재 의료진` 없음") if team else _NO_CLUB
        e = ui.card("🩺 의료진", head + "\n\n" + "\n".join(lines), ui.INFO, user, "🏟️ 구단")
        e.set_footer(text="/의료진 영입:<의료진> · 영입비는 한 번만 · 바꾸면 새 영입비 · 부상은 유망주만 당해요")
        await interaction.followup.send(embed=e)


class RematchView(discord.ui.View):
    """친선경기 결과 아래 [🔁 다시 붙기] — 경기를 건 사람만."""

    def __init__(self, cog: "Club", user, opp):
        super().__init__(timeout=300)
        self.cog, self.user, self.opp = cog, user, opp

    @discord.ui.button(label="다시 붙기", emoji="🔁", style=discord.ButtonStyle.primary)
    async def again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("🙅 경기를 건 사람만 누를 수 있어요.", ephemeral=True)
        await self.cog._friendly(interaction, self.user, self.opp)


async def setup(bot: commands.Bot):
    await bot.add_cog(Club(bot))
