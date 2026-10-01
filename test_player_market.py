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
    prices = lambda: {r[0]: r[7] for r in run(pm.search_players("", limit=25))}
    before = prices()
    run(pm.run_tick_if_due(NOW_OPEN + 137))        # 위상이 안 맞아도 실행돼야 한다
    after = prices()
    assert any(before[k] != after[k] for k in before), "틱이 가격을 전혀 못 움직임"

    frozen = prices()
    run(pm.run_tick_if_due(NOW_OPEN + 200))        # 10분 안 지남 → 스킵
    assert prices() == frozen


def test_news_moves_base_value_not_just_price():
    """뉴스는 기준가를 옮겨야 평균회귀가 되돌리지 않는다."""
    eco, pm = run(_setup())
    con = pm._connect()
    try:
        bases = {r[0]: r[1] for r in con.execute(
            "SELECT player_id, base_value FROM pm_players WHERE retired=0 AND player_id NOT LIKE 'AMT_%'")}
        events = []
        for i in range(60):                        # 확률 이벤트라 여러 번 굴린다
            events += pm._roll_tick_news(con, 1000 + i)
        con.commit()
        assert events, "60틱을 굴렸는데 뉴스가 한 번도 안 터짐"
        for e in events:
            assert e["headline"] and e["price_after"] > 0
        moved = {r[0]: r[1] for r in con.execute(
            "SELECT player_id, base_value FROM pm_players WHERE retired=0 AND player_id NOT LIKE 'AMT_%'")}
    finally:
        con.close()
    changed = [k for k in bases if bases[k] != moved[k]]
    assert changed, "뉴스가 기준가를 못 바꿈"


def test_jackpot_players_are_above_pack_price():
    from services.player_market_db import JACKPOT_RANGE, _draw_from_pool
    eco, pm = run(_setup())
    con = pm._connect()
    try:
        cfg = PACKS["실버"]
        price = cfg["price"]
        status, picks = _draw_from_pool(con, cfg, price, 400)
    finally:
        con.close()
    hits = [row for row, hit in picks if hit]
    assert hits, "400장 중 잭팟이 한 번도 없음"
    for row in hits:
        assert row[1] >= price * JACKPOT_RANGE[0], (row[1], price)


def test_top_packs_stay_near_price_and_have_jackpots():
    """상위 팩(플래티넘~얼티밋): 일반 추첨은 구간 안, 잭팟은 단가보다 비싸다 (천장 없음)."""
    from services.player_market_db import _draw_from_pool
    eco, pm = run(_setup())
    con = pm._connect()
    try:
        for name in ("플래티넘", "다이아몬드", "아이콘", "얼티밋"):
            cfg = PACKS[name]
            status, picks = _draw_from_pool(con, cfg, cfg["price"], 300)
            if status != "OK":
                continue          # 스폰 풀에 해당 구간 선수가 없을 수 있다
            for row, hit in picks:
                if hit:
                    assert row[1] > cfg["price"], (name, row[1])
                else:
                    assert row[1] >= cfg["min_price"], (name, row[1])
                    assert cfg["max_price"] is None or row[1] <= cfg["max_price"], (name, row[1])
            assert cfg["min_price"] >= cfg["price"] * 0.7, name   # 단가의 70% 미만은 안 나온다
    finally:
        con.close()


def test_short_player_ids():
    """새로 스폰되는 선수 ID 는 짧은 번호다."""
    eco, pm = run(_setup())
    con = pm._connect()
    try:
        ids = [r[0] for r in con.execute(
            "SELECT player_id FROM pm_players WHERE player_id NOT LIKE 'AMT_%'")]
    finally:
        con.close()
    assert ids and all(i.isdigit() for i in ids), ids[:5]
    row = run(pm.get_player("#" + ids[0]))
    assert row and row[0] == ids[0]
    assert run(pm.get_player(row[1]))[1] == row[1]           # 이름으로도 찾는다


def test_tick_path_emits_news():
    """run_tick_if_due가 실제로 뉴스를 발생시키는지 (틱 -> 뉴스 배선 확인)."""
    eco, pm = run(_setup())
    seen = []
    ts = NOW_OPEN
    for _ in range(40):                      # 40틱이면 뉴스 0건 확률은 사실상 0
        ts += 600
        seen += run(pm.run_tick_if_due(ts))
    assert seen, "40틱을 돌렸는데 run_tick_if_due가 뉴스를 하나도 반환하지 않음"
    for e in seen:
        assert e["headline"] and e["holders"] is not None
        assert e["price_after"] != e["price_before"] or e["pct"] == 0

    # 테스트들이 임시 DB 하나를 공유하므로 다른 테스트가 남긴 행도 섞인다.
    con = pm._connect()
    try:
        stored = con.execute("SELECT COUNT(*) FROM pm_news").fetchone()[0]
    finally:
        con.close()
    assert stored >= len(seen), (stored, len(seen))


def test_prune_price_history_in_batches():
    """30일보다 오래된 시세 기록만, 배치로 나눠 지운다."""
    from services.player_market_db import PRICE_HISTORY_KEEP_DAYS
    eco, pm = run(_setup())
    con = pm._connect()
    try:
        pid = con.execute("SELECT player_id FROM pm_players WHERE retired=0 AND player_id NOT LIKE 'AMT_%' LIMIT 1").fetchone()[0]
        now = NOW_OPEN + 400 * 86400
        old_ts = [now - (PRICE_HISTORY_KEEP_DAYS + 1) * 86400 - k * 600 for k in range(2500)]
        new_ts = [now - k * 600 for k in range(100)]
        con.executemany("INSERT OR IGNORE INTO pm_price_history(player_id, price, tick_ts) VALUES(?, 1, ?)",
                        [(pid, t) for t in old_ts + new_ts])
        con.commit()
        cutoff = now - PRICE_HISTORY_KEEP_DAYS * 86400
        before_old = con.execute("SELECT COUNT(*) FROM pm_price_history WHERE tick_ts < ?", (cutoff,)).fetchone()[0]
        before_new = con.execute("SELECT COUNT(*) FROM pm_price_history WHERE tick_ts >= ?", (cutoff,)).fetchone()[0]
    finally:
        con.close()
    deleted = run(pm.prune_price_history(now, batch=1000, pause=0))
    con = pm._connect()
    try:
        left_old = con.execute("SELECT COUNT(*) FROM pm_price_history WHERE tick_ts < ?", (cutoff,)).fetchone()[0]
        left_new = con.execute("SELECT COUNT(*) FROM pm_price_history WHERE tick_ts >= ?", (cutoff,)).fetchone()[0]
    finally:
        con.close()
    assert deleted == before_old >= 2500 and left_old == 0
    assert left_new == before_new                       # 최근 기록은 그대로
    assert run(pm.prune_price_history(now)) == 0        # 두 번째는 할 일 없음


def test_position_packs_and_profile():
    """포지션 팩은 그 포지션만 · 풀 개수도 포지션별 · 체격은 선수 ID 로 고정."""
    from services.player_market_db import _draw_from_pool, player_profile
    eco, pm = run(_setup())
    counts = run(pm.count_pack_pool())
    con = pm._connect()
    try:
        total = 0
        for name, pos in (("공격수", "FW"), ("미드필더", "MF"), ("수비수", "DF"), ("골키퍼", "GK")):
            cfg = PACKS[name]
            assert cfg["pos"] == pos and cfg["price"] == PACKS["골드"]["price"]
            total += counts[name]
            status, picks = _draw_from_pool(con, cfg, cfg["price"], 200)
            if status != "OK":
                assert counts[name] == 0
                continue
            assert all(row[4] == pos for row, _ in picks), name
        assert total <= counts["골드"] and total > 0             # 골드 구간을 포지션별로 나눈 것
    finally:
        con.close()
    a, b = player_profile("123", "GK"), player_profile("123", "GK")
    assert a == b and 184 <= a["height"] <= 198 and a["foot"] in ("오른발", "왼발", "양발")
    assert 50 <= a["weight"] <= 100
    feet = {player_profile(str(i), "MF")["foot"] for i in range(200)}
    assert feet == {"오른발", "왼발", "양발"}


def test_pack_card_shows_age_and_potential():
    """팩 개봉 카드에 나이 · OVR · 잠재력(등급)까지 나온다."""
    from services.player_market_db import _draw_from_pool
    from cogs.players_market import _card_line
    eco, pm = run(_setup())
    con = pm._connect()
    try:
        cfg = PACKS["골드"]
        status, picks = _draw_from_pool(con, cfg, cfg["price"], 5)
    finally:
        con.close()
    assert status == "OK"
    for row, hit in picks:
        pid, price, name, nation, pos, ovr, age, pot, potg = row
        assert 15 <= age <= 45 and pot >= ovr and potg
        _, line = _card_line(row, cfg["price"], hit)
        assert f"{age}세" in line and f"OVR **{ovr}**" in line and f"잠재 {pot}({potg})" in line


def test_goalkeeper_share():
    """새 선수는 GK 10% · DF 32% · MF 33% · FW 25% · 예전 균등 분포(GK 25%)는 안 가진 골키퍼만 옮겨 10%로."""
    import random
    import sqlite3
    pm = PlayerMarketDB()
    random.seed(7)
    pos = [pm._spawn_player(0, 0)["position"] for _ in range(4000)]
    assert 0.08 < pos.count("GK") / 4000 < 0.12 and 0.29 < pos.count("DF") / 4000 < 0.35

    run(_setup())
    con = sqlite3.connect(_tmp)
    ids = [r[0] for r in con.execute(
        "SELECT player_id FROM pm_players WHERE retired=0 AND player_id NOT LIKE 'AMT_%' ORDER BY player_id")]
    for i, pid in enumerate(ids):                        # 2.4 이전처럼 네 포지션 균등으로 되돌린다
        con.execute("UPDATE pm_players SET position=? WHERE player_id=?", (pmdb.POSITIONS[i % 4], pid))
    held, listed = ids[0], ids[4]                         # 둘 다 GK — 누가 가진(올린) 골키퍼는 그대로 둔다
    con.execute("INSERT OR REPLACE INTO pm_holdings(user_id, player_id, qty) VALUES(9, ?, 1)", (held,))
    con.execute("INSERT INTO pm_listings(seller_id, player_id, qty, price_per, listed_at, expires_at, instant_sell_at) "
                "VALUES(9, ?, 1, 1000, 0, 9999999999, 0)", (listed,))
    con.commit()
    moved, gk, active = pmdb._rebalance_goalkeepers(con)
    con.commit()
    left = con.execute("SELECT position, COUNT(*) FROM pm_players WHERE retired=0 AND player_id NOT LIKE 'AMT_%' "
                       "GROUP BY position").fetchall()
    share = dict(left)
    assert gk == len(ids[::4]) and moved == gk - round(active * 0.10) and share["GK"] == round(active * 0.10), left
    assert all(share[p] > share["GK"] for p in ("DF", "MF", "FW"))
    assert con.execute("SELECT position FROM pm_players WHERE player_id=?", (held,)).fetchone()[0] == "GK"
    assert con.execute("SELECT position FROM pm_players WHERE player_id=?", (listed,)).fetchone()[0] == "GK"
    assert pmdb._rebalance_goalkeepers(con)[0] == 0      # 한 번 맞추면 더 안 건드린다
    con.close()


def test_news_skipped_when_market_closed():
    eco, pm = run(_setup())
    closed = 23 * 3600                        # 08:00 KST
    assert run(pm.run_tick_if_due(closed)) == []


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
