"""토토 배당 재조회 방지 회귀 테스트.

한 번 실제 배당이 붙은 경기는 다시 The Odds API 를 호출하지 않아야 한다.
실행: .venv/Scripts/python.exe test_toto_odds.py
"""
import asyncio
import tempfile
from pathlib import Path

import services.economy_db as eco_db
eco_db.DB_PATH = Path(tempfile.mkdtemp()) / "economy.sqlite3"

from services.economy_db import EconomyDB  # noqa: E402

_LOOP = asyncio.new_event_loop()
run = _LOOP.run_until_complete

KICK = 2_000_000_000   # 먼 미래


def _db():
    return EconomyDB()


def _add(db, mid):
    run(db.toto_upsert_match(match_id=mid, home="Arsenal FC", away="Chelsea FC",
                             kickoff_ts=KICK, base_home=1.4, base_draw=2.9, base_away=2.1))


def test_new_match_needs_odds():
    db = _db()
    _add(db, "m1")
    assert run(db.toto_missing_odds(["m1"])) == {"m1"}


def test_applied_odds_are_not_refetched():
    db = _db()
    _add(db, "m2")
    run(db.toto_update_base_odds("m2", 2.05, 3.45, 3.50))
    assert run(db.toto_missing_odds(["m2"])) == set()


def test_reimport_does_not_clear_the_flag():
    """자동 등록이 다시 돌아도 배당 보유 표시가 지워지면 안 된다.

    지워지면 재시작마다 유료 호출이 되살아난다.
    """
    db = _db()
    _add(db, "m3")
    run(db.toto_update_base_odds("m3", 2.0, 3.4, 3.6))
    _add(db, "m3")                                  # 재등록(upsert)
    assert run(db.toto_missing_odds(["m3"])) == set()

    row = run(db.toto_get_match("m3"))
    # row = (match_id, home, away, kickoff_ts, status, result, base_home, base_draw, base_away)
    assert (row[6], row[7], row[8]) == (2.0, 3.4, 3.6), row  # 기본값으로 덮이지 않았는지


def test_mixed_batch_returns_only_missing():
    db = _db()
    _add(db, "m4"); _add(db, "m5")
    run(db.toto_update_base_odds("m4", 1.9, 3.5, 4.0))
    assert run(db.toto_missing_odds(["m4", "m5"])) == {"m5"}


def test_empty_input_makes_no_query():
    db = _db()
    assert run(db.toto_missing_odds([])) == set()
    assert run(db.toto_missing_odds(None)) == set()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
