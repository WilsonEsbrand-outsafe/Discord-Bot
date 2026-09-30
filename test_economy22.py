# test_economy22.py — 2.2 경제 변경 검증: 누적 출석 · 송금 한도 · 파산 · 구단 선발 선수 즉시판매 금지 · 원금 표시
# 실행: venv/Scripts/python.exe test_economy22.py
import asyncio
import sqlite3
import tempfile
from pathlib import Path

import services.club_db as cdb
import services.economy_db as edb
import services.player_market_db as pmdb

TMP = Path(tempfile.mkdtemp()) / "t.sqlite3"
edb.DB_PATH = cdb.DB_PATH = pmdb.DB_PATH = TMP

from cogs.economy import Economy  # noqa: E402
from services.sponsor_db import SponsorDB  # noqa: E402

D = 86400
NOW = 1_800_000_000 - 9 * 3600 + 3600   # KST 01:00


def test_settle_line_and_training():
    info = {"base": 12_000, "mult": 3}
    assert Economy._settle_line(36_000, info) == "`정산` **+36,000원** · 기본 +12,000원 × 3배"
    assert Economy._settle_line(-3_000, {"base": -3_000, "mult": 1}) == "`정산` **-3,000원**"
    rates = [e["success_rate"] for e in Economy.TRAIN_EVENTS]
    assert min(rates) >= 0.55 and sum(rates) / len(rates) > 0.75       # 2.2 상향


async def _flow():
    db = edb.EconomyDB()
    pm = pmdb.PlayerMarketDB()
    clubs = cdb.ClubDB()
    await pm.ensure_bootstrap(NOW)
    await pm.ensure_active_pool(NOW)

    # 출석: 누적 일수 — 하루 빠져도 이어지고, 7일째에 보너스
    U = 1
    days = [0, 1, 2, 4, 5, 6, 9]                 # 3·7·8일째는 빠짐
    for i, d in enumerate(days):
        ok, bal, _, total, bonus = await db.claim_daily(U, 30_000, NOW + d * D)
        assert ok and total == i + 1
    assert bonus == edb.ATTEND_BONUS[7] and bal == 30_000 * 7 + bonus
    assert not (await db.claim_daily(U, 30_000, NOW + 9 * D + 3600))[0]       # 같은 날 두 번 X

    # 송금: 하루 한도, 다음 날 초기화
    lim = edb.TRANSFER_DAILY_LIMIT
    await db.add_balance(2, lim * 3)
    assert await db.transfer(2, 3, lim - 1, NOW) is None
    assert "한도" in await db.transfer(2, 3, 2, NOW)
    assert await db.transfer_remaining(2, NOW) == 1
    assert await db.transfer(2, 3, lim, NOW + D) is None
    assert await db.get_balance(3) == lim * 2 - 1

    # 즉시판매: 선발 명단 카드 1장은 못 판다, 여분은 된다
    V = 4
    await clubs.create_club(V, "테스트 FC", NOW)
    await pm.give_amateur_squad(V)
    con = sqlite3.connect(TMP)
    pid = con.execute("SELECT player_id FROM pm_players WHERE retired=0 AND player_id NOT LIKE 'AMT_%' "
                      "ORDER BY ovr DESC LIMIT 1").fetchone()[0]
    con.close()
    con = sqlite3.connect(TMP)
    con.execute("INSERT OR REPLACE INTO pm_holdings(user_id, player_id, qty) VALUES(?,?,2)", (V, pid))
    con.commit(); con.close()
    assert (await clubs.set_slot(V, 10, pid))[0]
    assert pid in await pm.lineup_ids(V)
    ok, msg, _ = await pm.direct_instant_sell(user_id=V, player_id=pid, qty=2, now_ts=NOW, add_balance=db.add_balance)
    assert not ok and "선발" in msg
    ok, _, pay = await pm.direct_instant_sell(user_id=V, player_id=pid, qty=1, now_ts=NOW, add_balance=db.add_balance)
    assert ok and pay > 0 and await pm.get_holding(V, pid) == 1

    # 파산: 마이너스일 때만 · 카드와 스폰서 원금으로 갚고 남은 빚 탕감 · 30일 1회 · 3일 베팅 금지
    W = 5
    assert (await db.declare_bankruptcy(W, NOW))["reason"] == "not_negative"
    sp = SponsorDB()
    await sp.add_balance(W, 1_000_000)
    assert (await sp.open(W, "bank", 30, 1_000_000, NOW))["ok"]
    con = sqlite3.connect(TMP)
    con.execute("INSERT OR REPLACE INTO pm_holdings(user_id, player_id, qty) VALUES(?,?,1)", (W, pid))
    base = con.execute("SELECT base_value FROM pm_players WHERE player_id=?", (pid,)).fetchone()[0]
    con.commit(); con.close()
    debt = base + 5_000_000
    await db.add_balance(W, -debt)
    r = await db.declare_bankruptcy(W, NOW)
    assert r["ok"] and r["cards"] == 1 and r["cards_value"] == int(base * 0.5) and r["sponsor"] == 1_000_000
    assert r["forgiven"] == debt - int(base * 0.5) - 1_000_000 and r["balance"] == 0
    assert await db.get_balance(W) == 0 and await pm.get_holding(W, pid) == 0 and await sp.active(W) == []
    assert await db.bet_ban_until(W, NOW + D) == NOW + edb.BANKRUPT_BET_BAN
    assert await db.bet_ban_until(W, NOW + edb.BANKRUPT_BET_BAN) == 0
    await db.add_balance(W, -10)
    assert (await db.declare_bankruptcy(W, NOW + 10 * D))["reason"] == "cooldown"
    assert (await db.declare_bankruptcy(W, NOW + 31 * D))["ok"]


async def _items():
    db = edb.EconomyDB()
    eco = Economy.__new__(Economy)
    eco.db = db
    X, T = 11, NOW + 40 * D
    # 직관은 스카우트 15 → 훈련 30 을 마쳐야 열린다
    r = await db.play_watch(X, T, lambda lv, con: (0, 0, None))
    assert r["reason"] == "locked" and r["req_limit"] == edb.TRAIN_DAILY_LIMIT
    for i in range(edb.SCOUT_DAILY_LIMIT):
        assert (await db.play_scout(X, T + i * 60, lambda lv, con: (0, 0, None)))["ok"]
    for i in range(edb.TRAIN_DAILY_LIMIT):
        assert (await db.play_training(X, T + 1000 + i * 30, lambda lv, con: (0, 0, None)))["ok"]
    assert (await db.play_training(X, T + 5000, lambda lv, con: (0, 0, None)))["reason"] == "limit"

    # 직관 성공 시 아이템이 같은 트랜잭션에서 가방에 들어온다
    import cogs.economy as ce
    old = Economy.WATCH_DROP
    Economy.WATCH_DROP = (1.0,) * 5
    try:
        got = None
        for i in range(edb.WATCH_DAILY_LIMIT):
            r = await db.play_watch(X, T + 6000 + i * 60, lambda lv, con: eco._watch_roll(lv, con, X))
            assert r["ok"]
            got = got or r["info"]["item"]
        assert (await db.play_watch(X, T + 9000, lambda lv, con: (0, 0, None)))["reason"] == "limit"
    finally:
        Economy.WATCH_DROP = old
    inv, _ = await db.inventory(X)
    assert got and inv.get(got, 0) >= 1

    # 리셋권: 오늘 횟수 추가 (훈련 잠금은 유지되지 않고 풀린 상태 그대로)
    await db.give_item(X, "train_reset")
    await db.give_item(X, "scout_reset")
    assert (await db.use_item(X, "train_reset", T))["extra"] == edb.TRAIN_DAILY_LIMIT
    r = await db.play_training(X, T + 9000, lambda lv, con: (0, 0, None))
    assert r["ok"] and r["limit"] == edb.TRAIN_DAILY_LIMIT * 2
    assert (await db.use_item(X, "scout_reset", T))["ok"]
    assert (await db.play_scout(X, T + 9100, lambda lv, con: (0, 0, None)))["ok"]
    while (await db.use_item(X, "scout_reset", T))["ok"]:                 # 직관에서 더 얻었을 수도 있다
        pass
    assert (await db.inventory(X))[0].get("scout_reset", 0) == 0
    # 다음 날엔 추가 횟수가 사라진다
    assert (await db.play_scout(X, T + D, lambda lv, con: (0, 0, None)))["limit"] == edb.SCOUT_DAILY_LIMIT

    # 머플러: 5경기 효과, 경기마다 1회 소모
    Y = 12
    await db.give_item(Y, "muffler", 2)
    X = Y
    assert (await db.use_item(X, "muffler", T))["uses"] == edb.MUFFLER_USES
    assert (await db.use_item(X, "muffler", T))["uses"] == edb.MUFFLER_USES * 2      # 겹쳐 쓰면 누적
    for _ in range(edb.MUFFLER_USES * 2):
        assert await db.consume_buff(X, "muffler")
    assert not await db.consume_buff(X, "muffler")
    inv, buffs = await db.inventory(X)
    assert "muffler" not in inv and "muffler" not in buffs


async def _item_screens():
    from types import SimpleNamespace
    db = edb.EconomyDB()
    eco = Economy.__new__(Economy)
    eco.db = db
    user = SimpleNamespace(id=21, display_name="팬", display_avatar=SimpleNamespace(url="https://x/a.png"))
    sent = []
    async def rec(*a, **k):
        sent.append(k)
        return SimpleNamespace(edit=rec)
    async def noop(*a, **k): pass
    inter = SimpleNamespace(user=user, response=SimpleNamespace(defer=noop, send_message=rec, edit_message=rec),
                            followup=SimpleNamespace(send=rec))
    await Economy.watch.callback(eco, inter)                                   # 훈련 전: 잠김
    assert "잠겨" not in sent[-1]["embed"].title and "갈 수 없어요" in sent[-1]["embed"].title
    await db.give_item(user.id, "muffler")
    await Economy.bag.callback(eco, inter)
    view = sent[-1]["view"]
    assert "× 1" in sent[-1]["embed"].description and len(view.children) == 1
    await view.children[0].callback(inter)                                    # [응원 머플러 사용]
    result, bag = sent[-1]["embeds"]
    assert "사용" in result.title and bag.fields and "5경기" in bag.fields[0].value
    await Economy.use.callback(eco, inter, "train_reset")                    # 없는 아이템
    assert "없어요" in sent[-1]["embed"].title


def test_flow():
    asyncio.run(_flow())
    asyncio.run(_items())
    asyncio.run(_item_screens())


if __name__ == "__main__":
    test_settle_line_and_training()
    test_flow()
    print("OK: economy 2.2")
