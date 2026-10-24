# test_race.py — 레이스 3종(달팽이 · 조정 · F1): 상금표 · 연출 · 노 젓기 · 배당 기대값 · 트랙 그림 · 버튼 흐름 · 정산
# 실행: venv/Scripts/python.exe test_race.py
import asyncio
import logging
import random
import tempfile
from pathlib import Path
from types import SimpleNamespace

import discord

import services.economy_db as edb

edb.DB_PATH = Path(tempfile.mkdtemp()) / "t.sqlite3"
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)   # 로컬엔 NanumGothic 이 없다
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
    assert R.prizes(6, random.Random(1))[2] == 0                                     # 조정 3위는 본전
    for _ in range(300):
        finish = rng.sample(range(4), 4)
        fr = R.frames(finish, rng)
        assert len(fr) == 5 and all(0 <= x <= R.TRACK for f in fr for x in f)
        assert [fr[-1][h] for h in finish] == [R.TRACK - r for r in range(4)]         # 결승선: 순위대로
        cb = finish[rng.randint(1, 3)]
        fr = R.frames(finish, rng, comeback=cb)
        for f in fr[2:4]:                                                            # 막판까지 가짜 선두가 확실히 앞선다
            assert f[cb] > max(x for i, x in enumerate(f) if i != cb), (f, cb)
        assert fr[-1][finish[0]] == R.TRACK                                          # 그리고 결승선에서 뒤집힌다


def test_snail_twists():
    rng = random.Random(4)
    assert len({r[1] for r in R.SNAIL.POOL}) == 16                                   # 경마(10)보다 넓은 풀
    names_seen = set()
    for _ in range(500):
        runners = R.SNAIL.runners(rng)
        assert len(runners) == 4 and len({r[1] for r in runners}) == 4
        names_seen |= {r[1] for r in runners}
        finish = rng.sample(range(4), 4)
        cb = rng.choice([None, finish[1]])
        fr = R.frames(finish, rng, comeback=cb)
        last = list(fr[-1])
        calls = R.SNAIL.twist(fr, finish, cb, [r[1] for r in runners], rng)
        assert fr[-1] == last and all(0 <= x <= R.TRACK for f in fr for x in f)      # 돌발 상황은 순위를 안 바꾼다
        assert set(calls) <= set(range(5))
        if cb is not None:
            assert 4 not in calls                                                    # 대역전 멘트를 덮지 않는다
    assert len(names_seen) == 16


def test_rowing():
    """끝까지 열심히 저으면 상대와 같은 조건(순위 고르게 → 기대값 0), 안 저으면 거의 꼴찌. 목표 이상 연타는 소용없다."""
    rng = random.Random(9)

    def ranks(taps, n=20000):
        cnt = [0] * R.ROW_N
        for _ in range(n):
            dist = [0.0] * R.ROW_N
            for _ in R.ROW_SEGS:
                R.row_step(dist, 0, taps, rng)
            cnt[sorted(range(R.ROW_N), key=lambda i: -dist[i]).index(0)] += 1
        return [c / n for c in cnt]
    full = ranks(R.ROW_TARGET)
    assert all(abs(p - 1 / R.ROW_N) < 0.015 for p in full), full                     # 전속력 = 공평
    assert ranks(R.ROW_TARGET * 5, 4000)[0] < 1 / R.ROW_N + 0.03                     # 매크로로 더 연타해도 이득 없음
    lazy = ranks(0, 4000)
    assert lazy[-1] > 0.3 and lazy[0] < 0.08                                         # 안 저으면 꼴찌가 제일 흔하다 (2.82: 더 덜 가혹)
    half = ranks(1, 4000)
    assert half[-1] > full[-1] + 0.08 and half[0] > 0.04                             # 덜 저으면 불리하지만 운은 남는다
    assert R.row_step([0.0] * 6, 0, 1, rng) == 1 / 3 and R.row_step([0.0] * 6, 0, 99, rng) == 1.0
    # 2.82 하향: 구간 5초 · 전속력 3번 · 최저 90% (모든 서버)
    assert (R.ROW_SEC, R.ROW_TARGET, R.ROW_LAZY) == (5.0, 3, 0.9)
    assert R.row_rules(1374213619793006704) == R.row_rules(1) == (5.0, 3, 0.9)
    assert all(0 <= c < R.TRACK for c in R.row_cells([0, 2.5, 5.6, 9.0, -1, 3]))
    assert "폭발" in R.row_call("나", 1.0, "상대", False) and "선두입니다" in R.row_call("나", 0.5, "나", True)


def test_f1_model():
    assert len(R.DRIVERS) == 22 and len({d[0] for d in R.DRIVERS}) == 22 and len(R.CIRCUITS) == 24
    assert all(sum(d[3] == t for d in R.DRIVERS) == 2 for t in R.TEAMS)              # 11팀 × 2명
    assert all(c[1] in R.TRACKS and len(R.TRACKS[c[1]]["pts"]) == 200 for c in R.CIRCUITS)   # 서킷마다 실제 모양
    assert all(0 <= v <= 1 for t in R.TRACKS.values() for p in t["pts"] for v in p)
    rng = random.Random(7)
    kinds, moved, leads = set(), 0, 0
    for _ in range(400):
        gp = rng.choice(R.CIRCUITS)
        rain = rng.random() < 0.3
        grid = R.f1_qualify(rain, rng)
        assert sorted(grid) == list(range(22))
        fin, out, snaps = R.f1_sim(grid, gp, rain, rng, record=True)
        assert sorted(fin + list(out)) == list(range(22)) and all(0 <= t <= 1 for t in out.values())
        assert len(snaps) == len(R.F1_CHECKS) and snaps[-1]["order"] == fin                # 중계 마지막 장면 = 결과
        prev = grid
        for s in snaps:
            o = s["order"]
            assert set(o) | set(s["out"]) == set(range(22)) and not set(o) & set(s["out"])  # 리타이어한 차는 빠진다
            assert [s["gap"][d] for d in o] == sorted(s["gap"][d] for d in o) and s["gap"][o[0]] == 0
            assert all(s["tyre"][d] in R.TYRE_PACE for d in o)
            if s["flag"] in ("SC", "RED"):                                               # 세이프티카 = 간격 압축 (그 틈에 피트인한 차만 뒤로)
                cheap = next(e[1] for e in s["ev"] if e[0] in ("sc", "red"))
                assert all(s["gap"][d] <= 0.6 * (len(o) - 1) + 1e-9 for d in o if d not in cheap)
            kinds |= {e[0] for e in s["ev"]}
            moved += sum(abs(prev.index(d) - o.index(d)) for d in o if d in prev)
            leads += o[0] != prev[0]
            stage, text = R.f1_call(s, prev, grid[3], gp[2])
            assert stage and text and R.f1_status(s, prev, grid[3])
            prev = o
    assert kinds >= {"crash1", "dnf", "crash", "pit", "slowpit", "pen", "sc", "vsc", "red", "shower"}   # 변수가 다 나온다
    assert leads / 400 > 1.5 and moved / (400 * len(R.F1_CHECKS) * 20) > 1               # 순위가 장면마다 출렁인다
    o = (1.5, 0.6)
    assert [R.f1_mult(o, r) for r in (0, 1, 2, 3, 9, 10, 21, None)] == [1.5, 0.6, 0.6, 0, 0, -1, -1, -2]

    def pole_win(name):
        gp = next(c for c in R.CIRCUITS if c[1] == name)
        wins = 0
        for _ in range(1500):
            grid = R.f1_qualify(False, rng)
            fin, _, _ = R.f1_sim(grid, gp, False, rng)
            wins += bool(fin) and fin[0] == grid[0]
        return wins / 1500
    assert pole_win("모나코 GP") > pole_win("이탈리아 GP · 몬차") + 0.1                 # 추월 어려운 서킷은 그리드가 더 중요


def test_f1_odds_ev():
    """배당표를 따로 크게 돌린 시뮬레이션으로 검산 — 어느 드라이버에 걸어도 기대값이 0을 넘지 않는다."""
    rng = random.Random(11)
    for _ in range(3):
        gp = rng.choice(R.CIRCUITS)
        rain = rng.random() < 0.3
        grid = R.f1_qualify(rain, rng)
        odds = R.f1_odds(grid, gp, rain, rng=random.Random(rng.random()))
        assert all(0.01 <= w <= R.F1_CAP and 0 <= p <= w for w, p in odds)
        ev, sims = [0.0] * 22, 20000
        for _ in range(sims):
            fin, out, _ = R.f1_sim(grid, gp, rain, rng)
            for r, d in enumerate(fin):
                ev[d] += R.f1_mult(odds[d], r)
            for d in out:
                ev[d] += R.f1_mult(odds[d], None)
        assert max(e / sims for e in ev) < 0.01, [round(e / sims, 3) for e in ev]
        assert odds[grid[0]][0] < odds[grid[-1]][0]                                  # 폴시터 배당 < 맨 뒤 배당


def test_f1_ladder_310():
    """3.1.0 배당: 어느 그리드에 걸어도 기대값이 0 밑 · 강팀은 4위 밖이면 손실 · 하위 팀은 포인트만 따도 수익."""
    rng = random.Random(5)
    gp = next(c for c in R.CIRCUITS if c[1] == "영국 GP · 실버스톤")
    grid = R.f1_qualify(False, rng)
    lad = R.f1_ladder(R.f1_counts(grid, gp, False, rng=random.Random(1)))
    assert all(len(l) == 5 and l[4] == -2 and l[0] > 0 for l in lad)
    assert lad[grid[0]][2] < 0 and lad[grid[0]][1] >= 0                       # 폴시터: 포인트권(4~10위)은 손실
    assert lad[grid[-1]][2] > 0 and lad[grid[-1]][3] == 0                      # 맨 뒤: 포인트면 수익 · 11위 이하 본전
    assert R.f1_mult(lad[grid[-1]], 5) == lad[grid[-1]][2] and R.f1_mult(lad[grid[-1]], None) == -2
    ev, sims = [0.0] * 22, 15000
    for _ in range(sims):
        fin, out, _ = R.f1_sim(grid, gp, False, rng)
        for r, d in enumerate(fin):
            ev[d] += R.f1_mult(lad[d], r)
        for d in out:
            ev[d] += R.f1_mult(lad[d], None)
    assert max(e / sims for e in ev) < 0.01 and min(e / sims for e in ev) > -0.3, [round(e / sims, 3) for e in ev]
    assert "본전" in R.f1_ladder_text(lad[grid[0]])


def test_f1_render():
    rng = random.Random(2)
    for gp in R.CIRCUITS[::6]:
        grid = R.f1_qualify(False, rng)
        png = R.f1_render(gp, "출발 그리드", R.f1_grid_snap(grid), -1, None, [(1.0, 0.4)] * 22)
        assert png[:4] == b"\x89PNG" and len(png) < 400_000
        _, _, snaps = R.f1_sim(grid, gp, True, rng, record=True)
        for s in (snaps[0], snaps[3], snaps[-1]):
            assert R.f1_render(gp, "LAP 9/50", s, grid[15], grid, None, s["frac"] >= 1)[:4] == b"\x89PNG"
    assert R.F1_LOGO.startswith("https://") and R.F1_LOGO.endswith(".png")
    assert len(R.TEAM_LOGO) == len(R.TEAM_ICON) == 11 and all(t in R.TEAM_LOGO or t in R.TEAM_TAG for t in R.TEAMS)   # 팀마다 로고 또는 이름
    assert all(img.shape[0] == 64 and img.shape[2] == 4 for img in R.TEAM_LOGO.values())   # 미리 줄여 둔 투명 배경 로고
    assert sorted(f.stem for f in R.EMOJI_DIR.glob("*.png")) == sorted(R.TEAM_LOGO)


def test_emoji_and_ready():
    """팀 로고 앱 이모지: 없는 것만 올리고, 출전표 · 드라이버 목록에 쓴다. 경주는 미리 만들어 둔 걸 꺼내 쓴다."""
    created = []

    async def flow():
        async def ready():
            pass

        async def fetch():
            return [discord.PartialEmoji(name="f1_mer", id=1)]

        async def create(name, image):
            assert image[:4] == b"\x89PNG"
            created.append(name)
            return discord.PartialEmoji(name=name, id=len(created) + 1)
        cog = R.Race.__new__(R.Race)
        cog.bot = SimpleNamespace(wait_until_ready=ready, fetch_application_emojis=fetch, create_application_emoji=create)
        cog.team_emoji, cog.f1_ready, cog._filling = {}, [], None
        await cog._sync_emoji()
        assert len(cog.team_emoji) == 11 and "f1_mer" not in created and len(created) == 10     # 이미 있는 건 다시 안 올린다
        assert cog.temoji("MER").id == 1 and all(cog.temoji(t) is cog.team_emoji[t] for t in R.TEAMS)   # 11팀 모두 로고

        race = await cog._prepare_f1()
        assert set(race) == {"gp", "rain", "grid", "odds", "png", "odds310", "png310", "odds311", "png311"} and race["png310"][:4] == b"\x89PNG"
        user = SimpleNamespace(id=5, display_name="a", display_avatar=SimpleNamespace(url="https://x/a.png"))
        card = cog._f1_card(user, 1000, race)
        fields = " ".join(f.value for f in card.fields)
        assert "<:f1_mer:1>" in fields and "<:f1_rb:" in fields                          # 배당표: 로고 이모지
        opts = R.PickView(cog, "f1", user, 1000, race).children[0].options
        team = {o.value: R.DRIVERS[int(o.value)][3] for o in opts}
        assert all((o.emoji.id == cog.team_emoji[team[o.value]].id) if team[o.value] in cog.team_emoji
                   else str(o.emoji) == R.TEAMS[team[o.value]][1] for o in opts)          # 드롭다운도 로고
        cog._refill()                                                                       # 뒤에서 2개 채운다
        await cog._filling
        assert len(cog.f1_ready) == R.F1_READY
        cog._refill()
        assert cog._filling.done() and len(cog.f1_ready) == R.F1_READY                        # 다 차 있으면 더 안 만든다
    asyncio.run(flow())


def _inter(user, sent, views):
    async def arec(*a, **k):
        sent.append(k.get("embed"))
        views.append(k.get("view"))
        return SimpleNamespace(edit=arec)

    async def anoop(*a, **k):
        pass
    return SimpleNamespace(user=user, guild_id=None, response=SimpleNamespace(defer=anoop, edit_message=arec),
                           edit_original_response=arec, followup=SimpleNamespace(send=arec))


def test_flows():
    cog = R.Race.__new__(R.Race)
    cog.db = edb.EconomyDB()
    cog.team_emoji, cog.f1_ready, cog._filling = {}, [], None
    cog._refill = lambda: None                      # 테스트에선 뒤에서 미리 만들지 않는다
    user = SimpleNamespace(id=77, display_name="기수", display_avatar=SimpleNamespace(url="https://x/a.png"))

    async def flow():
        await cog.db.add_balance(user.id, 1_000_000)
        sent, views = [], []
        inter = _inter(user, sent, views)
        rowing = {"view": None, "taps": 0}
        real_sleep = R.asyncio.sleep

        async def fake_sleep(s):
            v = rowing["view"]
            if v is not None and v.started:      # 조정 구간 동안 노 젓기 연타
                for _ in range(rowing["taps"]):
                    await v.children[0].callback(inter)
            await real_sleep(0)
        R.asyncio.sleep = fake_sleep
        try:
            bal = 1_000_000
            # 🐌 출전표: 베팅금 먼저 차감 → 고르면 베팅금 + 순이익 → 전체 변동 = 상금표의 그 칸
            await R.Race.snail.callback(cog, inter, 10_000)
            assert await cog.db.get_balance(user.id) == bal - 10_000
            view = views[-1]
            assert len(view.children) == 4 and "상금표" in sent[-1].description
            n = len(sent)
            await view.children[0].callback(inter)
            assert len(sent) - n == 7 and "잔액" in sent[-1].description              # 대기 + 5장면 + 결과
            played = await cog.db.get_balance(user.id) - bal
            assert played in {10_000 * m for m in view.race["prizes"]}, played
            await view.on_timeout()                                                  # 고른 뒤엔 시간 초과 무시
            bal += played
            assert await cog.db.get_balance(user.id) == bal

            # 🚣 출발 → 구간마다 연타 → 순위대로 정산. 전속력으로 여러 번 → 순위가 고르게 나온다
            places = set()
            for taps in (R.ROW_TARGET,) * 6 + (0,):
                await R.Race.row.callback(cog, inter, 10_000)
                view = views[-1]
                assert "싱글 스컬" in sent[-1].title and view.children[0].label == "출발!"
                rowing.update(view=view, taps=taps)
                n = len(sent)
                await view.children[0].callback(inter)                               # 출발!
                assert view.children[0].label == "노 젓기!" and view.is_finished()
                assert len(sent) - n == 1 + len(R.ROW_SEGS) + 1 and "잔액" in sent[-1].description
                assert f"**{taps}번** 저음" in sent[-2].description
                played = await cog.db.get_balance(user.id) - bal
                assert played in {10_000 * m for m in view.race["prizes"]}, played
                bal += played
                places.add(next(i for i, m in enumerate(view.race["prizes"]) if 10_000 * m == played))
            rowing["view"] = None
            assert len(places) >= 2

            # 🏎️ 드라이버 목록에서 고르기 → 6장면(그림 + 순위표) → 순위에 맞는 배당
            await R.Race.f1.callback(cog, inter, 10_000)
            view = views[-1]
            sel = view.children[0]
            card = sent[-1]
            assert len(sel.options) == 22 and "출발 그리드" in card.title and card.thumbnail.url == R.F1_LOGO
            assert len(card.fields) == 2 and "P22" in card.fields[1].value and "세이프티카" in card.description
            me = view.race["grid"][5]
            sel._values = [str(me)]
            n = len(sent)
            await sel.callback(inter)
            assert len(sent) - n == 1 + len(R.F1_CHECKS) + 1 and "잔액" in sent[-1].description
            assert all(e.image.url == "attachment://f1.png" for e in sent[n:])
            played = await cog.db.get_balance(user.id) - bal
            allowed = {int(10_000 * R.f1_mult(view.race["odds"][me], r)) for r in (0, 1, 3, 10, None)}
            assert played in allowed, (played, allowed)
            bal += played

            # 60초 동안 안 고르면(안 출발하면): 베팅금을 잃고 실격
            for cmd in (R.Race.snail, R.Race.row, R.Race.f1):
                await cmd.callback(cog, inter, 10_000)
                await views[-1].on_timeout()
                assert "실격" in sent[-1].title and "-10,000원" in sent[-1].description
                bal -= 10_000
            assert await cog.db.get_balance(user.id) == bal

            # 잔액이 모자라면 시작도 안 한다
            poor = SimpleNamespace(id=78, display_name="빈털터리", display_avatar=user.display_avatar)
            await R.Race.row.callback(cog, _inter(poor, sent, views), 5_000)
            assert "참가할 수 없어요" in sent[-1].title and await cog.db.get_balance(poor.id) == 0
        finally:
            R.asyncio.sleep = real_sleep
    asyncio.run(flow())


if __name__ == "__main__":
    test_prizes_frames()
    test_snail_twists()
    test_rowing()
    test_f1_model()
    test_f1_odds_ev()
    test_f1_ladder_310()
    test_f1_render()
    test_emoji_and_ready()
    test_flows()
    print("OK: race")
