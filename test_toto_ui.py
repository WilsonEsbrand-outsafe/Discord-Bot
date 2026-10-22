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
    return SimpleNamespace(user=user, guild_id=ct.release.TEST_GUILD, response=SimpleNamespace(defer=noop, send_message=rec, send_modal=modal),
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
    async def fake_fights(ufc_only=True):
        return [fight]
    ct.upcoming_fights = fake_fights

    user = SimpleNamespace(id=U, display_name="토토러", display_avatar=SimpleNamespace(url="https://x/a.png"))
    sent = []
    inter = _inter(sent, user)

    # /토토 → 축구 1 + UFC 1 경기가 메뉴에 뜬다
    await Toto.toto_list.callback(cog, inter)
    menu = sent[-1]["view"]
    assert [[o.value for o in m.options] for m in menu.selects] == [["s:m1"], ["u:e1"]]   # 2.9.1: 종목별 메뉴
    soccer, ufc = sent[-1]["embeds"]                               # 2.9.1: 종목마다 임베드 따로 · 날짜별 묶음
    assert "아스날 vs 첼시" in soccer.description and "A vs B" in ufc.description and "📅" in soccer.description
    assert "`1.50`" in ufc.description and ufc.footer.text
    inter.guild_id, rel = 1, ct.release.RELEASE_291_TS             # 2.9.1 공개 전 서버: 예전 한 장짜리 목록
    ct.release.RELEASE_291_TS = None
    await Toto.toto_list.callback(cog, inter)
    ct.release.RELEASE_291_TS = rel
    assert "**⚽ 축구**" in sent[-1]["embed"].description and "A vs B" in sent[-1]["embed"].description
    inter.guild_id = ct.release.TEST_GUILD

    # 축구 경기 선택 → 결과 버튼 3개 → [원정승] → 금액 입력 → 베팅 완료 (공개)
    await menu._pick(inter, "s:m1")
    picks = sent[-1]["view"]
    assert sent[-1]["ephemeral"] and len(picks.children) == 3
    await picks.children[2].callback(inter)
    modal = sent[-1]["modal"]
    modal.amount._value = "10,000"
    await modal.on_submit(inter)
    assert "베팅 완료" in sent[-1]["embed"].title and not sent[-1].get("ephemeral")
    assert await cog.db.get_balance(U) == 90_000

    # UFC 선택 → 버튼 2개 → B 에 5,000원
    await menu._pick(inter, "u:e1")
    assert len(sent[-1]["view"].children) == 2
    await sent[-1]["view"].children[1].callback(inter)
    modal = sent[-1]["modal"]
    modal.amount._value = "5000"
    await modal.on_submit(inter)
    assert "베팅 완료" in sent[-1]["embed"].title and await cog.db.get_balance(U) == 85_000
    await modal.on_submit(inter)                                    # 같은 경기 두 번은 안 된다
    assert "이미" in sent[-1]["content"] and await cog.db.get_balance(U) == 85_000

    # 2.10.2: 베팅 등록 중 DB 가 터져도 '상호작용 실패' 대신 안내하고, 뺀 돈은 돌려준다
    real = cog.ufc.place_bet
    async def boom(*a, **k):
        raise RuntimeError("database is locked")
    cog.ufc.place_bet = boom
    modal.game = {**modal.game, "fight": {**modal.game["fight"], "event_id": "e2"}}
    await modal.on_submit(inter)
    assert "오류" in sent[-1]["content"] and sent[-1]["ephemeral"] and await cog.db.get_balance(U) == 85_000
    cog.ufc.place_bet = real

    # 숫자가 아니면 거절
    modal.amount._value = "만원"
    await modal.on_submit(inter)
    assert "숫자" in sent[-1]["content"]

    # /내베팅 → 축구·UFC 내역 + 취소 메뉴 → 축구 취소하면 전액 환불
    await Toto.my_bets.callback(cog, inter)
    d = sent[-1]["embed"].description
    assert "아스날 vs 첼시" in d and "A vs B" in d and not sent[-1].get("ephemeral")   # 2.10.1: 모두에게
    cancel = sent[-1]["view"]
    other = SimpleNamespace(user=SimpleNamespace(id=U + 1), response=inter.response)
    assert not await cancel.interaction_check(other)                                   # 남의 베팅은 취소 못 한다
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


def test_by_day_and_ufc_filter():
    base = 1792540800   # 2026-10-21 00:00 KST
    text = ct._by_day([(base + 3600, "a"), (base + 7200, "b"), (base + 86400 + 60, "c")])
    assert text.split("\n") == ["📅 **10/21 (수)**", "a", "b", "", "📅 **10/22 (목)**", "c"]
    from cogs.ufc_toto import _name_keys, is_ufc_fight
    names = _name_keys("José Aldo Jr.") | _name_keys("Alex Pereira")
    assert is_ufc_fight({"home": "Jose Aldo", "away": "X"}, names)                    # 악센트 · Jr 무시
    assert is_ufc_fight({"home": "Patricio Freire", "away": "A. Pereira"}, names)     # 이름 첫 글자만
    assert not is_ufc_fight({"home": "Patricio Freire", "away": "Usman Nurmagomedov"}, names)   # 다른 단체
    from cogs.ufc_toto import card_rank
    rank = {**dict.fromkeys(_name_keys("Alex Pereira"), 13), **dict.fromkeys(_name_keys("Jose Aldo"), 2)}
    assert card_rank({"home": "Alex Pereira", "away": "X"}, rank) == 13 and card_rank({"home": "A", "away": "B"}, rank) == -1
    # 대진 14경기 + 축구 15경기도 메뉴 두 개로 다 들어간다
    opts = [ct.discord.SelectOption(label=str(i), value=f"s:{i}") for i in range(15)] + \
           [ct.discord.SelectOption(label=str(i), value=f"u:{i}") for i in range(14)]
    menu = ct.GameMenu(None, {}, opts)
    assert [len(m.options) for m in menu.selects] == [15, 14]


async def _void():
    """2.9.1: 킥오프 뒤 3일이 지나도 결과가 없는(연기 · 취소) 경기는 환불하고 지운다."""
    cog = Toto.__new__(Toto)
    cog.db, cog.ufc = edb.EconomyDB(), udb.UfcDB()
    now = int(time.time())
    await cog.db.toto_upsert_match(match_id="old", home="라요", away="빌바오", kickoff_ts=now + 3600)
    await cog.db.add_balance(77, 10_000)
    assert not await cog.db.toto_place_bet(user_id=77, match_id="old", pick="1", amount=4_000, odds_locked=2.0, now_ts=now)
    con = cog.db._connect()
    con.execute("UPDATE toto_matches SET kickoff_ts=? WHERE match_id='old'", (now - 4 * 86400,))
    con.commit()
    con.close()
    await cog.db.toto_close_started(now)
    assert not (await cog.db.toto_refund_and_delete_open_match("old"))[0]        # 관리자 삭제는 여전히 open 만
    assert await cog.db.toto_list_stale(now - Toto.SETTLE_VOID_AFTER) == ["old"]
    await cog._void_match("old", "test")
    assert await cog.db.get_balance(77) == 10_000 and not await cog.db.toto_get_match("old")
    assert not await cog.db.toto_list_in_progress(now)


def test_void_stale():
    asyncio.run(_void())


def test_flow():
    asyncio.run(_flow())


if __name__ == "__main__":
    test_by_day_and_ufc_filter()
    test_void_stale()
    test_flow()
    print("OK: toto ui")
