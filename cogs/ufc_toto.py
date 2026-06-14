# cogs/ufc_toto.py
import os
import logging
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from services.ufc_db import UfcDB
from services.economy_db import EconomyDB
from services.notifier import send_notify
from auth import OWNER_ID

log = logging.getLogger(__name__)

ODDS_API_KEY   = os.getenv("ODDS_API_KEY", "")
ODDS_API_URL   = "https://api.the-odds-api.com/v4/sports/mma_mixed_martial_arts/odds/"
SCORES_API_URL = "https://api.the-odds-api.com/v4/sports/mma_mixed_martial_arts/scores/"
MMA_COLOR      = 0xE8003D



def _label(odds: float) -> str:
    return f"{odds:.2f}x"


async def _fetch_fights() -> list[dict]:
    """Odds API에서 MMA 경기 목록 + h2h 배당 조회."""
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "h2h",
        "oddsFormat": "decimal",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(ODDS_API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()

    fights = []
    for event in data:
        bookmakers = event.get("bookmakers", [])
        if not bookmakers:
            continue
        outcomes = bookmakers[0]["markets"][0]["outcomes"]
        home = event["home_team"]
        away = event["away_team"]
        home_odds = next((o["price"] for o in outcomes if o["name"] == home), None)
        away_odds = next((o["price"] for o in outcomes if o["name"] == away), None)
        if not home_odds or not away_odds:
            continue
        fights.append({
            "event_id":     event["id"],
            "match_id":     f"{home}|{away}",
            "home":         home,
            "away":         away,
            "home_odds":    home_odds,
            "away_odds":    away_odds,
            "commence_time": event["commence_time"],
        })
    return fights


async def _fetch_scores() -> list[dict]:
    """완료된 경기 스코어 조회 (최근 3일)."""
    params = {
        "apiKey":   ODDS_API_KEY,
        "daysFrom": "3",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(SCORES_API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            return await resp.json()


def _determine_winner(event: dict) -> str | None:
    """scores 배열에서 승자를 판별. 판별 불가 시 None."""
    scores = event.get("scores")
    if not scores:
        return None
    try:
        parsed = [(s["name"], s["score"]) for s in scores]
        # "W"/"L" 형식
        for name, score in parsed:
            if str(score).upper() == "W":
                return name
        # 숫자 형식 — 높은 쪽이 승자
        numeric = [(name, float(score)) for name, score in parsed]
        return max(numeric, key=lambda x: x[1])[0]
    except Exception:
        return None


class UfcToto(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db  = UfcDB()
        self.eco = EconomyDB()
        self._auto_settle_poll.start()

    def cog_unload(self):
        self._auto_settle_poll.cancel()

    # ── 경기 목록 ─────────────────────────────────────────────────────────
    @app_commands.command(name="ufc토토", description="현재 베팅 가능한 UFC 경기 목록을 불러옵니다.")
    async def ufc_list(self, interaction: discord.Interaction):
        await interaction.response.defer()
        fights = await _fetch_fights()
        if not fights:
            return await interaction.followup.send("❌ 현재 불러올 수 있는 UFC 경기가 없습니다.")

        embed = discord.Embed(title="🥊 UFC 베팅 목록", color=MMA_COLOR)
        now = datetime.now(timezone.utc)

        for f in fights[:10]:
            dt = datetime.fromisoformat(f["commence_time"].replace("Z", "+00:00"))
            ts = f"<t:{int(dt.timestamp())}:f>"
            status = "⏳" if dt > now else "🔴 시작됨"
            embed.add_field(
                name=f"{status} {f['home']} vs {f['away']}",
                value=(
                    f"📅 {ts}\n"
                    f"**{f['home']}** {_label(f['home_odds'])} | "
                    f"**{f['away']}** {_label(f['away_odds'])}"
                ),
                inline=False,
            )

        embed.set_footer(text="베팅: /ufc베팅 | 배당은 실시간 변동될 수 있음")
        await interaction.followup.send(embed=embed)

    # ── 베팅 ──────────────────────────────────────────────────────────────
    @app_commands.command(name="ufc베팅", description="UFC 경기에 베팅합니다.")
    @app_commands.describe(파이터="베팅할 파이터 이름 (정확하게 입력)", 금액="베팅 금액")
    async def ufc_bet(self, interaction: discord.Interaction, 파이터: str, 금액: int):
        await interaction.response.defer(ephemeral=True)

        if 금액 <= 0:
            return await interaction.followup.send("❌ 금액은 1 이상이어야 합니다.")

        bal = await self.eco.get_balance(interaction.user.id)
        if bal < 금액:
            return await interaction.followup.send(f"❌ 잔액 부족 (현재: **{bal:,}원**)")

        fights = await _fetch_fights()
        target_fight = None
        target_odds  = None
        for f in fights:
            if 파이터.lower() == f["home"].lower():
                target_fight = f
                target_odds  = f["home_odds"]
                break
            if 파이터.lower() == f["away"].lower():
                target_fight = f
                target_odds  = f["away_odds"]
                break

        if not target_fight:
            names = "\n".join(f"• {f['home']} / {f['away']}" for f in fights[:8])
            return await interaction.followup.send(
                f"❌ **{파이터}**를 찾을 수 없습니다.\n현재 경기:\n{names}"
            )

        existing = await self.db.get_bet(target_fight["event_id"], interaction.user.id)
        if existing:
            return await interaction.followup.send(
                f"❌ 이미 이 경기에 **{existing['fighter']}** ({existing['amount']:,}원)으로 베팅했습니다."
            )

        await self.eco.add_balance(interaction.user.id, -금액)
        ok = await self.db.place_bet(
            target_fight["event_id"], target_fight["match_id"],
            interaction.user.id, 파이터, 금액, target_odds
        )
        if not ok:
            await self.eco.add_balance(interaction.user.id, 금액)  # 롤백
            return await interaction.followup.send("❌ 베팅 등록 실패 (중복)")

        opponent = target_fight["away"] if 파이터.lower() == target_fight["home"].lower() else target_fight["home"]
        payout   = int(금액 * target_odds)
        embed = discord.Embed(title="✅ UFC 베팅 완료", color=MMA_COLOR)
        embed.add_field(name="경기", value=f"{target_fight['home']} vs {target_fight['away']}", inline=False)
        embed.add_field(name="내 픽", value=f"**{파이터}**", inline=True)
        embed.add_field(name="배당", value=_label(target_odds), inline=True)
        embed.add_field(name="베팅", value=f"{금액:,}원", inline=True)
        embed.add_field(name="당첨 시 수령", value=f"**{payout:,}원**", inline=True)
        embed.set_footer(text=f"상대 파이터: {opponent}")
        await interaction.followup.send(embed=embed)

    # ── 내 베팅 확인 ──────────────────────────────────────────────────────
    @app_commands.command(name="ufc내베팅", description="내 UFC 베팅 현황을 확인합니다.")
    @app_commands.describe(파이터="확인할 파이터 이름")
    async def ufc_my_bet(self, interaction: discord.Interaction, 파이터: str):
        await interaction.response.defer(ephemeral=True)
        fights = await _fetch_fights()
        for f in fights:
            if 파이터.lower() in (f["home"].lower(), f["away"].lower()):
                row = await self.db.get_bet(f["event_id"], interaction.user.id)
                if row:
                    payout = int(row["amount"] * row["odds"])
                    return await interaction.followup.send(
                        f"🥊 **{f['home']} vs {f['away']}**\n"
                        f"내 픽: **{row['fighter']}** / 배당: {row['odds']:.2f}x\n"
                        f"베팅: {row['amount']:,}원 → 당첨 시 **{payout:,}원**"
                    )
                return await interaction.followup.send("이 경기에 베팅 내역이 없습니다.")
        await interaction.followup.send(f"❌ **{파이터}** 경기를 찾을 수 없습니다.")

    # ── 자동 정산 백그라운드 태스크 ──────────────────────────────────────
    @tasks.loop(minutes=10)
    async def _auto_settle_poll(self):
        if not ODDS_API_KEY:
            return
        try:
            scores = await _fetch_scores()
        except Exception as e:
            log.warning(f"[UFC] 스코어 조회 실패: {e}")
            return

        for event in scores:
            if not event.get("completed"):
                continue

            event_id = event["id"]
            already  = await self.db.is_settled(event_id)
            if already:
                continue

            winner = _determine_winner(event)
            if winner is None:
                log.warning(f"[UFC] {event_id} 승자 판별 실패: {event.get('scores')}")
                continue

            results = await self.db.settle(event_id, winner)
            if not results:
                continue

            home = event.get("home_team", "?")
            away = event.get("away_team", "?")
            log.info(f"[UFC] 자동 정산: {home} vs {away} → 승자 {winner}")

            for r in results:
                if r["won"]:
                    await self.eco.add_balance(r["user_id"], r["payout"])

                embed = discord.Embed(
                    title="🥊 UFC 베팅 정산",
                    description=f"**{home} vs {away}**\n승자: **{winner}**",
                    color=MMA_COLOR,
                )
                if r["won"]:
                    embed.add_field(name="결과", value=f"✅ 당첨!", inline=True)
                    embed.add_field(name="수령액", value=f"**+{r['payout']:,}원**", inline=True)
                else:
                    embed.add_field(name="결과", value="❌ 낙첨", inline=True)
                    embed.add_field(name="손실", value=f"{r['amount']:,}원", inline=True)
                embed.add_field(name="내 픽", value=f"{r['fighter']} ({r['odds']:.2f}x)", inline=False)
                await send_notify(self.bot, self.eco, r["user_id"], "UFC_결과", embed)

    @_auto_settle_poll.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(UfcToto(bot))
