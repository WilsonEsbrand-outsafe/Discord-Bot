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

log = logging.getLogger(__name__)

ODDS_API_KEY   = os.getenv("ODDS_API_KEY", "")
ODDS_API_URL   = "https://api.the-odds-api.com/v4/sports/mma_mixed_martial_arts/odds/"
SCORES_API_URL = "https://api.the-odds-api.com/v4/sports/mma_mixed_martial_arts/scores/"
MMA_COLOR      = 0xE8003D


def _label(odds: float) -> str:
    return f"{odds:.2f}x"


async def _fetch_fights() -> list[dict]:
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
            "event_id":      event["id"],
            "match_id":      f"{home}|{away}",
            "home":          home,
            "away":          away,
            "home_odds":     home_odds,
            "away_odds":     away_odds,
            "commence_time": event["commence_time"],
        })
    return fights


async def _fetch_scores() -> list[dict]:
    params = {"apiKey": ODDS_API_KEY, "daysFrom": "3"}
    async with aiohttp.ClientSession() as session:
        async with session.get(SCORES_API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            return await resp.json()


def _determine_winner(event: dict) -> str | None:
    scores = event.get("scores")
    if not scores:
        return None
    try:
        parsed = [(s["name"], s["score"]) for s in scores]
        for name, score in parsed:
            if str(score).upper() == "W":
                return name
        numeric = [(name, float(score)) for name, score in parsed]
        return max(numeric, key=lambda x: x[1])[0]
    except Exception:
        return None


# ── 베팅 공통 로직 ────────────────────────────────────────────────────────────
async def _do_bet(
    interaction: discord.Interaction,
    fight: dict,
    fighter: str,
    odds: float,
    amount: int,
    eco: EconomyDB,
    db: UfcDB,
    ephemeral: bool = True,
):
    if amount <= 0:
        return await interaction.followup.send("❌ 금액은 1 이상이어야 합니다.", ephemeral=True)

    bal = await eco.get_balance(interaction.user.id)
    if bal < amount:
        return await interaction.followup.send(f"❌ 잔액 부족 (현재: **{bal:,}원**)", ephemeral=True)

    existing = await db.get_bet(fight["event_id"], interaction.user.id)
    if existing:
        return await interaction.followup.send(
            f"❌ 이미 이 경기에 **{existing['fighter']}** ({existing['amount']:,}원)으로 베팅했습니다.",
            ephemeral=True,
        )

    await eco.add_balance(interaction.user.id, -amount)
    ok = await db.place_bet(fight["event_id"], fight["match_id"], interaction.user.id, fighter, amount, odds)
    if not ok:
        await eco.add_balance(interaction.user.id, amount)
        return await interaction.followup.send("❌ 베팅 등록 실패 (중복)", ephemeral=True)

    opponent = fight["away"] if fighter.lower() == fight["home"].lower() else fight["home"]
    payout   = int(amount * odds)
    embed = discord.Embed(title="✅ UFC 베팅 완료", color=MMA_COLOR)
    embed.add_field(name="경기",       value=f"{fight['home']} vs {fight['away']}", inline=False)
    embed.add_field(name="내 픽",      value=f"**{fighter}**",  inline=True)
    embed.add_field(name="배당",       value=_label(odds),       inline=True)
    embed.add_field(name="베팅",       value=f"{amount:,}원",    inline=True)
    embed.add_field(name="당첨 시 수령", value=f"**{payout:,}원**", inline=True)
    embed.set_footer(text=f"상대: {opponent}")
    await interaction.followup.send(embed=embed, ephemeral=ephemeral)


# ── UI 컴포넌트 ───────────────────────────────────────────────────────────────
class BetAmountModal(discord.ui.Modal):
    amount_input = discord.ui.TextInput(
        label="베팅 금액",
        placeholder="예: 10000",
        max_length=12,
    )

    def __init__(self, fight: dict, fighter: str, odds: float, eco: EconomyDB, db: UfcDB):
        super().__init__(title=f"🥊 {fighter[:40]} 베팅")
        self.fight   = fight
        self.fighter = fighter
        self.odds    = odds
        self.eco     = eco
        self.db      = db

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.amount_input.value.replace(",", "").replace("원", "").strip()
        try:
            amount = int(raw)
        except ValueError:
            return await interaction.response.send_message("❌ 숫자를 입력해주세요.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await _do_bet(interaction, self.fight, self.fighter, self.odds, amount, self.eco, self.db)


class FighterButton(discord.ui.Button):
    def __init__(self, fight: dict, fighter: str, odds: float, row: int, eco: EconomyDB, db: UfcDB):
        super().__init__(
            label=f"{fighter} ({_label(odds)})",
            style=discord.ButtonStyle.primary if fighter == fight["home"] else discord.ButtonStyle.danger,
            row=row,
        )
        self.fight   = fight
        self.fighter = fighter
        self.odds    = odds
        self.eco     = eco
        self.db      = db

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            BetAmountModal(self.fight, self.fighter, self.odds, self.eco, self.db)
        )


class FightListView(discord.ui.View):
    def __init__(self, fights: list[dict], eco: EconomyDB, db: UfcDB):
        super().__init__(timeout=180)
        now = datetime.now(timezone.utc)
        for i, f in enumerate(fights[:5]):
            dt = datetime.fromisoformat(f["commence_time"].replace("Z", "+00:00"))
            if dt <= now:
                continue
            self.add_item(FighterButton(f, f["home"], f["home_odds"], row=i, eco=eco, db=db))
            self.add_item(FighterButton(f, f["away"], f["away_odds"], row=i, eco=eco, db=db))


# ── Cog ───────────────────────────────────────────────────────────────────────
class UfcToto(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db  = UfcDB()
        self.eco = EconomyDB()
        self._auto_settle_poll.start()

    def cog_unload(self):
        self._auto_settle_poll.cancel()

    # ── 경기 목록 + 버튼 베팅 ─────────────────────────────────────────────────
    @app_commands.command(name="ufc토토", description="UFC 경기 목록과 실시간 배당을 확인하고 바로 베팅합니다.")
    async def ufc_list(self, interaction: discord.Interaction):
        await interaction.response.defer()
        fights = await _fetch_fights()
        if not fights:
            return await interaction.followup.send("❌ 현재 불러올 수 있는 UFC 경기가 없습니다.")

        now   = datetime.now(timezone.utc)
        embed = discord.Embed(title="🥊 UFC 베팅 목록", color=MMA_COLOR)

        for f in fights[:10]:
            dt     = datetime.fromisoformat(f["commence_time"].replace("Z", "+00:00"))
            ts     = f"<t:{int(dt.timestamp())}:f>"
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

        embed.set_footer(text="버튼 클릭으로 바로 베팅 | 최대 5경기 버튼 표시")
        view = FightListView(fights, self.eco, self.db)
        await interaction.followup.send(embed=embed, view=view)

    # ── 자동완성 베팅 (텍스트 입력 방식) ─────────────────────────────────────
    async def _fighter_ac(self, interaction: discord.Interaction, current: str):
        try:
            fights = await _fetch_fights()
        except Exception:
            return []
        now = datetime.now(timezone.utc)
        choices = []
        for f in fights:
            dt = datetime.fromisoformat(f["commence_time"].replace("Z", "+00:00"))
            if dt <= now:
                continue
            for name in (f["home"], f["away"]):
                if current.lower() in name.lower():
                    choices.append(app_commands.Choice(name=name, value=name))
        return choices[:25]

    @app_commands.command(name="ufc베팅", description="파이터 이름으로 UFC 베팅합니다.")
    @app_commands.describe(파이터="베팅할 파이터 (자동완성 지원)", 금액="베팅 금액")
    @app_commands.autocomplete(파이터=_fighter_ac)
    async def ufc_bet(self, interaction: discord.Interaction, 파이터: str, 금액: int):
        await interaction.response.defer(ephemeral=True)

        fights = await _fetch_fights()
        target_fight = None
        target_odds  = None
        for f in fights:
            if 파이터.lower() == f["home"].lower():
                target_fight, target_odds = f, f["home_odds"]
                break
            if 파이터.lower() == f["away"].lower():
                target_fight, target_odds = f, f["away_odds"]
                break

        if not target_fight:
            names = "\n".join(f"• {f['home']} / {f['away']}" for f in fights[:8])
            return await interaction.followup.send(
                f"❌ **{파이터}**를 찾을 수 없습니다.\n현재 경기:\n{names}"
            )

        await _do_bet(interaction, target_fight, 파이터, target_odds, 금액, self.eco, self.db)

    # ── 내 베팅 목록 ──────────────────────────────────────────────────────────
    @app_commands.command(name="ufc내베팅", description="내 UFC 베팅 현황을 확인합니다.")
    async def ufc_my_bet(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rows = await self.db.list_bets_for_user(interaction.user.id)
        if not rows:
            return await interaction.followup.send("현재 진행 중인 UFC 베팅이 없습니다.")

        embed = discord.Embed(title="🥊 내 UFC 베팅 현황", color=MMA_COLOR)
        for row in rows:
            home, away = row["match_id"].split("|", 1)
            payout = int(row["amount"] * row["odds"])
            embed.add_field(
                name=f"{home} vs {away}",
                value=f"내 픽: **{row['fighter']}** ({row['odds']:.2f}x)\n{row['amount']:,}원 → 당첨 시 **{payout:,}원**",
                inline=False,
            )
        await interaction.followup.send(embed=embed)

    # ── 자동 정산 ─────────────────────────────────────────────────────────────
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
            if await self.db.is_settled(event_id):
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
                    embed.add_field(name="결과",   value="✅ 당첨!",              inline=True)
                    embed.add_field(name="수령액", value=f"**+{r['payout']:,}원**", inline=True)
                else:
                    embed.add_field(name="결과", value="❌ 낙첨",           inline=True)
                    embed.add_field(name="손실", value=f"{r['amount']:,}원", inline=True)
                embed.add_field(name="내 픽", value=f"{r['fighter']} ({r['odds']:.2f}x)", inline=False)
                await send_notify(self.bot, self.eco, r["user_id"], "UFC_결과", embed)

    @_auto_settle_poll.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(UfcToto(bot))
