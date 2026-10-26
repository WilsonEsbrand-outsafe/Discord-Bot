# test_sports_toto.py — 3.2.1: MLB · KBO · NBA 토토 (배당 크레딧 아끼기 · 베팅 · 정산).  실행: python test_sports_toto.py
import asyncio
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import services.economy_db as edb
import services.sports_db as sdb
import services.ufc_db as udb

TMP = Path(tempfile.mkdtemp())
edb.DB_PATH = TMP / "e.sqlite3"
sdb.DB_PATH = str(TMP / "s.sqlite3")
udb.DB_PATH = str(TMP / "u.sqlite3")

import cogs.sports_toto as st  # noqa: E402

U = 7


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def test_helpers():
    assert st.same_game("Los Angeles Clippers", "Boston Celtics", "Boston Celtics", "LA Clippers")
    assert not st.same_game("Boston Red Sox", "New York Yankees", "Chicago White Sox", "New York Mets")
    odds = st.parse_odds([{"id": "e1", "home_team": "A", "away_team": "B", "bookmakers": [
        {"markets": [{"key": "h2h", "outcomes": [{"name": "A", "price": 1.8}, {"name": "B", "price": 2.0}]}]},
        {"markets": [{"key": "h2h", "outcomes": [{"name": "A", "price": 1.9}, {"name": "B", "price": 2.1}]}]}]}])
    assert odds == [("e1", 1.85, 2.05)]
    fin = st.espn_finals([{"competitions": [{"date": "2026-10-25T23:00Z", "status": {"type": {"completed": True}},
                                            "competitors": [{"homeAway": "home", "team": {"displayName": "LA Clippers"}, "winner": True},
                                                            {"homeAway": "away", "team": {"displayName": "Boston Celtics"}}]}]}])
    bet = {"event_id": "x", "home": "Los Angeles Clippers", "away": "Boston Celtics", "commence_ts": st._ts("2026-10-25T23:10:00Z"),
           "pick": "Los Angeles Clippers"}
    assert st.bet_result(bet, fin) == "won" and st.bet_result({**bet, "pick": "Boston Celtics"}, fin) == "lost"
    tie = st.odds_finals([{"id": "k1", "completed": True, "home_team": "LG Twins", "away_team": "Doosan Bears",
                           "commence_time": "2026-10-25T09:30:00Z",
                           "scores": [{"name": "LG Twins", "score": "3"}, {"name": "Doosan Bears", "score": "3"}]}])
    assert st.bet_result({"event_id": "k1", "home": "LG Twins", "away": "Doosan Bears", "commence_ts": 0, "pick": "LG Twins"}, tie) == "void"


async def _flow():
    now = int(time.time())
    calls = []
    events = [{"id": "g1", "home_team": "Los Angeles Dodgers", "away_team": "Toronto Blue Jays", "commence_time": _iso(now + 20 * 3600)}]
    odds = [{"id": "g1", "home_team": "Los Angeles Dodgers", "away_team": "Toronto Blue Jays", "bookmakers": [
        {"markets": [{"key": "h2h", "outcomes": [{"name": "Los Angeles Dodgers", "price": 1.7},
                                                 {"name": "Toronto Blue Jays", "price": 2.2}]}]}]}]
    espn = {"events": []}

    async def fake_get(url, params):
        calls.append(url.rsplit("/", 1)[-1])
        if url.endswith("/events"):
            return 200, events
        if url.endswith("/odds"):
            st._credits["remaining"] = 70
            return 200, odds
        if url.endswith("/scoreboard"):
            return 200, espn
        return 404, None
    st._get = fake_get
    st.ODDS_API_KEY = "k"

    cog = st.SportsToto.__new__(st.SportsToto)
    cog.bot, cog.db, cog.eco = None, sdb.SportsDB(), edb.EconomyDB()
    sent = []
    async def fake_notify(bot, eco, uid, key, embed):
        sent.append((uid, key, embed))
    st.send_notify = fake_notify

    # 새 경기 → 배당 1번. 바로 다시 돌면 무료 목록만 (배당 호출 없음)
    await cog.refresh("mlb", now)
    assert calls == ["events", "odds"], calls
    await cog.refresh("mlb", now + 1800)
    assert calls[2:] == ["events"], calls
    games = await cog.games("mlb")
    assert games and games[0]["home_odds"] == 1.7 and games[0]["away_odds"] == 2.2

    # 시작 3시간 안 · 배당이 6시간 넘게 묵음 → 한 번 더. 그다음엔 또 안 받는다
    odds[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = 1.5
    calls.clear()
    await cog.refresh("mlb", now + 18 * 3600)
    await cog.refresh("mlb", now + 18 * 3600 + 1800)
    assert calls == ["events", "odds", "events"], calls

    # 크레딧이 모자라면 새 경기가 생겨도 배당을 안 받는다
    st._credits["remaining"] = 10
    events.append({"id": "g2", "home_team": "A Twins", "away_team": "B Bears", "commence_time": _iso(now + 40 * 3600)})
    calls.clear()
    await cog.refresh("mlb", now + 22 * 3600)
    assert calls == ["events"], calls
    st._credits["remaining"] = 400

    # 베팅: 배당은 지금 표 값으로 고정, 30분 전 마감
    await cog.eco.add_balance(U, 100_000)
    g = (await cog.games("mlb"))[0]
    err, o = await st.place_sports_bet(U, "mlb", g, "Los Angeles Dodgers", 10_000, cog.eco, cog.db)
    assert err is None and o == 1.5 and await cog.eco.get_balance(U) == 90_000
    err, _ = await st.place_sports_bet(U, "mlb", g, "Toronto Blue Jays", 1_000, cog.eco, cog.db)
    assert "이미" in err
    real_time = st.time.time
    st.time.time = lambda: g["ts"] - 600
    err, _ = await st.place_sports_bet(U + 1, "mlb", g, "Toronto Blue Jays", 1_000, cog.eco, cog.db)
    st.time.time = real_time
    assert "마감" in err

    # 정산: ESPN 결과 → 당첨 지급 · 알림, 두 번 돌아도 한 번만
    espn["events"] = [{"competitions": [{"date": _iso(g["ts"]), "status": {"type": {"completed": True}}, "competitors": [
        {"homeAway": "home", "team": {"displayName": "Los Angeles Dodgers"}, "winner": True},
        {"homeAway": "away", "team": {"displayName": "Toronto Blue Jays"}}]}]}]
    await cog.settle(g["ts"] + 4 * 3600)
    await cog.settle(g["ts"] + 5 * 3600)
    assert await cog.eco.get_balance(U) == 105_000 and len(sent) == 1 and sent[0][1] == "토토_결과"
    r = (await cog.db.list_recent_for_user(U))[0]
    assert r["settled"] == 1 and r["won"] == 1 and r["payout"] == 15_000

    # /토토 · /내베팅 — 테스트 서버에 야구 메뉴 · 내역
    import cogs.toto as ct
    toto = ct.Toto.__new__(ct.Toto)
    toto.db, toto.ufc, toto.sports, toto.api, toto.odds = cog.eco, udb.UfcDB(), cog.db, None, None
    toto.bot = SimpleNamespace(get_cog=lambda name: cog)
    async def no_fights(**k):
        return []
    ct.upcoming_fights = no_fights
    events[1]["commence_time"] = _iso(now + 30 * 3600)
    await cog.db.set_odds([("g2", 1.9, 1.9)], now)
    out = []
    async def rec(*a, **k):
        out.append(k)
    async def noop(*a, **k):
        pass
    user = SimpleNamespace(id=U, display_name="야구팬", display_avatar=SimpleNamespace(url="https://x/a.png"))
    inter = SimpleNamespace(user=user, guild_id=ct.release.TEST_GUILD, response=SimpleNamespace(defer=noop, send_message=rec),
                            followup=SimpleNamespace(send=rec))
    await ct.Toto.toto_list.callback(toto, inter)
    menu = out[-1]["view"]
    assert [o.value for o in menu.selects[0].options] == ["b:mlb:g1", "b:mlb:g2"]
    assert "⚾ MLB" in out[-1]["embeds"][0].title
    inter.guild_id = 1                                   # 공개 전 서버엔 안 보인다
    await ct.Toto.toto_list.callback(toto, inter)
    assert "view" not in out[-1] or not any(o.value.startswith("b:") for m in out[-1]["view"].selects for o in m.options)
    inter.guild_id = ct.release.TEST_GUILD
    await ct.Toto.my_bets.callback(toto, inter)
    assert "Los Angeles Dodgers vs Toronto Blue Jays" in out[-1]["embed"].description


def main():
    test_helpers()
    asyncio.run(_flow())
    print("OK: sports toto")


if __name__ == "__main__":
    main()
