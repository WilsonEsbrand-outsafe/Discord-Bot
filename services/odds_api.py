# services/odds_api.py
import os
import asyncio
import aiohttp
from difflib import SequenceMatcher
from datetime import datetime, timezone
from typing import Optional

BASE = "https://api.the-odds-api.com/v4"

# football-data.org 대회 코드 → The Odds API sport key 매핑
SPORT_KEYS: dict[str, str] = {
    "PL":  "soccer_england_premier_league",
    "ELC": "soccer_england_league1",
    "BL1": "soccer_germany_bundesliga",
    "FL1": "soccer_france_ligue_one",
    "SA":  "soccer_italy_serie_a",
    "PD":  "soccer_spain_la_liga",
    "DED": "soccer_netherlands_eredivisie",
    "PPL": "soccer_portugal_primeira_liga",
    "CL":  "soccer_uefa_champs_league",
    "EC":  "soccer_uefa_european_championship",
    "WC":  "soccer_fifa_world_cup",
    "CLI": "soccer_conmebol_libertadores",
    "BSA": "soccer_brazil_campeonato",
}


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


class OddsAPI:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.api_key = os.getenv("ODDS_API_KEY", "")

    def _enabled(self) -> bool:
        return bool(self.api_key)

    async def get_events(self, competition_code: str) -> list[dict]:
        """
        대회의 예정 경기 배당 목록을 가져옵니다.
        API 키가 없거나 지원하지 않는 대회면 빈 리스트 반환.
        """
        if not self._enabled():
            return []

        sport = SPORT_KEYS.get(competition_code.upper())
        if not sport:
            print(f"[ODDS] {competition_code}: sport key 없음, 스킵")
            return []

        url = f"{BASE}/sports/{sport}/odds"
        params = {
            "apiKey":      self.api_key,
            "regions":     "eu",
            "markets":     "h2h",
            "dateFormat":  "iso",
            "oddsFormat":  "decimal",
        }

        for attempt in range(3):
            try:
                async with self.session.get(url, params=params, timeout=20) as r:
                    if r.status == 401:
                        print("[ODDS] API 키 오류 (401)")
                        return []
                    if r.status == 404 or r.status == 422:
                        print(f"[ODDS] {competition_code}: 해당 시즌 데이터 없음 ({r.status})")
                        return []
                    if r.status in (429, 500, 502, 503) and attempt < 2:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    r.raise_for_status()
                    data = await r.json()
                    remaining = r.headers.get("x-requests-remaining", "?")
                    print(f"[ODDS] {competition_code}: {len(data)}경기 수신 (잔여 요청: {remaining})")
                    return data
            except Exception as e:
                if attempt >= 2:
                    print(f"[ODDS] {competition_code} 조회 실패: {e}")
                    return []
                await asyncio.sleep(1.5)
        return []

    @staticmethod
    def extract_h2h(
        event: dict,
    ) -> Optional[tuple[float, float, float]]:
        """
        이벤트에서 북메이커 평균 홈/무/원정 배당을 추출합니다.
        배당이 없으면 None 반환.
        """
        home_team  = event.get("home_team", "")
        bookmakers = event.get("bookmakers") or []

        home_prices: list[float] = []
        draw_prices: list[float] = []
        away_prices: list[float] = []

        for bm in bookmakers:
            for market in (bm.get("markets") or []):
                if market.get("key") != "h2h":
                    continue
                for o in (market.get("outcomes") or []):
                    name  = o.get("name", "")
                    price = float(o.get("price") or 0)
                    if price <= 1.01:
                        continue
                    if name == "Draw":
                        draw_prices.append(price)
                    elif _sim(name, home_team) >= 0.5:
                        home_prices.append(price)
                    else:
                        away_prices.append(price)

        if not home_prices or not away_prices:
            return None

        avg_h = round(sum(home_prices) / len(home_prices), 2)
        avg_d = round(sum(draw_prices) / len(draw_prices), 2) if draw_prices else 3.00
        avg_a = round(sum(away_prices) / len(away_prices), 2)
        return avg_h, avg_d, avg_a

    @staticmethod
    def find_match(
        home: str,
        away: str,
        kickoff_ts: int,
        events: list[dict],
        time_tol: int = 7200,    # 킥오프 허용 오차 (초), 기본 2시간
        name_thr:  float = 0.45, # 팀명 유사도 최소 임계값
    ) -> Optional[dict]:
        """
        football-data.org 경기와 The Odds API 이벤트를 시각+팀명으로 매칭.
        가장 유사한 이벤트를 반환하며, 기준 미달이면 None.
        """
        best_event: Optional[dict] = None
        best_score = 0.0

        for ev in events:
            try:
                ev_ts = int(datetime.fromisoformat(
                    ev["commence_time"].replace("Z", "+00:00")
                ).timestamp())
            except Exception:
                continue

            if abs(ev_ts - kickoff_ts) > time_tol:
                continue

            ev_home = ev.get("home_team", "")
            ev_away = ev.get("away_team", "")

            score = (_sim(home, ev_home) + _sim(away, ev_away)) / 2
            if score > best_score and score >= name_thr:
                best_score = score
                best_event = ev

        return best_event
