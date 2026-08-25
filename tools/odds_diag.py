"""Odds API 진단: 키 확인 → 대회 매핑 점검 → 실제 경기 매칭까지 확인.

    .venv/Scripts/python.exe tools/odds_diag.py          # 키/매핑만 (크레딧 0)
    .venv/Scripts/python.exe tools/odds_diag.py PL       # 해당 대회 실제 매칭까지 (크레딧 소모)
"""
import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
import aiohttp
from services.odds_api import OddsAPI, SPORT_KEYS
from services.football_api import FootballAPI

KEY = os.getenv("ODDS_API_KEY", "")
print("ODDS_API_KEY: %s (%d자)" % ("설정됨" if KEY else "없음!", len(KEY)))
if not KEY:
    sys.exit(1)


async def main():
    comp = sys.argv[1].upper() if len(sys.argv) > 1 else None
    async with aiohttp.ClientSession() as sess:
        async with sess.get("https://api.the-odds-api.com/v4/sports",
                            params={"apiKey": KEY, "all": "false"}, timeout=20) as r:
            print("GET /sports -> HTTP %s | 잔여 %s / 사용 %s" % (
                r.status, r.headers.get("x-requests-remaining", "?"),
                r.headers.get("x-requests-used", "?")))
            if r.status != 200:
                print("본문:", (await r.text())[:300]); return
            have = {d["key"] for d in await r.json()}

        print("\nSPORT_KEYS 매핑 점검:")
        for code, sk in SPORT_KEYS.items():
            print("  %-4s %-42s %s" % (code, sk, "OK" if sk in have else "<-- 지금 배당 없음(비시즌)"))

        if not comp:
            print("\n실제 매칭까지 보려면 대회 코드를 인자로 주세요. 예: odds_diag.py PL")
            return

        odds = OddsAPI(sess)
        events = await odds.get_events(comp)
        print("\n[%s] Odds API 이벤트 %d개" % (comp, len(events)))
        for ev in events[:5]:
            h2h = OddsAPI.extract_h2h(ev)
            print("   %-28s vs %-28s -> %s" % (
                ev.get("home_team"), ev.get("away_team"),
                ("%.2f / %.2f / %.2f" % h2h) if h2h else "h2h 추출 실패"))

        api = FootballAPI(sess)
        now = datetime.now(tz=timezone.utc)
        matches = await api.competition_matches(
            competition_code=comp, season_year=None, status="SCHEDULED",
            date_from=now.date().isoformat(),
            date_to=now.replace(year=now.year + 1).date().isoformat(), limit=10)
        print("\n[%s] football-data 예정 경기 %d개 | 매칭 결과:" % (comp, len(matches)))
        hit = 0
        for m in matches[:10]:
            home = (m.get("homeTeam") or {}).get("name") or "?"
            away = (m.get("awayTeam") or {}).get("name") or "?"
            ts = int(datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")).timestamp())
            ev = OddsAPI.find_match(home, away, ts, events)
            if ev:
                h2h = OddsAPI.extract_h2h(ev)
                hit += bool(h2h)
                print("   OK   %-24s vs %-24s -> %s" % (
                    home, away, ("%.2f / %.2f / %.2f" % h2h) if h2h else "h2h 실패"))
            else:
                print("   MISS %-24s vs %-24s" % (home, away))
        print("\n결과: %d/%d 경기에 배당 적용 가능" % (hit, min(10, len(matches))))

asyncio.run(main())
