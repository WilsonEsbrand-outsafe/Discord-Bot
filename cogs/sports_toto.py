# cogs/sports_toto.py — 3.2.1: MLB · KBO · NBA 토토. 목록 · 베팅 화면은 /토토 · /내베팅 (cogs/toto.py).
# The Odds API 무료 플랜(월 500 크레딧)을 축구 · UFC 와 나눠 쓰므로 크레딧을 최대한 아낀다:
#   · 경기 목록(/events)은 무료 — 30분마다 이걸로 새 경기 · 시각 변경만 확인
#   · 배당(/odds, 1크레딧)은 배당 없는 경기가 생겼을 때 한 번 + 경기 당일 시작 몇 시간 전 한 번 (선발 · 부상 발표 반영)
#   · 결과는 MLB · NBA 는 ESPN 점수판(무료), KBO 만 Odds API /scores (2크레딧) — 끝났을 때쯤에만
#   · 남은 크레딧이 SAFE_CREDITS 아래면 배당 새로 받기를 멈추고 정산만 한다
# 배당은 표에 저장해 재시작(배포)해도 다시 받지 않는다. 승패 두 갈래 — 무승부 · 연기 · 취소는 전액 환불.
import os
import re
import time
import logging
import unicodedata
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord.ext import commands, tasks

from services.sports_db import SportsDB
from services.economy_db import EconomyDB
from services.notifier import send_notify

log = logging.getLogger(__name__)

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
ODDS_BASE = "https://api.the-odds-api.com/v4"
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

# key → (Odds API 종목, ESPN 결과 경로 · 없으면 Odds API 결과, 이모지 · 이름, 색)
SPORTS: dict[str, tuple[str, str | None, str, int]] = {
    "mlb": ("baseball_mlb",   "baseball/mlb",   "⚾ MLB", 0x0B3D91),
    "kbo": ("baseball_kbo",   None,             "⚾ KBO", 0x1F6FB2),
    "nba": ("basketball_nba", "basketball/nba", "🏀 NBA", 0xC9082A),
}
OFF: set[str] = {"kbo"}   # 이번 달 크레딧이 모자라 꺼 둔 종목 (결과 조회도 크레딧을 쓴다). 초기화되면 뺀다

GAMES_AHEAD = 2 * 86400      # 앞으로 이틀 경기만 받고 보여 준다
CLOSE_BEFORE = 30 * 60       # 시작 30분 전 베팅 마감
REFRESH_WITHIN = 3 * 3600    # 시작 3시간 안에 들어온 경기는
REFRESH_STALE = 6 * 3600     # 배당이 시작 6시간 전보다 오래됐으면 한 번 더 받는다
ODDS_GAP = 3 * 3600          # 한 종목 배당은 3시간에 한 번까지 (배당 없는 경기가 계속 남아도 크레딧이 새지 않게)
SAFE_CREDITS = 50            # 남은 크레딧이 이 아래면 배당 새로 받기 멈춤
SETTLE_AFTER = 3 * 3600      # 시작 뒤 이만큼 지나야 결과를 본다
SCORES_EVERY = 2 * 3600      # KBO 결과 조회(2크레딧) 간격
VOID_AFTER = 3 * 86400       # 이만큼 지나도 결과가 없으면 (연기 · 취소) 환불

_credits: dict = {"remaining": None}
_last_scores: dict[str, float] = {}
_last_odds: dict[str, int] = {}


def enabled_sports() -> list[str]:
    return [k for k in SPORTS if k not in OFF]


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", s).split())


def team_keys(name: str) -> set[str]:
    """'Los Angeles Clippers' · 'LA Clippers' 가 'clippers' 로 만난다 (전체 이름 · 마지막 단어 · 마지막 두 단어)."""
    n = _norm(name).split()
    return {" ".join(n), n[-1], " ".join(n[-2:])} if n else set()


def same_game(home: str, away: str, a: str, b: str) -> bool:
    k = team_keys
    return bool(k(home) & k(a) and k(away) & k(b)) or bool(k(home) & k(b) and k(away) & k(a))


def _ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


async def _get(url: str, params: dict) -> tuple[int, object]:
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            rem = resp.headers.get("x-requests-remaining")
            if rem is not None and "the-odds-api" in url:
                try:
                    _credits["remaining"] = int(float(rem))
                except ValueError:
                    pass
            if resp.status != 200:
                return resp.status, None
            return 200, await resp.json()


def low_credits() -> bool:
    return _credits["remaining"] is not None and _credits["remaining"] < SAFE_CREDITS


def parse_odds(data: list[dict]) -> list[tuple[str, float, float]]:
    """Odds API /odds → [(event_id, 홈 배당, 원정 배당)] (북메이커 평균, 소수 둘째 자리)."""
    out = []
    for ev in data:
        home, away = ev["home_team"], ev["away_team"]
        hs, as_ = [], []
        for bm in ev.get("bookmakers", []):
            h2h = next((m for m in bm.get("markets", []) if m.get("key") == "h2h"), None)
            prices = {o["name"]: o["price"] for o in (h2h or {}).get("outcomes", [])}
            if home in prices and away in prices:
                hs.append(prices[home])
                as_.append(prices[away])
        if hs:
            out.append((ev["id"], round(sum(hs) / len(hs), 2), round(sum(as_) / len(as_), 2)))
    return out


def needs_odds(games, now: int) -> bool:
    """배당을 (다시) 받아야 하나: 배당 없는 경기가 있거나, 곧 시작하는데 배당이 오래된 경기가 있으면."""
    for g in games:
        if g["commence_ts"] - CLOSE_BEFORE <= now:
            continue
        if g["home_odds"] is None:
            return True
        if g["commence_ts"] - now <= REFRESH_WITHIN and g["odds_ts"] < g["commence_ts"] - REFRESH_STALE:
            return True
    return False


def open_games(rows, now: float) -> list[dict]:
    """베팅 가능한 경기 (배당 있음 · 마감 전)."""
    return [{"event_id": r["event_id"], "home": r["home"], "away": r["away"], "ts": r["commence_ts"],
             "home_odds": r["home_odds"], "away_odds": r["away_odds"]}
            for r in rows if r["home_odds"] and r["away_odds"] and now < r["commence_ts"] - CLOSE_BEFORE]


async def place_sports_bet(user_id: int, sport: str, game: dict, pick: str, amount: int,
                           eco: EconomyDB, db: SportsDB) -> tuple[str | None, float | None]:
    """베팅 — 배당은 지금 표에 있는 값으로 고정. (실패 이유, 배당)."""
    if amount <= 0:
        return "금액은 1 이상이어야 합니다.", None
    g = await db.get_game(game["event_id"])
    if not g or time.time() >= g["commence_ts"] - CLOSE_BEFORE:
        return "베팅이 마감된 경기입니다. (시작 30분 전 마감)", None
    odds = g["home_odds"] if pick == g["home"] else g["away_odds"] if pick == g["away"] else None
    if not odds:
        return "배당이 없는 경기입니다.", None
    bal = await eco.get_balance(user_id)
    if bal < amount:
        return f"잔액 부족 (현재: **{bal:,}원**)", None
    existing = await db.get_bet(game["event_id"], user_id)
    if existing:
        return f"이미 이 경기에 **{existing['pick']}** ({existing['amount']:,}원)으로 베팅했습니다.", None
    await eco.add_balance(user_id, -amount)
    try:
        ok = await db.place_bet(sport, {**game, "ts": g["commence_ts"]}, user_id, pick, amount, odds)
    except Exception:
        await eco.add_balance(user_id, amount)
        raise
    if not ok:
        await eco.add_balance(user_id, amount)
        return "베팅 등록 실패 (중복)", None
    return None, odds


# ── 결과 ──────────────────────────────────────────────────────────────────────
def espn_finals(events: list[dict]) -> list[dict]:
    """ESPN 점수판 → 끝난 경기 [{home, away, ts, winner(팀 이름 · 무승부면 None)}]."""
    out = []
    for ev in events:
        for comp in ev.get("competitions", []):
            if not (((comp.get("status") or {}).get("type") or {}).get("completed")):
                continue
            cs = {c.get("homeAway"): c for c in comp.get("competitors", [])}
            if set(cs) != {"home", "away"}:
                continue
            name = lambda c: (c.get("team") or {}).get("displayName", "")   # noqa: E731
            win = next((name(c) for c in cs.values() if c.get("winner")), None)
            out.append({"home": name(cs["home"]), "away": name(cs["away"]),
                        "ts": _ts(comp.get("date") or ev.get("date")), "winner": win})
    return out


def odds_finals(scores: list[dict]) -> list[dict]:
    """Odds API /scores → 끝난 경기 (점수가 같으면 무승부)."""
    out = []
    for ev in scores:
        if not ev.get("completed") or not ev.get("scores"):
            continue
        try:
            pts = {s["name"]: float(s["score"]) for s in ev["scores"]}
            h, a = pts[ev["home_team"]], pts[ev["away_team"]]
        except (KeyError, TypeError, ValueError):
            continue
        out.append({"event_id": ev["id"], "home": ev["home_team"], "away": ev["away_team"],
                    "ts": _ts(ev["commence_time"]),
                    "winner": ev["home_team"] if h > a else ev["away_team"] if a > h else None})
    return out


def bet_result(bet, finals: list[dict]) -> str | None:
    """'won' · 'lost' · 'void'(무승부) · None(아직). 같은 경기 id, 또는 같은 두 팀이 12시간 안에 붙은 경기."""
    for f in finals:
        if f.get("event_id") == bet["event_id"] or \
                abs(f["ts"] - bet["commence_ts"]) <= 12 * 3600 and same_game(bet["home"], bet["away"], f["home"], f["away"]):
            if f["winner"] is None:
                return "void"
            return "won" if team_keys(bet["pick"]) & team_keys(f["winner"]) else "lost"
    return None


class SportsToto(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = SportsDB()
        self.eco = EconomyDB()
        self._poll.start()

    def cog_unload(self):
        self._poll.cancel()

    # ── 경기 · 배당 ──
    async def refresh(self, sport: str, now: int | None = None):
        """무료 경기 목록으로 표를 맞추고, 필요할 때만 배당(1크레딧)을 받는다."""
        if not ODDS_API_KEY:
            return
        now = now or int(time.time())
        odds_key = SPORTS[sport][0]
        status, data = await _get(f"{ODDS_BASE}/sports/{odds_key}/events", {"apiKey": ODDS_API_KEY})
        if status == 404:
            return   # 비시즌
        if status != 200:
            log.warning(f"[SPORTS] {sport} 경기 목록 응답 오류: {status}")
            return
        games = [{"event_id": e["id"], "home": e["home_team"], "away": e["away_team"], "ts": _ts(e["commence_time"])}
                 for e in data if now < _ts(e["commence_time"]) <= now + GAMES_AHEAD]
        if games:
            await self.db.upsert_games(sport, games)
        rows = await self.db.list_games(sport, now, now + GAMES_AHEAD)
        if not needs_odds(rows, now):
            return
        last = max([r["odds_ts"] or 0 for r in rows] + [_last_odds.get(sport, 0)])   # 표에 남은 시각 — 재시작해도 지킨다
        if now - last < ODDS_GAP:
            return
        if low_credits():
            log.warning(f"[SPORTS] {sport} 배당 건너뜀 — 남은 크레딧 {_credits['remaining']}")
            return
        status, data = await _get(f"{ODDS_BASE}/sports/{odds_key}/odds",
                                  {"apiKey": ODDS_API_KEY, "regions": "us", "markets": "h2h", "oddsFormat": "decimal"})
        if status != 200:
            log.warning(f"[SPORTS] {sport} 배당 응답 오류: {status}")
            return
        _last_odds[sport] = now
        odds = parse_odds(data)
        await self.db.set_odds(odds, now)
        log.info(f"[SPORTS] {sport} 배당 {len(odds)}경기 · 남은 크레딧 {_credits['remaining']}")

    async def games(self, sport: str) -> list[dict]:
        """/토토 목록 — 표만 읽는다 (API 호출 없음)."""
        now = int(time.time())
        return open_games(await self.db.list_games(sport, now, now + GAMES_AHEAD), now)

    # ── 정산 ──
    async def _finals(self, sport: str, oldest_ts: int) -> list[dict] | None:
        odds_key, espn, *_ = SPORTS[sport]
        if espn:
            start = datetime.fromtimestamp(oldest_ts, timezone.utc) - timedelta(days=1)
            status, data = await _get(f"{ESPN_BASE}/{espn}/scoreboard",
                                      {"dates": f"{start:%Y%m%d}-{datetime.now(timezone.utc):%Y%m%d}", "limit": 300})
            if status != 200:
                log.warning(f"[SPORTS] ESPN {sport} 점수판 응답 오류: {status}")
                return None
            return espn_finals(data.get("events", []))
        if not ODDS_API_KEY or time.monotonic() - _last_scores.get(sport, -1e9) < SCORES_EVERY:
            return None
        _last_scores[sport] = time.monotonic()
        status, data = await _get(f"{ODDS_BASE}/sports/{odds_key}/scores", {"apiKey": ODDS_API_KEY, "daysFrom": "3"})
        if status != 200:
            log.warning(f"[SPORTS] {sport} 결과 응답 오류: {status}")
            return None
        return odds_finals(data)

    async def settle(self, now: int | None = None):
        now = now or int(time.time())
        bets = await self.db.list_unsettled(now - SETTLE_AFTER)
        for sport in {b["sport"] for b in bets}:
            mine = [b for b in bets if b["sport"] == sport]
            try:
                finals = await self._finals(sport, min(b["commence_ts"] for b in mine))
            except Exception as e:
                log.warning(f"[SPORTS] {sport} 결과 조회 실패: {e}")
                continue
            for b in mine:
                res = bet_result(b, finals) if finals is not None else None
                if res is None and now - b["commence_ts"] >= VOID_AFTER:
                    res = "void"   # 연기 · 취소 — 결과가 안 나온다
                if res is not None:
                    await self._pay(b, res)

    async def _pay(self, b, res: str):
        payout = int(b["amount"] * b["odds"]) if res == "won" else b["amount"] if res == "void" else 0
        if not await self.db.settle_bet(b["id"], None if res == "void" else res == "won", payout):
            return
        if payout:
            await self.eco.add_balance(b["user_id"], payout)
        _, _, label, color = SPORTS[b["sport"]]
        log.info(f"[SPORTS] 정산: {b['sport']} {b['home']} vs {b['away']} · {b['pick']} → {res}")
        e = discord.Embed(title=f"{label} 베팅 정산", description=f"**{b['home']} vs {b['away']}**", color=color)
        if res == "won":
            e.add_field(name="결과", value="✅ 당첨!", inline=True)
            e.add_field(name="수령액", value=f"**+{payout:,}원**", inline=True)
        elif res == "void":
            e.add_field(name="결과", value="↩️ 무승부 · 경기 취소 — 환불", inline=True)
            e.add_field(name="환불", value=f"**+{payout:,}원**", inline=True)
        else:
            e.add_field(name="결과", value="❌ 낙첨", inline=True)
            e.add_field(name="손실", value=f"{b['amount']:,}원", inline=True)
        e.add_field(name="내 픽", value=f"{b['pick']} 승 ({b['odds']:.2f}x)", inline=False)
        await send_notify(self.bot, self.eco, b["user_id"], "토토_결과", e)

    @tasks.loop(minutes=30)
    async def _poll(self):
        for sport in enabled_sports():
            try:
                await self.refresh(sport)
            except Exception as e:
                log.warning(f"[SPORTS] {sport} 갱신 실패: {e}")
        try:
            await self.settle()   # 꺼 둔 종목도 이미 걸린 베팅은 정산한다
        except Exception as e:
            log.warning(f"[SPORTS] 정산 실패: {e}")

    @_poll.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(SportsToto(bot))
