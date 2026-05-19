# services/football_api.py
import os
import asyncio
import aiohttp
from typing import List, Dict, Optional
from datetime import datetime, timezone

BASE = "https://api.football-data.org/v4"

def season_compact_to_year(compact: str) -> int:
    s = (compact or "").strip().replace(" ", "")
    if not s:
        raise ValueError("season is empty")
    if "-" in s:
        first = s.split("-", 1)[0]
    else:
        first = s
    if len(first) == 2 and first.isdigit():
        return 2000 + int(first)
    if len(first) == 4 and first.isdigit():
        return int(first)
    raise ValueError(f"invalid season string: {compact!r}")

class FootballAPI:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        token = os.getenv("FOOTBALL_DATA_TOKEN")
        if not token:
            raise RuntimeError("FOOTBALL_DATA_TOKEN이 .env에 없습니다.")
        self.headers = {"X-Auth-Token": token}

    async def _get(self, path: str, params: Optional[Dict] = None) -> Dict:
        url = f"{BASE}{path}"
        params = params or {}
        for attempt in range(3):
            async with self.session.get(url, headers=self.headers, params=params, timeout=20) as r:
                if r.status in (429, 500, 502, 503, 504) and attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                r.raise_for_status()
                return await r.json()
        return {}

    async def competition_matches(self, competition_code: str, season_year: Optional[int] = None,
                                  status: Optional[str] = None, date_from: Optional[str] = None,
                                  date_to: Optional[str] = None, limit: int = 5) -> List[Dict]:
        params: Dict[str, str] = {}
        if season_year: params["season"] = str(season_year)
        if status: params["status"] = status
        if date_from: params["dateFrom"] = date_from
        if date_to: params["dateTo"] = date_to
        data = await self._get(f"/competitions/{competition_code}/matches", params=params)
        matches = data.get("matches") or []
        matches.sort(key=lambda m: m.get("utcDate") or "9999-12-31T23:59:59Z")
        return matches[:limit]

    async def next_pl_fixtures(self, season_compact: str, limit: int = 5) -> List[Dict]:
        year = season_compact_to_year(season_compact)
        today = datetime.now(timezone.utc).date()
        date_from = today.isoformat()
        date_to = f"{year + 1}-06-30"
        matches = await self.competition_matches("PL", year, "SCHEDULED", date_from, date_to, limit * 3)
        try:
            timed = await self.competition_matches("PL", year, "TIMED", date_from, date_to, limit * 3)
            by_id = {m["id"]: m for m in matches}
            for t in timed:
                by_id.setdefault(t["id"], t)
            matches = list(by_id.values())
            matches.sort(key=lambda m: m.get("utcDate") or "")
        except Exception:
            pass
        def is_future(m) -> bool:
            try:
                from datetime import datetime as dt
                d = dt.fromisoformat(m["utcDate"].replace("Z", "+00:00")).date()
                return d >= today
            except Exception:
                return False
        return [m for m in matches if is_future(m)][:limit]
        
    async def match(self, match_id: str) -> Dict:
        return await self._get(f"/matches/{match_id}")

