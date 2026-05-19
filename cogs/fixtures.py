# cogs/fixtures.py
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone
from typing import Optional, List, Dict
import re
import pytz
import aiohttp

# ─────────────────────────────────────────────────────────
# 팀 한글명 / 별칭 / 커스텀 이모지 / 팀 컬러 (EPL 중심, 미존재 팀은 영문 그대로 표시)
TEAM_NAME_KO = {
    "Arsenal FC": "아스널",
    "Aston Villa FC": "아스톤 빌라",
    "AFC Bournemouth": "본머스",
    "Brentford FC": "브렌트포드",
    "Brighton & Hove Albion FC": "브라이튼",
    "Burnley FC": "번리",
    "Chelsea FC": "첼시",
    "Crystal Palace FC": "크리스탈 팰리스",
    "Everton FC": "에버턴",
    "Fulham FC": "풀럼",
    "Leeds United FC": "리즈 유나이티드",
    "Liverpool FC": "리버풀",
    "Manchester City FC": "맨체스터 시티",
    "Manchester United FC": "맨체스터 유나이티드",
    "Newcastle United FC": "뉴캐슬 유나이티드",
    "Nottingham Forest FC": "노팅엄 포레스트",
    "Sunderland AFC": "선덜랜드",
    "Tottenham Hotspur FC": "토트넘 홋스퍼",
    "West Ham United FC": "웨스트햄 유나이티드",
    "Wolverhampton Wanderers FC": "울버햄튼 원더러스",
}
TEAM_ALIASES = {
    "맨유": "Manchester United FC",
    "맨시티": "Manchester City FC",
    "첼시": "Chelsea FC",
    "토트넘": "Tottenham Hotspur FC",
    "아스날": "Arsenal FC",
    "리버풀": "Liverpool FC",
    "뉴캐슬": "Newcastle United FC",
    "웨스트햄": "West Ham United FC",
    "울버햄튼": "Wolverhampton Wanderers FC",
    "브라이튼": "Brighton & Hove Albion FC",
    "브렌트포드": "Brentford FC",
    "본머스": "AFC Bournemouth",
    "아스톤빌라": "Aston Villa FC",
    "노팅엄": "Nottingham Forest FC",
    "리즈": "Leeds United FC",
    "선덜랜드": "Sunderland AFC",
    "풀럼": "Fulham FC",
    "에버튼": "Everton FC",
    "번리": "Burnley FC",
}
TEAM_EMOJI_NAME = {
    "Arsenal FC": "ARS_Logo",
    "Aston Villa FC": "AVL_Logo",
    "AFC Bournemouth": "BOU_Logo",
    "Brentford FC": "BRE_Logo",
    "Brighton & Hove Albion FC": "BHA_Logo",
    "Burnley FC": "BUR_Logo",
    "Chelsea FC": "CHE_Logo",
    "Crystal Palace FC": "CRY_Logo",
    "Everton FC": "EVE_Logo",
    "Fulham FC": "FUL_Logo",
    "Leeds United FC": "LEE_Logo",
    "Liverpool FC": "LFC_Logo",
    "Manchester City FC": "MCI_Logo",
    "Manchester United FC": "MUN_Logo",
    "Newcastle United FC": "NEW_Logo",
    "Nottingham Forest FC": "NFO_Logo",
    "Sunderland AFC": "SUN_Logo",
    "Tottenham Hotspur FC": "TOT_Logo",
    "West Ham United FC": "WHU_Logo",
    "Wolverhampton Wanderers FC": "WOL_Logo",
}
TEAM_COLOR_HEX = {
    "Arsenal FC": "#EF0107",
    "Aston Villa FC": "#670E36",
    "AFC Bournemouth": "#DA291C",
    "Brentford FC": "#E30613",
    "Brighton & Hove Albion FC": "#0057B8",
    "Burnley FC": "#6C1D45",
    "Chelsea FC": "#034694",
    "Crystal Palace FC": "#1B458F",
    "Everton FC": "#003399",
    "Fulham FC": "#000000",
    "Leeds United FC": "#FFCD00",
    "Liverpool FC": "#C8102E",
    "Manchester City FC": "#6CABDD",
    "Manchester United FC": "#DA291C",
    "Newcastle United FC": "#231F20",
    "Nottingham Forest FC": "#DD0000",
    "Sunderland AFC": "#E2231A",
    "Tottenham Hotspur FC": "#001C58",
    "West Ham United FC": "#7A263A",
    "Wolverhampton Wanderers FC": "#FDB913",
}

PL_COLOR_HEX = "#3A225D"
KST = pytz.timezone("Asia/Seoul")
PL_CREST = "https://crests.football-data.org/PL.png"

# football-data.org v4 대회 코드
COMPETITIONS: Dict[str, str] = {
    "프리미어리그": "PL",
    "라리가": "PD",
    "챔피언스리그": "CL",
    "FA컵": "FAC",
    "EFL컵": "EFLC",
}

# ───────────────── 유틸 ─────────────────
def _norm(s: str) -> str:
    return "".join((s or "").lower().split())

def _is_team_match(query: str, team_en: str) -> bool:
    q = _norm(query)
    name_en = _norm(team_en)
    name_ko = _norm(TEAM_NAME_KO.get(team_en, ""))
    if q == name_en or q == name_ko:
        return True
    if q and (q in name_en or (name_ko and q in name_ko)):
        return True
    alias_target = TEAM_ALIASES.get((query or "").strip(), None)
    return bool(alias_target and alias_target == team_en)

def _resolve_custom_emoji(guild: Optional[discord.Guild], emoji_name: str) -> str:
    if not guild or not emoji_name:
        return ""
    for e in guild.emojis:
        if e.name == emoji_name:
            return f"<:{e.name}:{e.id}>"
    return ""

def _hex_to_color(hex_str: str) -> discord.Color:
    try:
        return discord.Color(int(hex_str.replace("#", ""), 16))
    except Exception:
        return discord.Color.blue()

def _pick_embed_color_for_team(team_en: str) -> discord.Color:
    hexv = TEAM_COLOR_HEX.get(team_en)
    return _hex_to_color(hexv) if hexv else _hex_to_color(PL_COLOR_HEX)

_SEASON_PATTERNS = (re.compile(r"^\d{2}-\d{2}$"), re.compile(r"^\d{4}-\d{4}$"))

def _validate_season(s: str) -> str:
    s = (s or "").strip()
    if any(p.fullmatch(s) for p in _SEASON_PATTERNS):
        return s
    raise ValueError("시즌 형식은 25-26 또는 2025-2026 이어야 합니다.")

def _compact_to_start_year(s: str) -> int:
    # "25-26" -> 2025, "2025-2026" -> 2025
    s = s.strip()
    if "-" in s:
        first = s.split("-", 1)[0]
    else:
        first = s
    if len(first) == 2 and first.isdigit():
        return 2000 + int(first)
    if len(first) == 4 and first.isdigit():
        return int(first)
    raise ValueError("시즌 형식이 올바르지 않습니다.")

def _utc_iso_to_kst_str(utc_iso: str) -> str:
    try:
        kst = datetime.fromisoformat(utc_iso.replace("Z", "+00:00")).astimezone(KST)
        return kst.strftime("%Y-%m-%d %H:%M KST")
    except Exception:
        return "시간 미정"

def _is_future_or_today(utc_iso: str) -> bool:
    try:
        dt_utc = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
        return dt_utc.date() >= datetime.now(timezone.utc).date()
    except Exception:
        return True

# ───────────────── Cog ─────────────────
class Fixtures(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self.api = None

    async def cog_load(self):
        from services.football_api import FootballAPI
        self.session = aiohttp.ClientSession()
        self.api = FootballAPI(self.session)
        print("🧩 Fixtures cog loaded (PL + UCL + FA + EFL / Slash)")

    async def cog_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()

    @app_commands.command(
        name="경기일정",
        description="예정된 축구 경기(프리미어리그, 챔피언스리그, FA컵, EFL컵)를 날짜순으로 보여줍니다."
    )
    @app_commands.describe(
        season="시즌: 25-26 또는 2025-2026 (기본: 25-26)",
        count="가져올 경기 수 1~10 (기본: 5, 모든 대회 합산)",
        team="팀(선택). 한국어/영어/별칭 가능 (예: 맨유, 토트넘, Liverpool)",
    )
    async def fixtures_slash(
        self,
        interaction: discord.Interaction,
        season: Optional[str] = "25-26",
        count: Optional[int] = 5,
        team: Optional[str] = None,
    ):
        await interaction.response.defer()

        # 입력 정리
        try:
            season = _validate_season(season or "25-26")
        except ValueError as e:
            return await interaction.followup.send(str(e))

        try:
            count = int(count or 5)
        except Exception:
            count = 5
        count = max(1, min(10, count))

        start_year = _compact_to_start_year(season)
        date_from = datetime.now(timezone.utc).date().isoformat()
        date_to = f"{start_year + 1}-06-30"

        # 대회별 일정 수집 (SCHEDULED + TIMED 병합, 일부 대회 실패해도 계속)
        try:
            all_matches: List[Dict] = []
            for comp_name, comp_code in COMPETITIONS.items():
                try:
                    scheduled = await self.api.competition_matches(
                        comp_code,
                        season_year=start_year,
                        status="SCHEDULED",
                        date_from=date_from,
                        date_to=date_to,
                        limit=40,
                    )
                    try:
                        timed = await self.api.competition_matches(
                            comp_code,
                            season_year=start_year,
                            status="TIMED",
                            date_from=date_from,
                            date_to=date_to,
                            limit=40,
                        )
                    except Exception:
                        timed = []
                except Exception:
                    # 이 대회는 권한/쿼터 문제 등으로 실패 → 다른 대회 계속 시도
                    continue

                by_id = {m["id"]: m for m in scheduled}
                for t in timed:
                    by_id.setdefault(t["id"], t)

                # 표시용 한글 대회명 주입
                for m in by_id.values():
                    if "competition" in m and isinstance(m["competition"], dict):
                        m["competition"]["name"] = comp_name
                    else:
                        m["competition"] = {"name": comp_name}

                all_matches.extend(by_id.values())

            # 필터링: 오늘 이후만, 날짜 오름차순
            all_matches = [m for m in all_matches if _is_future_or_today(m.get("utcDate") or "")]
            all_matches.sort(key=lambda m: m.get("utcDate") or "9999-12-31T23:59:59Z")
        except Exception:
            return await interaction.followup.send("API 호출 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요.")

        # 팀 필터 (선택)
        crest_for_thumbnail = None
        if team:
            filtered: List[Dict] = []
            for m in all_matches:
                home_en = (m.get("homeTeam") or {}).get("name", "")
                away_en = (m.get("awayTeam") or {}).get("name", "")
                def is_match(q): return _is_team_match(q, home_en) or _is_team_match(q, away_en)
                if is_match(team):
                    filtered.append(m)
                    if not crest_for_thumbnail:
                        if _is_team_match(team, home_en):
                            crest_for_thumbnail = (m.get("homeTeam") or {}).get("crest")
                        elif _is_team_match(team, away_en):
                            crest_for_thumbnail = (m.get("awayTeam") or {}).get("crest")
            all_matches = filtered

        if not all_matches:
            return await interaction.followup.send(
                f"`{season}` 시즌에서" + (f" `{team}` " if team else " ") + "예정 경기를 찾지 못했습니다."
            )

        # 모든 대회 합산에서 앞쪽 count개만 노출
        matches = all_matches[:count]

        # 임베드 색상: 팀 지정 시 해당 팀 컬러 우선
        embed_color = _hex_to_color(PL_COLOR_HEX)
        if team and matches:
            first = matches[0]
            home_en = (first.get("homeTeam") or {}).get("name", "")
            away_en = (first.get("awayTeam") or {}).get("name", "")
            target_en = home_en if _is_team_match(team, home_en) else (away_en if _is_team_match(team, away_en) else None)
            if target_en:
                embed_color = _pick_embed_color_for_team(target_en)

        # 본문 라인
        lines: List[str] = []
        for m in matches:
            home_en = (m.get("homeTeam") or {}).get("name", "")
            away_en = (m.get("awayTeam") or {}).get("name", "")
            home = TEAM_NAME_KO.get(home_en, home_en or "미정")
            away = TEAM_NAME_KO.get(away_en, away_en or "미정")

            eh = ea = ""
            if interaction.guild:
                eh = _resolve_custom_emoji(interaction.guild, TEAM_EMOJI_NAME.get(home_en, ""))
                ea = _resolve_custom_emoji(interaction.guild, TEAM_EMOJI_NAME.get(away_en, ""))

            comp = (m.get("competition") or {}).get("name") or "대회 미정"
            t_str = _utc_iso_to_kst_str(m.get("utcDate") or "")
            venue = m.get("venue") or ""
            tail = f" | {venue}" if venue else ""
            lines.append(f"- {eh} **{home}** vs {ea} **{away}** | {comp} | {t_str}{tail}")

        title = f"{season} 예정 경기 {len(matches)}경기 (PL/LL/UCL/FA/EFL)"
        if team:
            title += f" — {team}"

        embed = discord.Embed(title=title, description="\n".join(lines), color=embed_color)
        if crest_for_thumbnail:
            embed.set_thumbnail(url=crest_for_thumbnail)
        else:
            embed.set_author(name="축구 일정", icon_url=PL_CREST)

        await interaction.followup.send(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(Fixtures(bot))
