# test_checklist.py — 주간 체크리스트: 매크로 가동 보고 → 날짜별 시간 · 부풀리기 방지 · 버튼 기록 · 표 그림
# 실행: venv/Scripts/python.exe test_checklist.py
import calendar
import tempfile
from pathlib import Path

import cogs.checklist as ck

ck.DB_PATH = Path(tempfile.mkdtemp()) / "t.sqlite3"
H = 3600


def kst(y, m, d, hh=0, mm=0):
    return calendar.timegm((y, m, d, hh, mm, 0)) - ck.KST


def test_flow():
    con = ck.connect()
    mon = kst(2026, 10, 12)                       # 월요일
    week = ck.week_of(ck.kst_day(mon))
    assert week == ck.kst_day(mon) and ck.week_of(ck.kst_day(kst(2026, 10, 18, 23))) == week   # 일요일까지 같은 주

    # 을: 월 21:00 시작 → 5분마다 보고. 자정을 넘긴 시간은 화요일로
    sid = kst(2026, 10, 12, 21)
    for t in range(0, 6 * H + 1, 300):
        ck.add_runtime(con, "을", sid, t, sid + t)
    w = ck.week_data(con, week, sid + 6 * H)
    assert round(w["hours"]["을"][0], 2) == 3 and round(w["hours"]["을"][1], 2) == 3 and round(w["eul_week"], 2) == 6

    # 부풀리기: 실제로 흐른 시간보다 많이 늘어난 건 인정 안 함 · 같은 보고를 또 받아도 그대로
    now = sid + 6 * H
    assert ck.add_runtime(con, "을", sid, 6 * H + 10 * H, now + 300) <= 300 + ck.SLACK
    before = ck.week_data(con, week, now + 300)["eul_week"]
    ck.add_runtime(con, "을", sid, 6 * H + 10 * H, now + 300)
    assert ck.week_data(con, week, now + 300)["eul_week"] == before
    # 시작 시각을 과거로 속여도 지금까지 흐른 시간까지만
    fake = kst(2026, 10, 13, 10)
    assert ck.add_runtime(con, "갑", fake, 100 * H, fake + H) <= H + ck.SLACK

    # 갑: 평일 합계 · 주말은 빼고
    sat = kst(2026, 10, 17, 9)
    ck.add_runtime(con, "갑", sat, 5 * H, sat + 5 * H)
    w = ck.week_data(con, week, sat + 5 * H)
    assert round(w["gap_weekday"]) == 1 and round(sum(w["hours"]["갑"])) == 6

    con.commit()

    # 버튼: 의뢰 · 수정 요청/완료 · 취소
    cog = ck.Checklist.__new__(ck.Checklist)
    import time
    gap, byung = 1146659618790051960, 1194489261693612043
    assert ck.who(gap) == {"갑"} and ck.who(byung) == {"병"} and ck.who(1) == set()
    assert cog.record_event("fix_done", byung) == "대기 중인 수정 요청이 없어요."
    for k, u in (("commission", gap), ("commission", gap), ("fix_req", gap), ("fix_req", gap), ("fix_done", byung)):
        assert cog.record_event(k, u) == ""
    assert cog.record_event("undo", gap) == ""                                       # 마지막 수정 요청 취소
    con2 = ck.connect()
    w = ck.week_data(con2, ck.week_of(ck.kst_day(time.time())), time.time())
    assert w["commissions"] == 2 and w["fix_req"] == 1 and w["fix_done"] == 1 and w["pending"] == 0
    assert w["fix"][ck.kst_day(time.time()) - w["week"]] == "done"

    # 보고 이름 해석
    assert cog._report("가동|을|A90|%d|60" % (time.time() - 60), time.time())
    assert not cog._report("가동|병|A90|1|60", time.time()) and not cog._report("아무거나", time.time())

    # 그림 · 임베드
    png = ck.render_png(w)
    assert png[:4] == b"\x89PNG" and ck.render_png(w, final=True)[:4] == b"\x89PNG"
    assert "마감" in ck.make_embed(w, True).title


def test_ledger():
    import time
    cog = ck.Checklist.__new__(ck.Checklist)
    gap = 1146659618790051960
    # 금액 읽기 · 지분 나누기 (자투리는 갑)
    assert ck.parse_amount("1,500,000") == 1_500_000 and ck.parse_amount("150만원") == 1_500_000
    assert ck.parse_amount("12.5만") == 125_000 and ck.parse_amount("abc") is None and ck.parse_amount("0") is None
    assert ck.split(1_000_000, "egg", "갑") == {"갑": 450_000, "을": 250_000, "병": 300_000}
    assert ck.split(1_000_000, "egg", "을") == {"갑": 300_000, "을": 400_000, "병": 300_000}
    assert ck.split(1_001, "commission", "갑") == {"갑": 501, "을": 250, "병": 250}

    # 매크로 알 보고 · 직접 등록 · 의뢰 +1 → 장부
    assert cog._egg("알|갑|A34|1", time.time()) and cog._egg("알|을|A90|1", time.time())
    assert not cog._egg("알|병|X|1", time.time()) and not cog._egg("가동|갑|A34|1|0", time.time())
    assert cog.record_event("commission", gap) == ""
    d = cog.ledger()
    egg_g, egg_e, com = d["rows"][-3:]
    assert (egg_g["kind"], egg_g["source"], egg_e["source"], com["kind"]) == ("egg", "갑", "을", "commission")
    # 금액 등록 뒤엔 의뢰 체크 취소 불가 → 장부에서 삭제하면 주간 의뢰 수도 빠진다
    assert cog.ledger_set_amount(com["id"], 300_000)
    assert "기록 삭제" in cog.record_event("undo", gap)
    week = ck.week_of(ck.kst_day(time.time()))
    con = ck.connect()
    n0 = ck.week_data(con, week, time.time())["commissions"]
    con.close()
    assert cog.ledger_delete(com["id"]) and not cog.ledger_delete(com["id"])
    con = ck.connect()
    assert ck.week_data(con, week, time.time())["commissions"] == n0 - 1
    con.close()
    # 금액 등록 전 의뢰는 체크 취소로 장부에서도 빠진다
    n_com = sum(e["kind"] == "commission" for e in cog.ledger()["rows"])
    assert cog.record_event("commission", gap) == "" and cog.record_event("undo", gap) == ""
    assert sum(e["kind"] == "commission" for e in cog.ledger()["rows"]) == n_com

    # 정산: 금액 있는 것만 · 나머지는 다음으로
    assert cog.ledger_set_amount(egg_g["id"], 2_000_000) and cog.ledger_set_amount(egg_e["id"], 1_000_000)
    d = cog.ledger()
    assert d["owed"]["을"] == 500_000 + 400_000 and d["owed"]["병"] == 600_000 + 300_000
    emb = ck.ledger_embed(d)
    assert "정산하면" in emb.description and "갑 → 을 **900,000원**" in emb.description
    r = cog.ledger_settle(gap)
    assert r["count"] >= 2 and r["owed"]["을"] == 900_000 and cog.ledger_settle(gap) is None
    assert not cog.ledger_set_amount(egg_g["id"], 1) and not cog.ledger_delete(egg_g["id"])   # 정산된 건 못 고친다
    assert "마지막 정산" in ck.ledger_embed(cog.ledger()).footer.text


if __name__ == "__main__":
    test_flow()
    test_ledger()
    print("OK: checklist")
