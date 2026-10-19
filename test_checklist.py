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
    cog.record_commission(gap, "손님1", None, None, None)
    cog.record_commission(gap, "손님2", None, None, None)
    for k, u in (("fix_req", gap), ("fix_req", gap), ("fix_done", byung)):
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
    cid = cog.record_commission(gap, "홍길동", "캐릭터 육성\n3일 안에", None, "디코 DM으로 연락")
    d = cog.ledger()
    egg_g, egg_e, com = d["rows"][-3:]
    assert (egg_g["kind"], egg_g["source"], egg_e["source"], com["kind"]) == ("egg", "갑", "을", "commission")
    assert com["id"] == cid and (com["buyer"], com["amount"]) == ("홍길동", None)
    desc = ck.ledger_embed(d).description
    assert "구매자 **홍길동**" in desc and "캐릭터 육성 3일 안에" in desc and "메모 디코 DM으로 연락" in desc
    # 금액 등록 뒤엔 의뢰 체크 취소 불가 → 장부에서 삭제하면 주간 의뢰 수도 빠진다
    assert cog.ledger_update(com["id"], "홍길동", "캐릭터 육성", 300_000, None)
    assert cog.ledger()["rows"][-1]["amount"] == 300_000
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
    cog.record_commission(gap, "임시", None, None, None)
    assert cog.record_event("undo", gap) == ""
    assert sum(e["kind"] == "commission" for e in cog.ledger()["rows"]) == n_com

    # 정산: 금액 있는 것만 · 나머지는 다음으로
    assert cog.ledger_update(egg_g["id"], "알손님", None, 2_000_000, None) and cog.ledger_update(egg_e["id"], None, None, 1_000_000, None)
    d = cog.ledger()
    assert d["owed"]["을"] == 500_000 + 400_000 and d["owed"]["병"] == 600_000 + 300_000
    emb = ck.ledger_embed(d)
    assert "정산하면" in emb.description and "갑 → 을 **900,000원**" in emb.description
    r = cog.ledger_settle(gap)
    assert r["count"] >= 2 and r["owed"]["을"] == 900_000 and cog.ledger_settle(gap) is None
    assert not cog.ledger_update(egg_g["id"], None, None, 1, None) and not cog.ledger_delete(egg_g["id"])   # 정산된 건 못 고친다
    assert "마지막 정산" in ck.ledger_embed(cog.ledger()).footer.text


def test_modal():
    """[대리 등록] 입력 창 → 장부 · [금액 · 내용] 으로 고치기 · 의뢰 수만큼 주간 의뢰 수."""
    import asyncio
    from types import SimpleNamespace

    async def run():
        cog = ck.Checklist.__new__(ck.Checklist)
        sent = []

        async def rec(*a, **k):
            sent.append(a[0] if a else k)

        async def noop(*a, **k):
            pass
        cog.refresh = cog.refresh_ledger = cog.refresh_week = noop
        inter = SimpleNamespace(user=SimpleNamespace(id=1146659618790051960), response=SimpleNamespace(send_message=rec))
        m = ck.EntryModal(cog, "commission")
        assert [i.label for i in m.children] == ["구매자 (이름 · 닉네임)", "의뢰 내용", "의뢰 수 (이 대리에 든 의뢰 건수)",
                                                "받은 금액 (나중에 등록해도 돼요)", "메모"]
        assert all(len(i.label) <= 45 for i in m.children) and m.buyer.required and not m.amount.required
        import time
        week = ck.week_of(ck.kst_day(time.time()))
        count = lambda: ck.week_data(ck.connect(), week, time.time())["commissions"]   # noqa: E731
        n0 = count()
        assert m.qty.default == "1"
        m.buyer._value, m.detail._value, m.qty._value, m.amount._value, m.note._value = "김손님", "던전 클리어 대리", "3", "", ""
        await m.on_submit(inter)
        e = cog.ledger()["rows"][-1]
        assert (e["kind"], e["buyer"], e["detail"], e["amount"], e["qty"]) == ("commission", "김손님", "던전 클리어 대리", None, 3)
        assert "의뢰 3건" in sent[-1] and "의뢰 3건" in ck.entry_label(e) and count() == n0 + 3
        # 의뢰 수가 이상하면 안내만
        m = ck.EntryModal(cog, "commission")
        m.buyer._value, m.detail._value, m.qty._value, m.amount._value, m.note._value = "x", "", "0", "", ""
        await m.on_submit(inter)
        assert "의뢰 수" in sent[-1] and cog.ledger()["rows"][-1]["id"] == e["id"]
        # 금액이 이상하면 안내만
        m = ck.EntryModal(cog, "commission")
        m.buyer._value, m.detail._value, m.qty._value, m.amount._value, m.note._value = "x", "", "1", "삼만원", ""
        await m.on_submit(inter)
        assert "숫자로" in sent[-1] and cog.ledger()["rows"][-1]["id"] == e["id"]
        # 고치기: 기존 값이 미리 채워진다 · 알은 금액이 필수
        m = ck.EntryModal(cog, "commission", e)
        assert m.buyer.default == "김손님" and m.detail.default == "던전 클리어 대리" and m.qty.default == "3"
        m.buyer._value, m.detail._value, m.qty._value, m.amount._value, m.note._value = "김손님", "던전 클리어 대리", "4", "30만", "선입금"
        await m.on_submit(inter)
        e = cog.ledger()["rows"][-1]
        assert e["amount"] == 300_000 and e["qty"] == 4 and "고쳤어요" in sent[-1] and count() == n0 + 4
        egg = ck.EntryModal(cog, "egg", {"id": 999, "buyer": None, "detail": None, "amount": None, "note": None})
        assert [i.label for i in egg.children] == ["받은 금액", "구매자 (이름 · 닉네임)", "메모"] and egg.amount.required
    asyncio.run(run())


def test_bump():
    """스레드에 새 메시지가 남으면 표 · 장부가 맨 아래로 (연달아 와도 한 번) · 봇 자신 · 가동 보고는 안 옮긴다."""
    import asyncio
    import time
    from types import SimpleNamespace
    import discord

    class Msg:
        def __init__(self, th, kind):
            self.th, self.kind, self.id, self.deleted = th, kind, len(th.log) + 1, False

        async def delete(self):
            self.deleted = True

        async def edit(self, **k):
            pass

    class Thread:
        def __init__(self):
            self.log = []

        async def send(self, embed=None, file=None, view=None):
            m = Msg(self, "표" if file else ("장부" if isinstance(view, ck.LedgerView) else "기타"))
            self.log.append(m)
            return m

        async def fetch_message(self, mid):
            m = next((m for m in self.log if m.id == mid and not m.deleted), None)
            if m is None:
                raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "없음")
            return m

        def bottom(self):
            return [m.kind for m in self.log if not m.deleted][-2:]

    async def run():
        ck.BUMP_QUIET = 0.05
        th = Thread()
        cog = ck.Checklist(SimpleNamespace(user=SimpleNamespace(id=999)))

        async def thread():
            return th
        cog._thread = thread
        await cog.refresh()
        assert th.bottom() == ["표", "장부"]

        def msg(author=5, webhook=None, name="사람"):
            async def delete():
                th.log.append(SimpleNamespace(kind="삭제됨", deleted=True, id=-1))
            return SimpleNamespace(channel=SimpleNamespace(id=ck.THREAD_ID), author=SimpleNamespace(id=author, name=name),
                                   webhook_id=webhook, delete=delete)
        for _ in range(3):                                         # 채팅 3개가 연달아
            th.log.append(SimpleNamespace(kind="채팅", deleted=False, id=-1))
            await cog.on_message(msg())
        assert th.bottom() == ["채팅", "채팅"] and not cog._bumper.done()   # 아직 조용해지기 전
        await cog._bumper
        assert th.bottom() == ["표", "장부"] and sum(m.kind == "표" and not m.deleted for m in th.log) == 1
        n = len(th.log)
        await cog.on_message(msg(author=999))                     # 봇 자신은 무시
        await cog.on_message(SimpleNamespace(channel=SimpleNamespace(id=1), author=SimpleNamespace(id=5)))   # 다른 채널도
        assert len(th.log) == n and cog._bumper.done()
        sid = int(time.time()) - 60                               # 가동 보고: 지우고 표만 고친다 (안 옮김)
        await cog.on_message(msg(webhook=ck.WEBHOOK_ID, name=f"가동|을|A90|{sid}|60"))
        assert th.bottom() == ["장부", "삭제됨"] or th.bottom() == ["표", "장부"]
        assert cog._bumper.done()
        th.log.append(SimpleNamespace(kind="알 사진", deleted=False, id=-1))   # 알 보고: 장부에 적고 맨 아래로
        before = len(cog.ledger()["rows"])
        await cog.on_message(msg(webhook=ck.WEBHOOK_ID, name=f"알|갑|A34|{int(time.time())}"))
        await cog._bumper
        assert len(cog.ledger()["rows"]) == before + 1 and th.bottom() == ["표", "장부"]
        th.log.append(SimpleNamespace(kind="채팅", deleted=False, id=-1))   # 봇이 꺼져 있던 동안 올라온 채팅
        again = ck.Checklist(SimpleNamespace(user=SimpleNamespace(id=999)))   # 재시작(배포)
        again._thread = thread
        await again.refresh()
        assert th.bottom() == ["표", "장부"] and sum(m.kind == "표" and not m.deleted for m in th.log) == 1
    asyncio.run(run())


def test_gap_pass():
    """지난주(10/12 주) 갑 달성 처리 — 시간 · 의뢰가 모자라도 달성, 다른 주는 그대로."""
    from types import SimpleNamespace
    week = ck.week_of(ck.kst_day(kst(2026, 10, 12, 12)))
    con = ck.connect()
    con.execute("INSERT OR REPLACE INTO chk_messages(week, message_id, final) VALUES(?, 1, 1)", (week,))
    con.execute("DELETE FROM chk_kv WHERE key='manual_20261019'")
    con.commit()
    ck.Checklist(SimpleNamespace())
    ck.Checklist(SimpleNamespace())                                     # 두 번 켜져도 한 번만
    w = ck.week_data(con, week, kst(2026, 10, 19, 12))
    assert w["gap_pass"] and not ck.week_data(con, week + 7, kst(2026, 10, 19, 12))["gap_pass"]
    assert con.execute("SELECT final FROM chk_messages WHERE week=?", (week,)).fetchone()[0] == 0   # 다시 마감 표로 그린다
    ck.render_png(w, final=True)


if __name__ == "__main__":
    test_flow()
    test_ledger()
    test_modal()
    test_bump()
    test_gap_pass()
    print("OK: checklist")
