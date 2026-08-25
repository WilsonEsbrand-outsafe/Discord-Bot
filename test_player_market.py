"""선수 시장 돈/보유 경로 회귀 테스트 (임시 DB, 네트워크 없음).

실행: .venv/Scripts/python.exe test_player_market.py
"""
import asyncio
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp()) / "economy.sqlite3"

import services.economy_db as economy_db
import services.player_market_db as pmdb

economy_db.DB_PATH = _tmp
pmdb.DB_PATH = _tmp
_tmp.parent.mkdir(parents=True, exist_ok=True)

from services.economy_db import EconomyDB          # noqa: E402
from services.player_market_db import PlayerMarketDB, PACKS, _take_cash, _take_holding  # noqa: E402

USER = 1111
NOW_OPEN = 0  # 09:00 KST == 00:00 UTC → 시장 오픈 시각


_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


async def _setup():
    eco = EconomyDB()
    pm = PlayerMarketDB()
    await pm.ensure_bootstrap(NOW_OPEN)
    return eco, pm


async def _any_player(pm):
    rows = await pm.search_players("", limit=5)
    assert rows, "부트스트랩 후에도 선수 풀이 비어 있음"
    return rows[0][0]


def test_market_open_hours():
    pm = PlayerMarketDB()
    assert pm._is_market_open(0)              # 09:00 KST
    assert not pm._is_market_open(23 * 3600)  # 08:00 KST


def test_take_cash_is_atomic():
    """잔액보다 많이 쓰려 하면 아무것도 차감되지 않는다."""
    eco, pm = run(_setup())
    run(eco.set_balance(USER, 1_000))
    con = pm._connect()
    try:
        assert _take_cash(con, USER, 400) is True
        assert _take_cash(con, USER, 900) is False   # 남은 600 < 900
        con.commit()
    finally:
        con.close()
    assert run(eco.get_balance(USER)) == 600


def test_take_holding_never_goes_negative():
    eco, pm = run(_setup())
    pid = run(_any_player(pm))
    con = pm._connect()
    try:
        con.execute("INSERT OR REPLACE INTO pm_holdings(user_id, player_id, qty) VALUES(?,?,2)", (USER, pid))
        assert _take_holding(con, USER, pid, 2) is True
        assert _take_holding(con, USER, pid, 1) is False   # 이미 0
        con.commit()
        qty = con.execute("SELECT qty FROM pm_holdings WHERE user_id=? AND player_id=?", (USER, pid)).fetchone()[0]
    finally:
        con.close()
    assert qty == 0, f"보유 수량이 음수로 내려감: {qty}"


def test_buy_then_sell_roundtrip():
    eco, pm = run(_setup())
    pid = run(_any_player(pm))
    price = run(pm.get_player(pid))[9]
    run(eco.set_balance(USER, price * 3))

    ok, msg = run(pm.buy_from_market(user_id=USER, player_id=pid, qty=2, now_ts=NOW_OPEN,
                                     get_balance=eco.get_balance, add_balance=eco.add_balance))
    assert ok, msg
    assert run(eco.get_balance(USER)) == price          # 3장 값 중 2장 사용
    assert run(pm.get_holding(USER, pid)) == 2

    ok, msg = run(pm.sell_to_market(user_id=USER, player_id=pid, qty=2, now_ts=NOW_OPEN,
                                    add_balance=eco.add_balance))
    assert ok, msg
    assert run(pm.get_holding(USER, pid)) == 0
    # 판매 수수료 5% 차감 후 입금
    assert run(eco.get_balance(USER)) > price


def test_buy_rejected_when_broke():
    eco, pm = run(_setup())
    pid = run(_any_player(pm))
    run(eco.set_balance(USER, 0))
    ok, msg = run(pm.buy_from_market(user_id=USER, player_id=pid, qty=1, now_ts=NOW_OPEN,
                                     get_balance=eco.get_balance, add_balance=eco.add_balance))
    assert not ok and "잔액이 부족" in msg
    assert run(eco.get_balance(USER)) == 0
    assert run(pm.get_holding(USER, pid)) == 0


def test_oversell_pays_only_once():
    """보유 2장을 3장 팔려 하면 거절되고 돈이 늘지 않는다."""
    eco, pm = run(_setup())
    pid = run(_any_player(pm))
    price = run(pm.get_player(pid))[9]
    run(eco.set_balance(USER, price * 2))
    run(pm.buy_from_market(user_id=USER, player_id=pid, qty=2, now_ts=NOW_OPEN,
                           get_balance=eco.get_balance, add_balance=eco.add_balance))
    before = run(eco.get_balance(USER))
    ok, msg = run(pm.sell_to_market(user_id=USER, player_id=pid, qty=3, now_ts=NOW_OPEN,
                                    add_balance=eco.add_balance))
    assert not ok, msg
    assert run(eco.get_balance(USER)) == before


def test_pack_purchase_is_all_or_nothing():
    eco, pm = run(_setup())
    cost = PACKS["브론즈"]["price"]
    run(eco.set_balance(USER, cost))
    ok, msg, results = run(pm.buy_pack(user_id=USER, pack_type="브론즈", pulls=1, now_ts=NOW_OPEN,
                                       get_balance=eco.get_balance, add_balance=eco.add_balance))
    assert ok, msg
    assert results and len(results) == 1
    assert run(eco.get_balance(USER)) == 0

    # 잔액 0 → 실패하고 차감도 지급도 없어야 한다
    ok, msg, results = run(pm.buy_pack(user_id=USER, pack_type="브론즈", pulls=1, now_ts=NOW_OPEN,
                                       get_balance=eco.get_balance, add_balance=eco.add_balance))
    assert not ok and results is None
    assert run(eco.get_balance(USER)) == 0


def test_listing_roundtrip_pays_seller():
    eco, pm = run(_setup())
    seller, buyer = 2222, 3333
    pid = run(_any_player(pm))
    price = run(pm.get_player(pid))[9]
    run(eco.set_balance(seller, price))
    run(pm.buy_from_market(user_id=seller, player_id=pid, qty=1, now_ts=NOW_OPEN,
                           get_balance=eco.get_balance, add_balance=eco.add_balance))

    ok, msg = run(pm.create_listing(seller_id=seller, player_id=pid, qty=1,
                                    price_per=10_000, now_ts=NOW_OPEN))
    assert ok, msg
    lid = run(pm.get_my_listings(seller))[0][0]

    run(eco.set_balance(buyer, 10_000))
    ok, msg, meta = run(pm.buy_listing(listing_id=lid, buyer_id=buyer, qty=1, now_ts=NOW_OPEN,
                                       get_balance=eco.get_balance, add_balance=eco.add_balance))
    assert ok, msg                       # 예전엔 price_per NameError로 터졌다
    assert meta["price"] == 10_000
    assert run(eco.get_balance(buyer)) == 0
    assert run(pm.get_holding(buyer, pid)) == 1
    assert run(eco.get_balance(seller)) == 10_000 - int(10_000 * 0.05)


def test_listing_buy_rejected_when_broke():
    eco, pm = run(_setup())
    seller, buyer = 4444, 5555
    pid = run(_any_player(pm))
    price = run(pm.get_player(pid))[9]
    run(eco.set_balance(seller, price))
    run(pm.buy_from_market(user_id=seller, player_id=pid, qty=1, now_ts=NOW_OPEN,
                           get_balance=eco.get_balance, add_balance=eco.add_balance))
    run(pm.create_listing(seller_id=seller, player_id=pid, qty=1, price_per=50_000, now_ts=NOW_OPEN))
    lid = run(pm.get_my_listings(seller))[0][0]

    run(eco.set_balance(buyer, 10))
    ok, msg, meta = run(pm.buy_listing(listing_id=lid, buyer_id=buyer, qty=1, now_ts=NOW_OPEN,
                                       get_balance=eco.get_balance, add_balance=eco.add_balance))
    assert not ok and "잔액이 부족" in msg
    # 매물은 그대로 남아 있어야 한다
    assert len(run(pm.get_my_listings(seller))) == 1
    assert run(pm.get_holding(buyer, pid)) == 0


def test_tick_moves_prices_regardless_of_phase():
    """예전엔 now_ts % 600 > 4 면 틱을 통째로 건너뛰었다."""
    eco, pm = run(_setup())
    assert run(pm.run_tick_if_due(NOW_OPEN + 137)) is True     # 위상이 안 맞아도 실행
    assert run(pm.run_tick_if_due(NOW_OPEN + 200)) is False    # 10분 안 지남 → 스킵
    assert run(pm.run_tick_if_due(NOW_OPEN + 800)) is True


def test_pack_pity_guarantees_jackpot():
    """천장 안에 반드시 잭팟이 한 번 나온다."""
    from services.player_market_db import JACKPOT_PITY, _draw_from_pool
    eco, pm = run(_setup())
    con = pm._connect()
    try:
        cfg = PACKS["브론즈"]
        status, picks, pity = _draw_from_pool(con, cfg, cfg["price"], JACKPOT_PITY, pity=0)
    finally:
        con.close()
    assert status == "OK"
    assert any(hit for _row, hit in picks), "천장까지 뽑았는데 잭팟이 없음"
    assert pity < JACKPOT_PITY


def test_jackpot_players_are_above_pack_price():
    from services.player_market_db import JACKPOT_RANGE, _draw_from_pool
    eco, pm = run(_setup())
    con = pm._connect()
    try:
        cfg = PACKS["실버"]
        price = cfg["price"]
        status, picks, _ = _draw_from_pool(con, cfg, price, 400, pity=0)
    finally:
        con.close()
    hits = [row for row, hit in picks if hit]
    assert hits, "400장 중 잭팟이 한 번도 없음"
    for row in hits:
        assert row[1] >= price * JACKPOT_RANGE[0], (row[1], price)


def test_pity_persists_across_pack_purchases():
    eco, pm = run(_setup())
    cost = PACKS["브론즈"]["price"]
    run(eco.set_balance(USER, cost * 3))
    before = run(pm.get_pack_pity(USER, "브론즈"))
    run(pm.buy_pack(user_id=USER, pack_type="브론즈", pulls=3, now_ts=NOW_OPEN,
                    get_balance=eco.get_balance, add_balance=eco.add_balance))
    after = run(pm.get_pack_pity(USER, "브론즈"))
    # 잭팟이 안 떴으면 3 증가, 떴으면 리셋되어 3보다 작다
    assert after in (before + 3, 0, 1, 2), (before, after)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
