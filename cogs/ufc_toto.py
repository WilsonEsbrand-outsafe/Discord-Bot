# cogs/ufc_toto.py — UFC 경기·배당 조회와 자동 정산. 목록·베팅·내역 화면은 /토토 · /내베팅 (cogs/toto.py)
import os
import re
import time
import logging
import unicodedata
from datetime import datetime, timedelta, timezone

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


# 2.9.1: Odds API 의 MMA 는 UFC · PFL · ONE 등이 섞여 온다 (단체 이름이 없다). ESPN 의 UFC 대진표(무료 · 키 없음)에
# 나온 선수가 뛰는 경기만 남긴다. 대진표를 못 받아 오면 마지막으로 받은 명단을 쓰고, 한 번도 못 받았으면 아무 경기도 안 보인다.
UFC_CARD_URL = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard"
UFC_CARD_DAYS = 30
UFC_CARD_TTL = 6 * 3600
UFC_SETTLE_DAYS = 30   # 3.2.1: 정산 때 ESPN 결과를 며칠 전까지 본다
_ufc_cache: dict = {"names": set(), "rank": {}, "pairs": set(), "ts": 0.0}   # rank: 이름 키 → 대진표 순서 (클수록 메인이벤트 쪽)
# 3.2.1: pairs = 대진표에 같이 잡힌 두 선수의 이름 키 쌍. 한 선수만 UFC 대진에 있어도 통과시키던 탓에
# Odds API 에 미리 올라온 (아직 잡히지 않은 · 다른 날 · 다른 단체) 경기가 /토토 에 섞여 나왔다.


def _norm(name: str) -> str:
    """'José Aldo Jr.' → 'jose aldo' — 악센트 · 점 · 대소문자 · Jr 차이를 없앤다."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z ]+", " ", s.replace("-", " "))
    return " ".join(w for w in s.split() if w not in ("jr", "sr", "ii", "iii"))


def _name_keys(name: str) -> set[str]:
    """이름 하나 → 비교용 키 (전체 이름 · '성 + 이름 첫 글자')."""
    n = _norm(name).split()
    if not n:
        return set()
    return {" ".join(n), f"{n[-1]} {n[0][0]}"}


def card_rank(fight: dict, rank: dict[str, int]) -> int:
    return max((rank.get(k, -1) for side in ("home", "away") for k in _name_keys(fight[side])), default=-1)


def _kst_day(fight: dict) -> str:
    return (datetime.fromisoformat(fight["commence_time"].replace("Z", "+00:00")) + timedelta(hours=9)).date().isoformat()


def is_ufc_fight(fight: dict, ufc_names: set[str]) -> bool:
    return any(_name_keys(fight[k]) & ufc_names for k in ("home", "away"))


def _pair_keys(a: str, b: str) -> set[frozenset]:
    return {frozenset((x, y)) for x in _name_keys(a) for y in _name_keys(b)}


def is_card_fight(fight: dict, pairs: set[frozenset]) -> bool:
    """3.2.1: 두 선수가 ESPN 대진표의 같은 경기에 잡혀 있어야 한다."""
    return bool(_pair_keys(fight["home"], fight["away"]) & pairs)


def _athlete_names(c: dict) -> list[str]:
    a = c.get("athlete") or {}
    return [n for n in (a.get("displayName"), a.get("fullName"), c.get("displayName")) if n]


async def _fetch_espn(days_back: int, days_ahead: int) -> list[dict] | None:
    today = datetime.now(timezone.utc)
    params = {"dates": f"{(today - timedelta(days=days_back)):%Y%m%d}-{(today + timedelta(days=days_ahead)):%Y%m%d}", "limit": 100}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(UFC_CARD_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    log.warning(f"[UFC] ESPN 대진표 응답 오류: {resp.status}")
                    return None
                return (await resp.json()).get("events", [])
    except Exception as e:
        log.warning(f"[UFC] ESPN 대진표 조회 실패: {e}")
        return None


async def _fetch_ufc_names() -> set[str]:
    now = time.monotonic()
    if _ufc_cache["ts"] and now - _ufc_cache["ts"] < UFC_CARD_TTL:
        return _ufc_cache["names"]
    events = await _fetch_espn(1, UFC_CARD_DAYS)
    if events is None:
        return _ufc_cache["names"]
    names: set[str] = set()
    rank: dict[str, int] = {}
    pairs: set[frozenset] = set()
    for ev in events:
        for i, comp in enumerate(ev.get("competitions", [])):   # ESPN 은 언더카드 → 메인이벤트 순서
            sides = [set().union(*(_name_keys(n) for n in _athlete_names(c))) for c in comp.get("competitors", [])]
            for keys in sides:
                names |= keys
                rank.update(dict.fromkeys(keys, i))
            if len(sides) == 2:
                pairs |= {frozenset((x, y)) for x in sides[0] for y in sides[1]}
    if names:
        _ufc_cache.update(names=names, rank=rank, pairs=pairs, ts=now)
    return _ufc_cache["names"]


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


async def upcoming_fights(ufc_only: bool = True, pairs: bool = False) -> list[dict]:
    """베팅 가능한(시작 전) UFC 경기 — 다른 MMA 단체 경기는 뺀다 (ufc_only=False 면 2.9.1 전처럼 MMA 전부).
    pairs=True(3.2.1) 면 두 선수가 같은 대진으로 잡힌 경기만. API 키가 없거나 조회에 실패하면 빈 목록."""
    if not ODDS_API_KEY:
        return []
    try:
        fights = [f for f in await _fetch_fights() if not fight_started(f)]
        if not ufc_only:
            return fights
        ufc = await _fetch_ufc_names() if fights else set()
        fights = [f for f in fights if (is_card_fight(f, _ufc_cache["pairs"]) if pairs else is_ufc_fight(f, ufc))]
        return sorted(fights, key=lambda f: (_kst_day(f), -card_rank(f, _ufc_cache["rank"]), f["commence_time"]))   # 같은 날은 메인이벤트부터
    except Exception as e:
        log.warning(f"[UFC] 경기 조회 실패: {e}")
        return []


async def _fetch_scores() -> list[dict]:
    params = {"apiKey": ODDS_API_KEY, "daysFrom": "3"}
    async with aiohttp.ClientSession() as session:
        async with session.get(SCORES_API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                log.warning(f"[UFC] 스코어 API 응답 오류: {resp.status}")   # 3.2.1: 조용히 빈 목록을 돌려줘 정산이 왜 안 되는지 안 보였다
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


def espn_results(events: list[dict]) -> list[dict]:
    """3.2.1: ESPN 대진표에서 승자가 나온 경기 → [{"pairs", "winner"(이름 키)}]. 무승부 · 무효는 건너뛴다."""
    out = []
    for ev in events:
        for comp in ev.get("competitions", []):
            st = (comp.get("status") or {}).get("type") or {}
            cs = comp.get("competitors", [])
            if not st.get("completed") or len(cs) != 2:
                continue
            sides = [set().union(*(_name_keys(n) for n in _athlete_names(c))) for c in cs]
            win = next((k for c, k in zip(cs, sides) if c.get("winner")), None)
            if win:
                out.append({"pairs": {frozenset((x, y)) for x in sides[0] for y in sides[1]}, "winner": win})
    return out


def match_result(match_id: str, fighter: str, results: list[dict]) -> str | None:
    """베팅 하나('홈|원정', 고른 선수) → 'won' · 'lost' · None(아직 결과 없음)."""
    home, _, away = match_id.partition("|")
    keys = _pair_keys(home, away)
    for r in results:
        if keys & r["pairs"]:
            return "won" if _name_keys(fighter) & r["winner"] else "lost"
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
    try:
        ok = await db.place_bet(fight["event_id"], fight["match_id"], user_id, fighter, amount, odds)
    except Exception:
        await eco.add_balance(user_id, amount)   # 2.10.2: 등록이 터져도 돈은 돌려준다
        raise
    if not ok:
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
            scores = []

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

            home = event.get("home_team", "?")
            away = event.get("away_team", "?")
            results = await self.db.settle(event_id, winner)
            results += await self.db.settle(f"{home}|{away}", winner)   # 2.10.2: 예전 표에서 옮겨 온 베팅은 'A|B' 가 경기 키
            if not results:
                continue

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
        await self._settle_from_espn()

    async def _settle_from_espn(self):
        """3.2.1: Odds API 결과가 비거나 3일이 지나 사라진 경기는 ESPN 결과로 정산한다."""
        bets = await self.db.list_unsettled()
        if not bets:
            return
        events = await _fetch_espn(UFC_SETTLE_DAYS, 1)
        if events is None:
            return
        results = espn_results(events)
        for b in bets:
            res = match_result(b["match_id"], b["fighter"], results)
            if res is None:
                continue
            payout = int(b["amount"] * b["odds"]) if res == "won" else 0
            if not await self.db.settle_bet(b["id"], res == "won"):
                continue   # 그 사이 다른 정산이 먼저 했다
            if payout:
                await self.eco.add_balance(b["user_id"], payout)
            home, _, away = b["match_id"].partition("|")
            log.info(f"[UFC] ESPN 정산: {home} vs {away} · {b['fighter']} → {res}")
            embed = discord.Embed(title="🥊 UFC 베팅 정산", description=f"**{home} vs {away}**", color=MMA_COLOR)
            if res == "won":
                embed.add_field(name="결과",   value="✅ 당첨!",            inline=True)
                embed.add_field(name="수령액", value=f"**+{payout:,}원**", inline=True)
            else:
                embed.add_field(name="결과", value="❌ 낙첨",             inline=True)
                embed.add_field(name="손실", value=f"{b['amount']:,}원", inline=True)
            embed.add_field(name="내 픽", value=f"{b['fighter']} ({b['odds']:.2f}x)", inline=False)
            await send_notify(self.bot, self.eco, b["user_id"], "UFC_결과", embed)

    @_auto_settle_poll.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(UfcToto(bot))
