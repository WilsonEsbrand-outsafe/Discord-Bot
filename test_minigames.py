# test_minigames.py — 훈련 / 페널티킥 / 퀴즈 검증.  실행: venv/Scripts/python.exe test_minigames.py
import asyncio
import random
import tempfile
from fractions import Fraction
from pathlib import Path

import services.economy_db as edb
import services.quiz as Q
from cogs.economy import Economy

TMP = Path(tempfile.mkdtemp()) / "t.sqlite3"
edb.DB_PATH = TMP
Q.DB_PATH = TMP


def test_penalty_table():
    t = Economy.PK_TABLE
    assert len(t) == 10 and len({m for _, m, *_ in t}) == 10   # 10단계, 배수 모두 다름
    assert abs(sum(p for p, *_ in t) - 1) < 1e-9
    ev = sum(p * float(Fraction(m)) for p, m, *_ in t)
    assert -0.03 < ev < 0, ev                                # 약한 하우스 엣지
    mult_of = {name: m for _, m, _, name, *_ in t}
    assert (mult_of["골"], mult_of["골대 맞고 인"], mult_of["노룩 킥"], mult_of["손 맞고 골"]) == ("1", "2", "5", "0")
    assert (mult_of["선방"], mult_of["골대 강타"], mult_of["부정킥"], mult_of["관중석 홈런"]) == ("-1", "-2", "-5", "-10")
    prob_of = {name: p for p, _, _, name, *_ in t}
    assert max(t, key=lambda r: r[0])[3] == "선방"           # 1배 손실(선방)이 가장 자주 나온다
    assert abs(prob_of["선방"] - prob_of["골"]) <= 0.03       # 골과 비슷하게
    assert 0 < prob_of["골대 강타"] - prob_of["골대 맞고 인"] <= 0.03   # 2배 손실이 2배 수익보다 살짝 높게
    assert prob_of["노룩 킥"] > 0.01
    assert t[-1][3] == "선방"                                 # 부동소수 잔여 구간은 선방으로
    assert Economy.PK_MIN_BET == 1_000
    assert all("초" not in t + c for t, c in Economy.PK_SPAM_LINES)   # 도배 방지 멘트는 남은 시간을 말하지 않는다
    label = Economy._pk_label
    assert [label(Fraction(m)) for m in ("200", "1", "0", "-1", "-10")] == ["200배 수익", "1배 수익", "본전", "1배 손실", "10배 손실"]


def test_batting_table():
    t = Economy.BAT_TABLE
    assert abs(sum(r[0] for r in t) - 1) < 1e-9
    ev = sum(r[0] * float(Fraction(r[1])) for r in t)
    assert -0.02 < ev < 0, ev
    m = {r[3].rstrip("!"): r[1] for r in t}
    assert (m["장외홈런"], m["끝내기 홈런"], m["볼넷"], m["뜬공"], m["땅볼"], m["삼진"], m["병살타"], m["트리플 플레이"]) ==         ("50", "10", "0", "-1", "-1", "-2", "-5", "-10")
    assert Economy.BAT_MIN_BET == 1_000 and t[-1][3] == "땅볼" and max(t, key=lambda r: r[0])[3] == "안타"


def test_table_games():
    """야구 · 농구 · UFC: 같은 확률 구조, 연출만 다르다 · 실제 명령 흐름에서 정산이 표와 맞는다."""
    for t in (Economy.BAT_TABLE, Economy.HOOP_TABLE, Economy.UFC_TABLE):
        assert tuple(r[0] for r in t) == Economy.GAME_ODDS and tuple(r[1] for r in t) == Economy.GAME_MULTS
        assert len({r[3] for r in t}) == len(t)                    # 결과 이름이 겹치지 않는다
    from types import SimpleNamespace
    import cogs.economy as ce
    eco = Economy.__new__(Economy)
    eco.db, eco._pk_last = edb.EconomyDB(), {}
    user = SimpleNamespace(id=66, display_name="선수", display_avatar=SimpleNamespace(url="https://x/a.png"))

    async def flow():
        await eco.db.add_balance(user.id, 1_000_000)
        sent = []
        async def rec(*a, **k):
            sent.append(k.get("embed"))
            return SimpleNamespace(edit=rec)
        async def noop(*a, **k): pass
        inter = SimpleNamespace(user=user, response=SimpleNamespace(defer=noop, send_message=rec),
                                followup=SimpleNamespace(send=rec))
        real_sleep, ce.asyncio.sleep = ce.asyncio.sleep, (lambda s: real_sleep(0))
        try:
            for cmd, table in ((Economy.basketball, Economy.HOOP_TABLE), (Economy.ufc_fight, Economy.UFC_TABLE)):
                before = await eco.db.get_balance(user.id)
                eco._pk_last.clear()
                await cmd.callback(eco, inter, 10_000)
                delta = await eco.db.get_balance(user.id) - before
                row = next(r for r in table if sent[-1].title.endswith(r[3]))
                assert delta == 10_000 * int(row[1]), (delta, row)
        finally:
            ce.asyncio.sleep = real_sleep

    asyncio.run(flow())


def test_league():
    """리그: 20팀 · 순위별 배수 합 0(기대값 0) · 순위표는 승점 내림차순 · 명령 정산이 순위와 맞는다."""
    mults = [Economy.league_payout(r)[0] for r in range(1, 21)]
    assert sum(mults) == 0 and mults == sorted(mults, reverse=True) and mults[0] == 10 and mults[-1] == -3
    assert all(len(set(t)) == 20 for t in Economy.LEAGUES.values())
    rng = random.Random(3)
    for _ in range(200):
        t = Economy._league_table([f"t{i}" for i in range(20)], rng)
        pts = [r["pts"] for r in t]
        assert pts == sorted(pts, reverse=True)
        assert all(r["w"] * 3 + r["d"] == r["pts"] and r["w"] + r["d"] + r["l"] >= 0 and r["l"] >= 0 for r in t)

    from types import SimpleNamespace
    import cogs.economy as ce
    eco = Economy.__new__(Economy)
    eco.db = edb.EconomyDB()
    user = SimpleNamespace(id=77, display_name="감독", display_avatar=SimpleNamespace(url="https://x/a.png"))

    async def flow():
        await eco.db.add_balance(user.id, 1_000_000)
        sent = []
        async def rec(*a, **k):
            sent.append(k.get("embed"))
            return SimpleNamespace(edit=rec)
        async def noop(*a, **k): pass
        inter = SimpleNamespace(user=user, response=SimpleNamespace(defer=noop), followup=SimpleNamespace(send=rec))
        real_sleep, ce.asyncio.sleep = ce.asyncio.sleep, (lambda s: real_sleep(0))
        try:
            for _ in range(5):
                before = await eco.db.get_balance(user.id)
                await Economy.league.callback(eco, inter, 10_000, "라리가")
                final = sent[-1]
                rank = int(final.title.rsplit(" ", 1)[1].rstrip("위"))
                assert await eco.db.get_balance(user.id) - before == 10_000 * Economy.league_payout(rank)[0]
                assert "감독 FC** 👈" in final.description and len(sent) >= 5    # 개막 + 3장면 + 최종
        finally:
            ce.asyncio.sleep = real_sleep

    asyncio.run(flow())


def test_horse_race():
    rng = random.Random(5)
    seen = set()
    for _ in range(3000):
        p = Economy._race_prizes(rng)
        assert p[0] > p[1] > 0 > p[2] > p[3] and sum(p) == 0, p    # 1·2위 수익, 3·4위 손실, 기대값 0
        assert p[0] <= 7 and p[3] >= -7
        seen.add(tuple(p))
    assert len(seen) > 10                                           # 상금표가 경주마다 다양하다
    assert len({h[1] for h in Economy.RACE_HORSES}) == 10 and Economy.RACE_MIN_BET == 1_000
    for _ in range(300):
        finish = rng.sample(range(4), 4)
        frames = Economy._race_frames(finish, rng)
        assert len(frames) == len(Economy.RACE_CALLS)
        assert all(0 <= x <= Economy.RACE_TRACK for f in frames for x in f)
        assert [frames[-1][h] for h in finish] == [Economy.RACE_TRACK - r for r in range(4)]   # 결승선: 순위대로
        cb = finish[rng.randint(1, 3)]
        frames = Economy._race_frames(finish, rng, comeback=cb)
        for f in frames[3:5]:                                        # 4코너 · 마지막 직선은 가짜 선두가 확실히 앞선다
            assert f[cb] > max(x for i, x in enumerate(f) if i != cb), (f, cb)
        assert frames[-1][finish[0]] == Economy.RACE_TRACK             # 그리고 결승선에서 뒤집힌다

    # 출주표 → 버튼 선택 → 연출 → 결과까지 화면이 깨지지 않고 돈이 정확히 정산되는지
    from types import SimpleNamespace
    import cogs.economy as ce
    eco = Economy.__new__(Economy)
    eco.db = edb.EconomyDB()
    user = SimpleNamespace(id=55, display_name="기수", display_avatar=SimpleNamespace(url="https://x/a.png"))
    race = {"no": 3, "horses": Economy.RACE_HORSES[:4], "moods": ["🙂 좋음"] * 4, "prizes": [3, 1, -1, -3]}
    card = eco._race_card(user, 10_000, race)
    assert "+30,000원" in card.description and "-30,000원" in card.description

    async def flow():
        await eco.db.add_balance(user.id, 100_000)
        sent, views = [], []
        async def arec(*a, **k):
            sent.append(k.get("embed")); views.append(k.get("view"))
            return SimpleNamespace(edit=arec)
        async def anoop(*a, **k): pass
        inter = SimpleNamespace(user=user, response=SimpleNamespace(defer=anoop, edit_message=arec),
                                edit_original_response=arec, followup=SimpleNamespace(send=arec))
        real_sleep, ce.asyncio.sleep = ce.asyncio.sleep, (lambda s: real_sleep(0))
        try:
            # 말을 고르면: 출주표에서 베팅금 차감 → 결과에서 베팅금 + 순이익 지급 → 전체 변동 = 순이익
            await Economy.horse_race.callback(eco, inter, 10_000)
            assert await eco.db.get_balance(user.id) == 90_000
            view = views[-1]
            n = len(sent)
            await view.children[0].callback(inter)
            assert len(sent) - n == 8 and "잔액" in sent[-1].description        # 대기 + 6장면 + 결과
            played = await eco.db.get_balance(user.id) - 100_000
            assert played in {10_000 * m for m in view.race["prizes"]}, played
            await view.on_timeout()                                              # 고른 뒤엔 시간 초과가 무시된다
            assert await eco.db.get_balance(user.id) == 100_000 + played

            # 60초 동안 안 고르면: 베팅금을 잃고 화면은 실격
            await Economy.horse_race.callback(eco, inter, 10_000)
            await views[-1].on_timeout()
            assert "실격" in sent[-1].title and "-10,000원" in sent[-1].description
            assert await eco.db.get_balance(user.id) == 90_000 + played
        finally:
            ce.asyncio.sleep = real_sleep

    asyncio.run(flow())


def test_training_roll():
    eco = Economy.__new__(Economy)
    lo = min(min(e["win"][0], -e["lose"][1]) for e in Economy.TRAIN_EVENTS)
    for lv in (1, 5, 10):
        mult = Economy.train_money_mult(lv)
        assert mult == lv and isinstance(mult, int)            # Lv.N = N배, 소수점 없음
        for _ in range(2000):
            delta, xp, info = eco._train_roll(lv)
            assert isinstance(delta, int) and "rate" not in info    # 성공률은 화면용 정보에서 뺐다
            if info["ok"]:
                assert delta > 0 and xp == 3                  # 대성공 없음
            else:
                assert delta < 0 and xp == -1                 # 실패: 돈 손실 + 경험치 -1
            assert abs(delta) % mult == 0 and abs(delta) >= lo * mult
    assert Economy.train_tier(1).endswith("유스") and Economy.train_tier(10).endswith("레전드")
    assert edb.TRAIN_MAX_LEVEL == 10 and edb.TRAIN_DAILY_LIMIT == 30
    needs = [edb.train_xp_need(lv) for lv in range(1, 10)]
    assert needs == sorted(needs) and len(set(needs)) == 9     # 레벨이 오를수록 필요 경험치 증가
    assert sum(needs) >= 10_000 and needs[-1] >= 3_000         # 스카우트처럼 만렙까지 몇 달


async def _finish_scouting(db, user_id: int, ts: int):
    """훈련은 그날 스카우트 15회를 마쳐야 열린다 — 테스트에선 그날 스카우트를 다 한 것으로 만든다."""
    for i in range(edb.SCOUT_DAILY_LIMIT):
        r = await db.play_scout(user_id, ts - 3600 + 60 * i, lambda lv, con: (0, 0, None))
        assert r["ok"], r


async def _training_db():
    db = edb.EconomyDB()
    now = 1_800_000_000
    r = await db.play_training(1, now, lambda lv, con: (1000, 3, None))
    assert not r["ok"] and r["reason"] == "locked" and r["req_used"] == 0 and r["req_limit"] == 15
    await _finish_scouting(db, 1, now)
    r = await db.play_training(1, now, lambda lv, con: (-500, -1, None))
    assert r["ok"] and r["xp"] == 0 and r["new_bal"] == -500   # 경험치는 0 아래로 안 내려감
    r = await db.play_training(1, now + 10, lambda lv, con: (1000, 0, None))
    assert not r["ok"] and r["reason"] == "cooldown" and r["remaining"] == 20
    for i in range(1, 30):
        r = await db.play_training(1, now + 30 * i, lambda lv, con: (1000, 0, None))
        assert r["ok"], i
    r = await db.play_training(1, now + 30 * 30, lambda lv, con: (1000, 0, None))
    assert not r["ok"] and r["reason"] == "limit" and r["used"] == 30
    assert await db.get_balance(1) == 29_000 - 500
    # 다음 날 초기화: 다시 잠김 → 스카우트 후 경험치 몰아주기로 만렙(10)
    assert (await db.play_training(1, now + 86400, lambda lv, con: (0, 100_000, None)))["reason"] == "locked"
    await _finish_scouting(db, 1, now + 86400)
    r = await db.play_training(1, now + 86400, lambda lv, con: (0, 100_000, None))
    assert r["ok"] and r["level"] == edb.TRAIN_MAX_LEVEL == 10 and r["xp"] == 0 and r["used"] == 1
    # 필요 경험치 딱 맞으면 한 레벨만 오른다
    await _finish_scouting(db, 2, now)
    r = await db.play_training(2, now, lambda lv, con: (0, edb.train_xp_need(1), None))
    assert r["level"] == 2 and r["xp"] == 0 and r["need"] == edb.train_xp_need(2)


def test_questions_build():
    rng = random.Random(0)
    for kind in Q.KINDS:
        for item in Q._pool(kind):
            for _ in range(3):
                q = Q.build(kind, item, rng)
                if q.choices:
                    assert len(set(q.choices)) == 4 and q.choices[q.answer_idx] == q.answer
                else:
                    assert q.answer and Q.is_correct(q.answer, q)
    ids = [p["id"] for p in Q.PLAYERS] + [t["id"] for t in Q.TRIVIA]
    assert len(ids) == len(set(ids))
    assert Q.daily_question(20000).qid == Q.daily_question(20000).qid


def test_answer_matching():
    salah = Q.build("player", next(p for p in Q.PLAYERS if p["id"] == "salah"), random.Random())
    for ok in ("살라", "모하메드 살라", "Mohamed Salah", "mohamed  salah", "Mohamed Slah"):
        assert Q.is_correct(ok, salah), ok
    for bad in ("케인", "s", "", "Salah Kane Son"):
        assert not Q.is_correct(bad, salah), bad
    mbappe = Q.build("player", next(p for p in Q.PLAYERS if p["id"] == "mbappe"), random.Random())
    assert Q.is_correct("Mbappé", mbappe)


def test_score():
    assert Q.calc_score(False, 0, "hard", 1, 20, 5) == 0
    assert Q.calc_score(True, 0, "easy", 20, 20, 1) == 100
    assert Q.calc_score(True, 3, "easy", 60, 60, 1) == 40
    assert Q.calc_score(True, 0, "hard", 0, 20, 5) == 200 + 20 + 50


async def _quiz_db():
    db = Q.QuizDB()
    q = Q.build("trivia", Q.TRIVIA[0], random.Random())
    now = 1_800_000_000
    rewards = []
    for _ in range(Q.PAID_PER_DAY + 2):
        rewards.append((await db.record(7, 100, q, True, 0, 5, now))["reward"])
    assert all(rewards[:Q.PAID_PER_DAY]) and rewards[-1] == 0      # 하루 상금 한도
    res = await db.record(7, 100, q, False, 0, 5, now)
    assert res["streak"] == 0 and res["best"] == Q.PAID_PER_DAY + 2
    daily = await db.record(7, 100, q, True, 0, 5, now, daily=True)
    assert daily["reward"] > 0                                     # 오늘의 퀴즈는 한도와 별개

    assert await db.start_daily(7, now) == 1
    assert await db.start_daily(7, now) is None
    assert await db.start_daily(7, now + 86400) == 2
    assert await db.start_daily(7, now + 86400 * 3) == 1

    board = await db.leaderboard(100)
    assert board[0][0] == 7
    p = await db.profile(7, 100)
    assert p["rank"] == 1 and p["played"] == Q.PAID_PER_DAY + 4
    bal = await edb.EconomyDB().get_balance(7)
    assert bal == sum(rewards) + daily["reward"]


if __name__ == "__main__":
    test_penalty_table()
    test_batting_table()
    test_table_games()
    test_league()
    test_horse_race()
    test_training_roll()
    asyncio.run(_training_db())
    test_questions_build()
    test_answer_matching()
    test_score()
    asyncio.run(_quiz_db())
    print("OK: minigames 11 checks passed")
