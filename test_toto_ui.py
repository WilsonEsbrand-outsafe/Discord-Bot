# test_toto_ui.py — /토토 · /내베팅 · /토토관리 화면 흐름 검증.  실행: venv/Scripts/python.exe test_toto_ui.py
import asyncio
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import services.economy_db as edb
import services.ufc_db as udb

TMP = Path(tempfile.mkdtemp())
edb.DB_PATH = TMP / "e.sqlite3"
udb.DB_PATH = str(TMP / "u.sqlite3")

import cogs.toto as ct  # noqa: E402
from cogs.toto import Toto  # noqa: E402

U = 42


def _inter(sent, user):
    async def rec(*a, **k):
        sent.append({**k, "content": a[0] if a else k.get("content")})
    async def noop(*a, **k):
        pass
    async def modal(m):
        sent.append({"modal": m})
    return SimpleNamespace(user=user, response=SimpleNamespace(defer=noop, send_message=rec, send_modal=modal),
                           followup=SimpleNamespace(send=rec))


async def _flow():
    cog = Toto.__new__(Toto)
    cog.db, cog.ufc, cog.api, cog.odds = edb.EconomyDB(), udb.UfcDB(), None, None
    now = int(time.time())
    await cog.db.toto_upsert_match(match_id="m1", home="아스날", away="첼시", kickoff_ts=now + 86400,
                                   base_home=1.5, base_draw=3.2, base_away=2.4)
    await cog.db.add_balance(U, 100_000)
    fight = {"event_id": "e1", "match_id": "A|B", "home": "A", "away": "B", "home_odds": 1.5, "away_odds": 2.6,
             "commence_time": "2099-01-01T00:00:00Z"}
    async def fake_fights():
        return [fight]
    ct.upcoming_fights = fake_fights

    user = SimpleNamespace(id=U, display_name="토토러", display_avatar=SimpleNamespace(url="https://x/a.png"))
    sent = []
    inter = _inter(sent, user)

    # /토토 → 축구 1 + UFC 1 경기가 메뉴에 뜬다
    await Toto.toto_list.callback(cog, inter)
    menu = sent[-1]["view"]
    assert [o.value for o in menu.select.options] == ["s:m1", "u:e1"]
    assert "아스날 vs 첼시" in sent[-1]["embed"].description and "A vs B" in sent[-1]["embed"].description

    # 축구 경기 선택 → 결과 버튼 3개 → [원정승] → 금액 입력 → 베팅 완료 (공개)
    menu.select._values = ["s:m1"]
    await menu._chosen(inter)
    picks = sent[-1]["view"]
    assert sent[-1]["ephemeral"] and len(picks.children) == 3
    await picks.children[2].callback(inter)
    modal = sent[-1]["modal"]
    modal.amount._value = "10,000"
    await modal.on_submit(inter)
    assert "베팅 완료" in sent[-1]["embed"].title and not sent[-1].get("ephemeral")
    assert await cog.db.get_balance(U) == 90_000

    # UFC 선택 → 버튼 2개 → B 에 5,000원
    menu.select._values = ["u:e1"]
    await menu._chosen(inter)
    assert len(sent[-1]["view"].children) == 2
    await sent[-1]["view"].children[1].callback(inter)
    modal = sent[-1]["modal"]
    modal.amount._value = "5000"
    await modal.on_submit(inter)
    assert "베팅 완료" in sent[-1]["embed"].title and await cog.db.get_balance(U) == 85_000
    await modal.on_submit(inter)                                    # 같은 경기 두 번은 안 된다
    assert "이미" in sent[-1]["content"] and await cog.db.get_balance(U) == 85_000

    # 숫자가 아니면 거절
    modal.amount._value = "만원"
    await modal.on_submit(inter)
    assert "숫자" in sent[-1]["content"]

    # /내베팅 → 축구·UFC 내역 + 취소 메뉴 → 축구 취소하면 전액 환불
    await Toto.my_bets.callback(cog, inter)
    d = sent[-1]["embed"].description
    assert "아스날 vs 첼시" in d and "A vs B" in d
    cancel = sent[-1]["view"]
    cancel.select._values = ["m1"]
    await cancel._cancel(inter)
    assert await cog.db.get_balance(U) == 95_000

    # /토토관리: 필요한 값이 없으면 안내, 결과 입력은 정산까지
    notified = []
    async def fake_dm(mid):
        notified.append(mid)
    cog._notify_settle_dm = fake_dm
    await Toto.manage.callback(cog, inter, "result", None)
    assert "빠졌" in sent[-1]["content"]
    await Toto.manage.callback(cog, inter, "add", "m2", None, "홈", "원정", now - 7200)
    assert await cog.db.toto_get_match("m2")


def test_flow():
    asyncio.run(_flow())


if __name__ == "__main__":
    test_flow()
    print("OK: toto ui")
