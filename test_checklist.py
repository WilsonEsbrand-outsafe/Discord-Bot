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


if __name__ == "__main__":
    test_flow()
    print("OK: checklist")
