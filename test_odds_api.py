"""services/odds_api.py 팀명 매칭 / h2h 추출 회귀 테스트.

실행: .venv/Scripts/python.exe test_odds_api.py
"""
from datetime import datetime

from services.odds_api import OddsAPI, _normalize, _team_sim


def _event(home, away, markets_outcomes):
    return {
        "home_team": home,
        "away_team": away,
        "commence_time": "2026-09-01T14:00:00Z",
        "bookmakers": [
            {"markets": [{"key": "h2h", "outcomes": outs}]}
            for outs in markets_outcomes
        ],
    }


def _o(name, price):
    return {"name": name, "price": price}


def test_identity_words_survive():
    # 식별어(city/united)가 살아 있어야 서로 다른 팀으로 구분된다.
    # 예전엔 둘 다 "manchester"로 붕괴해 유사도가 정확히 1.0으로 같았다.
    assert _normalize("Manchester United FC") != _normalize("Manchester City FC")
    assert (_team_sim("Manchester United FC", "Manchester United")
            > _team_sim("Manchester United FC", "Manchester City"))
    # 약어만으로 된 이름이 빈 문자열로 붕괴하지 않는다
    assert _normalize("Athletic Club") != ""
    assert _team_sim("Athletic Club", "Athletic Bilbao") > 0.3


def test_extract_h2h_basic():
    ev = _event("Arsenal FC", "Chelsea FC", [
        [_o("Arsenal FC", 2.0), _o("Draw", 3.4), _o("Chelsea FC", 3.6)],
        [_o("Arsenal FC", 2.1), _o("Draw", 3.5), _o("Chelsea FC", 3.4)],
    ])
    assert OddsAPI.extract_h2h(ev) == (2.05, 3.45, 3.5)


def test_extract_h2h_similar_names():
    # 예전엔 두 팀 다 홈으로 몰려 None이 나왔다
    ev = _event("Manchester United", "Manchester City", [
        [_o("Manchester United", 3.2), _o("Draw", 3.6), _o("Manchester City", 2.1)],
    ])
    assert OddsAPI.extract_h2h(ev) == (3.2, 3.6, 2.1)


def test_extract_h2h_outcomes_reversed():
    # 북메이커가 원정팀을 먼저 넣어도 홈/원정이 뒤집히지 않는다
    ev = _event("Real Madrid CF", "FC Barcelona", [
        [_o("FC Barcelona", 2.6), _o("Real Madrid CF", 2.4), _o("Draw", 3.5)],
    ])
    assert OddsAPI.extract_h2h(ev) == (2.4, 3.5, 2.6)


def test_extract_h2h_none_when_no_market():
    assert OddsAPI.extract_h2h(_event("A", "B", [])) is None
    # 아웃컴이 2팀 미만이면 무시
    assert OddsAPI.extract_h2h(_event("A", "B", [[_o("A", 2.0)]])) is None


def test_find_match_picks_right_fixture():
    derby = _event("Manchester United", "Manchester City", [])
    other = _event("Manchester City", "Manchester United", [])
    ts = int(datetime.fromisoformat("2026-09-01T14:00:00+00:00").timestamp())
    got = OddsAPI.find_match("Manchester United FC", "Manchester City FC", ts, [other, derby])
    assert got is derby


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
