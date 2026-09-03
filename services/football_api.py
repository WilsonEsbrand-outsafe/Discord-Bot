# services/football_api.py
import os
import asyncio
import time
import aiohttp
from collections import deque
from typing import List, Dict, Optional
from datetime import datetime, timezone

BASE = "https://api.football-data.org/v4"

# ─────────────────────────────────────────────────────────────
# 프로세스 전역 레이트 리미터
#
# football-data.org 무료 플랜은 분당 10회다. 정산 루프·자동 등록 루프·
# 수동 /토토불러오기 가 각자 FootballAPI 인스턴스를 만들어 쓰기 때문에
# 인스턴스 단위로 막으면 서로의 사용량을 모른다. 모든 호출이 _get 을
# 지나므로 여기 모듈 전역에서 한 번만 조절한다.
#
# 락을 쥔 채로 대기하므로 호출자들이 자연히 줄을 선다.
# ─────────────────────────────────────────────────────────────
RATE_LIMIT_PER_MIN = 9        # 10 중 1회는 여유로 남긴다
_rate_lock = asyncio.Lock()
_call_times: deque = deque()


async def _throttle() -> None:
    async with _rate_lock:
        while True:
            now = time.monotonic()
            while _call_times and (now - _call_times[0]) >= 60.0:
                _call_times.popleft()
            if len(_call_times) < RATE_LIMIT_PER_MIN:
                _call_times.append(now)
                return
            await asyncio.sleep(60.0 - (now - _call_times[0]) + 0.05)

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
            await _throttle()
            async with self.session.get(url, headers=self.headers, params=params, timeout=20) as r:
                if r.status in (429, 500, 502, 503, 504) and attempt < 2:
                    # 429면 서버가 알려준 만큼 기다린다. 예전엔 고정 1.5초라
                    # 한도가 소진된 상태에서 재시도가 다시 429를 불렀다.
                    if r.status == 429:
                        try:
                            wait = float(r.headers.get("Retry-After") or 0)
                        except ValueError:
                            wait = 0.0
                        await asyncio.sleep(max(wait, 10.0) + 0.5)
                    else:
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

