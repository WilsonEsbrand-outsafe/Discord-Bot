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
    assert Economy.PK_MIN_BET == 5_000
    assert all("초" not in t + c for t, c in Economy.PK_SPAM_LINES)   # 도배 방지 멘트는 남은 시간을 말하지 않는다
    label = Economy._pk_label
    assert [label(Fraction(m)) for m in ("200", "1", "0", "-1", "-10")] == ["200배 수익", "1배 수익", "본전", "1배 손실", "10배 손실"]


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


async def _training_db():
    db = edb.EconomyDB()
    now = 1_800_000_000
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
    # 다음 날 초기화 + 경험치 몰아주기로 만렙(10)
    r = await db.play_training(1, now + 86400, lambda lv, con: (0, 10_000, None))
    assert r["ok"] and r["level"] == edb.TRAIN_MAX_LEVEL == 10 and r["xp"] == 0 and r["used"] == 1
    # 필요 경험치 딱 맞으면 한 레벨만 오른다
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
    test_training_roll()
    asyncio.run(_training_db())
    test_questions_build()
    test_answer_matching()
    test_score()
    asyncio.run(_quiz_db())
    print("OK: minigames 7 checks passed")
