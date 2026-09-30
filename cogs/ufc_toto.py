# cogs/ufc_toto.py — UFC 경기·배당 조회와 자동 정산. 목록·베팅·내역 화면은 /토토 · /내베팅 (cogs/toto.py)
import os
import time
import logging
from datetime import datetime, timezone

import aiohttp
import discord
from discord.ext import commands, tasks

from services.ufc_db import UfcDB
from services.economy_db import EconomyDB
from services.notifier import send_notify

log = logging.getLogger(__name__)

ODDS_API_KEY   = os.getenv("ODDS_API_KEY", "")
ODDS_API_URL   = "https://api.the-odds-api.com/v4/sports/mma_mixed_martial_arts/odds/"
SCORES_API_URL = "https://api.the-odds-api.com/v4/sports/mma_mixed_martial_arts/scores/"
MMA_COLOR      = 0xE8003D


FIGHTS_CACHE_TTL = 300  # 초 — 짧은 시간 내 중복 호출로 크레딧 낭비 방지
_fights_cache: dict = {"data": [], "ts": 0.0}


async def _fetch_fights() -> list[dict]:
    now = time.monotonic()
    if now - _fights_cache["ts"] < FIGHTS_CACHE_TTL:
        return _fights_cache["data"]

    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "h2h",
        "oddsFormat": "decimal",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(ODDS_API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                log.warning(f"[UFC] odds API 응답 오류: {resp.status} {await resp.text()}")
                return _fights_cache["data"]
            data = await resp.json()

    fights = []
    for event in data:
        home = event["home_team"]
        away = event["away_team"]
        home_odds = away_odds = None

        # 첫 북메이커에 라인이 없으면 다음 북메이커도 확인
        for bm in event.get("bookmakers", []):
            h2h = next((m for m in bm.get("markets", []) if m.get("key") == "h2h"), None)
            if not h2h:
                continue
            outcomes = h2h.get("outcomes", [])
            home_odds = home_odds or next((o["price"] for o in outcomes if o["name"] == home), None)
            away_odds = away_odds or next((o["price"] for o in outcomes if o["name"] == away), None)
            if home_odds and away_odds:
                break

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

    _fights_cache["data"] = fights
    _fights_cache["ts"]   = now
    return fights


async def upcoming_fights() -> list[dict]:
    """베팅 가능한(시작 전) UFC 경기. API 키가 없거나 조회에 실패하면 빈 목록."""
    if not ODDS_API_KEY:
        return []
    try:
        return [f for f in await _fetch_fights() if not fight_started(f)]
    except Exception as e:
        log.warning(f"[UFC] 경기 조회 실패: {e}")
        return []


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


# ── 베팅 공통 로직 (/토토 에서 사용) ──────────────────────────────────────────
def fight_started(fight: dict) -> bool:
    return datetime.fromisoformat(fight["commence_time"].replace("Z", "+00:00")) <= datetime.now(timezone.utc)


async def place_ufc_bet(user_id: int, fight: dict, fighter: str, odds: float, amount: int,
                        eco: EconomyDB, db: UfcDB) -> str | None:
    """UFC 베팅. 실패하면 이유를, 성공하면 None 을 돌려준다."""
    if amount <= 0:
        return "금액은 1 이상이어야 합니다."
    if fight_started(fight):
        return "이미 시작된 경기입니다."
    bal = await eco.get_balance(user_id)
    if bal < amount:
        return f"잔액 부족 (현재: **{bal:,}원**)"
    existing = await db.get_bet(fight["event_id"], user_id)
    if existing:
        return f"이미 이 경기에 **{existing['fighter']}** ({existing['amount']:,}원)으로 베팅했습니다."

    await eco.add_balance(user_id, -amount)
    if not await db.place_bet(fight["event_id"], fight["match_id"], user_id, fighter, amount, odds):
        await eco.add_balance(user_id, amount)
        return "베팅 등록 실패 (중복)"
    return None


# ── Cog: 자동 정산만 담당 ─────────────────────────────────────────────────────
class UfcToto(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db  = UfcDB()
        self.eco = EconomyDB()
        self._auto_settle_poll.start()

    def cog_unload(self):
        self._auto_settle_poll.cancel()

    @tasks.loop(minutes=30)
    async def _auto_settle_poll(self):
        if not ODDS_API_KEY:
            return
        if not await self.db.has_unsettled_bets():
            return  # 베팅이 없으면 API 호출 자체를 건너뛰어 크레딧 절약
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
