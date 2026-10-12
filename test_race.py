# test_race.py — 레이스 3종(달팽이 · 경정 · F1): 상금표 · 연출 · 배당 기대값 · 버튼 흐름 · 정산
# 실행: venv/Scripts/python.exe test_race.py
import asyncio
import random
import tempfile
from pathlib import Path
from types import SimpleNamespace

import services.economy_db as edb

edb.DB_PATH = Path(tempfile.mkdtemp()) / "t.sqlite3"
import cogs.race as R   # noqa: E402


def test_prizes_frames():
    rng = random.Random(3)
    for n in (4, 6):
        seen = set()
        for _ in range(3000):
            p = R.prizes(n, rng)
            assert len(p) == n and sum(p) == 0 and p[0] > p[1] > 0, p              # 1 · 2위 수익, 기대값 0
            assert all(x <= 0 for x in p[2:]) and p[-1] < 0 and p[2:] == sorted(p[2:], reverse=True), p
            seen.add(tuple(p))
        assert len(seen) > 10                                                        # 상금표가 경주마다 다양하다
        for _ in range(300):
            finish = rng.sample(range(n), n)
            fr = R.frames(finish, rng)
            assert len(fr) == 5 and all(0 <= x <= R.TRACK for f in fr for x in f)
            assert [fr[-1][h] for h in finish] == [R.TRACK - r for r in range(n)]     # 결승선: 순위대로
            cb = finish[rng.randint(1, n - 1)]
            fr = R.frames(finish, rng, comeback=cb)
            for f in fr[2:4]:                                                        # 막판까지 가짜 선두가 확실히 앞선다
                assert f[cb] > max(x for i, x in enumerate(f) if i != cb), (f, cb)
            assert fr[-1][finish[0]] == R.TRACK                                      # 그리고 결승선에서 뒤집힌다
    assert R.prizes(6, random.Random(1))[2] == 0                                     # 경정 3착은 본전


def test_pools_twists():
    rng = random.Random(4)
    assert len({r[1] for r in R.SNAIL.POOL}) == 16 and len({r[0] for r in R.BOAT.POOL}) == 16   # 경마(10)보다 넓은 풀
    for game in (R.SNAIL, R.BOAT):
        assert len(game.calls) == 5 and len(game.results) == game.n
        names_seen = set()
        for _ in range(500):
            runners = game.runners(rng)
            assert len(runners) == game.n and len({r[1] for r in runners}) == game.n
            names_seen |= {r[1] for r in runners}
            finish = rng.sample(range(game.n), game.n)
            cb = rng.choice([None, finish[1]])
            fr = R.frames(finish, rng, comeback=cb)
            last = list(fr[-1])
            calls = game.twist(fr, finish, cb, [r[1] for r in runners], rng)
            assert fr[-1] == last and all(0 <= x <= R.TRACK for f in fr for x in f)  # 돌발 상황은 순위를 안 바꾼다
            assert set(calls) <= set(range(5)) and all(isinstance(t, str) for t in calls.values())
            if cb is not None:
                assert 4 not in calls                                                # 대역전 멘트를 덮지 않는다
        assert len(names_seen) == 16                                                 # 풀 전체가 돌아가며 나온다
    assert [r[0] for r in R.BOAT.runners(rng)] == list(R.Boat.LANES)                 # 정번 색은 레인 순서대로


def test_f1_model():
    assert len(R.DRIVERS) == 22 and len({d[0] for d in R.DRIVERS}) == 22 and len(R.CIRCUITS) == 24
    assert all(sum(d[3] == t for d in R.DRIVERS) == 2 for t in R.TEAMS)              # 11팀 × 2명
    rng = random.Random(7)
    for _ in range(200):
        gp = rng.choice(R.CIRCUITS)
        grid = R.f1_qualify(rng.random() < 0.3, rng)
        assert sorted(grid) == list(range(22))
        fin, out = R.f1_race(grid, gp, False, rng.random() < 0.5, rng)
        assert sorted(fin + list(out)) == list(range(22)) and all(0 < t < 1 for t in out.values())
        cb = fin[1] if rng.random() < 0.5 else None
        fr = R.f1_frames(grid, fin, out, cb, rng)
        assert len(fr) == len(R.F1_STAGES) and fr[-1] == fin
        for k, (f, order) in enumerate(zip(R.F1_STAGES, fr[:-1])):
            assert set(order) == {d for d in range(22) if out.get(d, 2) > f}          # 리타이어한 차는 그 시점부터 빠진다
            if cb is not None and k >= len(R.F1_STAGES) - 3:
                assert order[0] == cb                                                # 막판 가짜 선두
    o = (1.5, 0.6)
    assert [R.f1_mult(o, r) for r in (0, 1, 2, 3, 9, 10, 21, None)] == [1.5, 0.6, 0.6, 0, 0, -1, -1, -2]

    # 그리드가 결과에 반영된다: 모나코 폴은 몬차 폴보다 자주 이긴다
    def pole_win(name):
        gp = next(c for c in R.CIRCUITS if c[1] == name)
        wins = 0
        for _ in range(2000):
            grid = R.f1_qualify(False, rng)
            fin, _ = R.f1_race(grid, gp, False, False, rng)
            wins += bool(fin) and fin[0] == grid[0]
        return wins / 2000
    assert pole_win("모나코 GP") > pole_win("이탈리아 GP · 몬차") + 0.1


def test_f1_odds_ev():
    """배당표를 따로 크게 돌린 시뮬레이션으로 검산 — 어느 드라이버에 걸어도 기대값이 0을 넘지 않는다."""
    rng = random.Random(11)
    for _ in range(4):
        gp = rng.choice(R.CIRCUITS)
        rain = rng.random() < 0.3
        grid = R.f1_qualify(rain, rng)
        odds = R.f1_odds(grid, gp, rain, rng=random.Random(rng.random()))
        assert all(0.01 <= w <= R.F1_CAP and 0 <= p <= w for w, p in odds)
        ev, sims = [0.0] * 22, 30000
        for _ in range(sims):
            fin, out = R.f1_race(grid, gp, rain, rng.random() < gp[4] / 100, rng)
            for r, d in enumerate(fin):
                ev[d] += R.f1_mult(odds[d], r)
            for d in out:
                ev[d] += R.f1_mult(odds[d], None)
        assert max(e / sims for e in ev) < 0.01, [round(e / sims, 3) for e in ev]
        fav = grid[0]
        assert odds[fav][0] < odds[grid[-1]][0]                                      # 폴시터 배당 < 맨 뒤 배당


def _inter(user, sent, views):
    async def arec(*a, **k):
        sent.append(k.get("embed"))
        views.append(k.get("view"))
        return SimpleNamespace(edit=arec)

    async def anoop(*a, **k):
        pass
    return SimpleNamespace(user=user, response=SimpleNamespace(defer=anoop, edit_message=arec),
                           edit_original_response=arec, followup=SimpleNamespace(send=arec))


def test_flows():
    cog = R.Race.__new__(R.Race)
    cog.db = edb.EconomyDB()
    user = SimpleNamespace(id=77, display_name="기수", display_avatar=SimpleNamespace(url="https://x/a.png"))

    async def flow():
        await cog.db.add_balance(user.id, 1_000_000)
        sent, views = [], []
        inter = _inter(user, sent, views)
        real_sleep, R.asyncio.sleep = R.asyncio.sleep, (lambda s: real_sleep(0))
        try:
            bal = 1_000_000
            for cmd, game in ((R.Race.snail, R.SNAIL), (R.Race.boat, R.BOAT)):
                # 출전표: 베팅금 먼저 차감 → 고르면 베팅금 + 순이익 → 전체 변동 = 상금표의 그 칸
                await cmd.callback(cog, inter, 10_000)
                assert await cog.db.get_balance(user.id) == bal - 10_000
                view = views[-1]
                assert len(view.children) == game.n and "상금표" in sent[-1].description
                n = len(sent)
                await view.children[0].callback(inter)
                assert len(sent) - n == 7 and "잔액" in sent[-1].description          # 대기 + 5장면 + 결과
                played = await cog.db.get_balance(user.id) - bal
                assert played in {10_000 * m for m in view.race["prizes"]}, played
                await view.on_timeout()                                              # 고른 뒤엔 시간 초과 무시
                bal += played
                assert await cog.db.get_balance(user.id) == bal

            # F1: 드라이버 목록에서 고르기 → 6장면 → 순위에 맞는 배당
            await R.Race.f1.callback(cog, inter, 10_000)
            view = views[-1]
            sel = view.children[0]
            assert len(sel.options) == 22 and "출발 그리드" in sent[-1].title and "P22" in sent[-1].description
            me = view.race["grid"][5]
            sel._values = [str(me)]
            n = len(sent)
            await sel.callback(inter)
            assert len(sent) - n == 1 + len(R.F1_STAGES) + 1 and "잔액" in sent[-1].description
            played = await cog.db.get_balance(user.id) - bal
            allowed = {int(10_000 * R.f1_mult(view.race["odds"][me], r)) for r in (0, 1, 3, 10, None)}
            assert played in allowed, (played, allowed)
            bal += played

            # 60초 동안 안 고르면: 베팅금을 잃고 실격
            for cmd in (R.Race.snail, R.Race.f1):
                await cmd.callback(cog, inter, 10_000)
                await views[-1].on_timeout()
                assert "실격" in sent[-1].title and "-10,000원" in sent[-1].description
                bal -= 10_000
            assert await cog.db.get_balance(user.id) == bal

            # 잔액이 모자라면 시작도 안 한다
            poor = SimpleNamespace(id=78, display_name="빈털터리", display_avatar=user.display_avatar)
            await R.Race.boat.callback(cog, _inter(poor, sent, views), 5_000)
            assert "참가할 수 없어요" in sent[-1].title and await cog.db.get_balance(poor.id) == 0
        finally:
            R.asyncio.sleep = real_sleep
    asyncio.run(flow())


if __name__ == "__main__":
    test_prizes_frames()
    test_pools_twists()
    test_f1_model()
    test_f1_odds_ev()
    test_flows()
    print("OK: race")
