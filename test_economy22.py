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

    # 파산: 마이너스일 때만 · 스폰서 강제 해지 원금으로 갚고 남은 빚 30~70% 랜덤 탕감 · 선수는 그대로 · 한 시간 1회
    import random as _r
    W = 5
    assert (await db.declare_bankruptcy(W, NOW))["reason"] == "not_negative"
    sp = SponsorDB()
    await sp.add_balance(W, 1_000_000)
    assert (await sp.open(W, "bank", 30, 1_000_000, NOW))["ok"]
    con = sqlite3.connect(TMP)
    con.execute("INSERT OR REPLACE INTO pm_holdings(user_id, player_id, qty) VALUES(?,?,1)", (W, pid))
    con.commit(); con.close()
    await db.add_balance(W, -5_000_000)
    r = await db.declare_bankruptcy(W, NOW, _r.Random(1))
    left = 5_000_000 - 1_000_000
    assert r["ok"] and r["sponsor"] == 1_000_000 and 0.3 <= r["rate"] <= 0.7
    assert r["forgiven"] == round(left * r["rate"]) and r["balance"] == -(left - r["forgiven"]) < 0
    assert await db.get_balance(W) == r["balance"] and await pm.get_holding(W, pid) == 1 and await sp.active(W) == []
    assert (await db.declare_bankruptcy(W, NOW + 1800))["reason"] == "cooldown"
    r2 = await db.declare_bankruptcy(W, NOW + 3600)
    assert r2["ok"] and r2["balance"] > r["balance"]                          # 한 시간 뒤 다시 → 빚이 더 줄어든다
    # 스폰서 원금이 빚보다 크면 남는 돈은 그대로 잔액으로
    await db.set_balance(W, 3_000_000)
    assert (await sp.open(W, "bank", 30, 3_000_000, NOW))["ok"]
    await db.add_balance(W, -1_000_000)
    r = await db.declare_bankruptcy(W, NOW + 7200)
    assert r["ok"] and r["forgiven"] == 0 and r["balance"] == 2_000_000


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

    # 직관: 하루 100회 · 쿨타임 10초 · 관람 80% / 이벤트 15% / 실패 5%
    # 이벤트는 돈 대신 아이템(같은 트랜잭션에서 가방으로) · 리셋권 · 스킵권은 나오지 않는다
    assert edb.WATCH_DAILY_LIMIT == 100 and Economy.WATCH_COOLDOWN == 10
    assert dict(zip(Economy.WATCH_KINDS, Economy.WATCH_ODDS)) == {"관람": 0.80, "이벤트": 0.15, "실패": 0.05}
    assert not set(Economy.WATCH_ITEM_WEIGHTS) & (set(edb.RESET_ITEMS) | set(edb.SKIP_ITEMS))
    assert set(Economy.WATCH_ITEM_WEIGHTS) <= set(Economy.WATCH_ITEM_EVENTS)
    import random as _r
    _r.seed(22)
    got, kinds = {}, {k: 0 for k in Economy.WATCH_KINDS}
    for i in range(edb.WATCH_DAILY_LIMIT):
        r = await db.play_watch(X, T + 6000 + i * 10, lambda lv, con: eco._watch_roll(lv, con, X), cooldown_sec=10)
        info = r["info"]
        assert r["ok"] and abs(info["base"]) <= 5000                            # 한 번 보상은 작다
        kinds[info["kind"]] += 1
        if info["kind"] == "이벤트":
            assert info["item"] and r["delta"] == 0 and info["ok"]              # 이벤트: 아이템만
            got[info["item"]] = got.get(info["item"], 0) + 1
        else:
            assert not info["item"] and (r["delta"] > 0) == info["ok"] == (info["kind"] == "관람")
    assert all(kinds.values()) and kinds["관람"] > kinds["이벤트"] > kinds["실패"], kinds
    mem = sqlite3.connect(":memory:")                                         # 비율 확인: 2만 번 굴리기
    mem.execute("CREATE TABLE inventory (user_id INTEGER, item TEXT, qty INTEGER, PRIMARY KEY(user_id, item))")
    n = 20_000
    rolled = [eco._watch_roll(1, mem, 1)[2]["kind"] for _ in range(n)]
    for k, p in zip(Economy.WATCH_KINDS, Economy.WATCH_ODDS):
        assert abs(rolled.count(k) / n - p) < 0.015, (k, rolled.count(k) / n)
    r = await db.play_watch(X, T + 6000 + 100 * 10, lambda lv, con: (0, 0, None), cooldown_sec=10)
    assert r["reason"] == "limit"
    inv, _ = await db.inventory(X)
    assert inv == got
    await db._tx(lambda con: con.execute("DELETE FROM inventory WHERE user_id=?", (X,)))   # 아래 테스트를 위해 비운다

    # 직관 리셋권 → 오늘 직관 +100회 · 화면은 100/200 이 아니라 0/100 부터
    await db.give_item(X, "watch_reset")
    r = await db.use_item(X, "watch_reset", T)
    assert r["extra"] == edb.WATCH_DAILY_LIMIT and r["left"] == edb.WATCH_DAILY_LIMIT
    r = await db.play_watch(X, T + 8000, lambda lv, con: (0, 0, None), cooldown_sec=10)
    assert (r["used"], r["limit"]) == (1, edb.WATCH_DAILY_LIMIT)
    assert (await db.today_status(X, T))["spectating"] == edb.WATCH_DAILY_LIMIT + 1   # 실제 횟수는 그대로 센다

    # 리셋권: 15/15 에서 쓰면 0/15 (훈련 잠금은 풀린 상태 그대로)
    await db.give_item(X, "train_reset")
    await db.give_item(X, "scout_reset")
    assert (await db.use_item(X, "train_reset", T))["left"] == edb.TRAIN_DAILY_LIMIT
    r = await db.play_training(X, T + 9000, lambda lv, con: (0, 0, None))
    assert r["ok"] and (r["used"], r["limit"]) == (1, edb.TRAIN_DAILY_LIMIT)
    # 하던 중에 쓰면 남은 횟수 전부가 새 묶음: 1/30 에서 리셋 → 0/59
    await db.give_item(X, "train_reset")
    assert (await db.use_item(X, "train_reset", T))["left"] == edb.TRAIN_DAILY_LIMIT * 2 - 1
    r = await db.play_training(X, T + 9050, lambda lv, con: (0, 0, None))
    assert (r["used"], r["limit"]) == (1, edb.TRAIN_DAILY_LIMIT * 2 - 1)
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

    # 상점: 리셋권은 팔지 않는다 · 돈을 내고 가방에 · 구매 제한 없음 · 잔액 부족
    Z = 13
    assert not set(edb.SHOP_PRICES) & set(edb.RESET_ITEMS)
    price = edb.SHOP_PRICES["muffler"]
    assert (await db.buy_item(Z, "muffler"))["reason"] == "balance"
    await db.add_balance(Z, price * 3)
    for n in (1, 2, 3):
        r = await db.buy_item(Z, "muffler")
        assert r["ok"] and r["balance"] == price * (3 - n) and r["qty"] == n
    assert (await db.buy_item(Z, "muffler"))["reason"] == "balance"
    assert price == 50_000                                                    # 머플러 가격 인하 (20만 → 5만)

    # 판매: 원가의 50% · 수량 부족 · 토토 용지는 판매 불가
    r = await db.sell_item(Z, "muffler", 2)
    assert r["ok"] and r["gain"] == price and r["each"] == price // 2 and r["left"] == 1 and r["balance"] == price
    assert (await db.sell_item(Z, "muffler", 2))["reason"] == "short"
    assert "toto_slip" not in edb.ITEM_PRICES and (await db.sell_item(Z, "toto_slip", 1))["reason"] == "unsellable"
    assert set(edb.SELL_PRICES) == set(edb.ITEMS) - {"toto_slip"}
    await db._tx(lambda con: con.execute("DELETE FROM inventory WHERE user_id=?", (Z,)))

    # 토토 용지: 가진다 → 3배 / 그대로 / -2배 · 신고 → 포상금(금액보다 적게) + 직관 경험치 / 아무것도
    import random as _r
    W = 14
    assert (await db.use_toto_slip(W, "keep"))["reason"] == "none"
    seen = {}
    for i in range(400):
        await db.give_item(W, "toto_slip")
        bal0 = await db.get_balance(W)
        r = await db.use_toto_slip(W, "keep" if i % 2 else "report", _r.Random(i))
        lo, hi = edb.TOTO_SLIP_AMOUNT
        assert r["ok"] and lo <= r["amount"] <= hi and await db.get_balance(W) - bal0 == r["delta"]
        want = {"win": 3 * r["amount"], "even": r["amount"], "illegal": -2 * r["amount"], "nothing": 0}
        if r["kind"] == "reward":
            assert 0 < r["delta"] < r["amount"] and type(r["delta"]) is int and r["xp"] == edb.TOTO_REPORT_XP
        else:
            assert r["delta"] == want[r["kind"]] and r["xp"] == 0
        seen[r["kind"]] = seen.get(r["kind"], 0) + 1
    assert set(seen) == {"win", "even", "illegal", "reward", "nothing"}, seen
    assert (await db.inventory(W))[0] == {}                                    # 쓸 때마다 1장씩 사라진다
    lv, xp = (await db._tx(lambda con: con.execute("SELECT level, xp FROM spectating WHERE user_id=?", (W,)).fetchone()))
    assert lv > 1 or xp > 0                                                    # 신고 경험치가 직관에 쌓였다

    # 쿠폰: 코드 대소문자 무시 · 계정당 한 번 · 없는 코드 · 만료
    items, expires = edb.COUPONS["PATCH22"]
    assert set(items) == {"scout_reset", "train_reset", "watch_reset"}
    assert (await db.redeem_coupon(Z, "nope", T))["reason"] == "unknown"
    r = await db.redeem_coupon(Z, " patch22 ", expires - 1)
    assert r["ok"] and (await db.inventory(Z))[0] == {"train_reset": 1, "scout_reset": 1, "watch_reset": 1}
    assert (await db.redeem_coupon(Z, "PATCH22", expires - 1))["reason"] == "used"
    assert (await db.redeem_coupon(Z + 1, "PATCH22", expires))["reason"] == "expired"


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
    assert sent[-1]["ephemeral"]                                              # 가방은 나만 보인다
    assert "× 1" in sent[-1]["embed"].description and "판매가 25,000원" in sent[-1]["embed"].description
    assert [type(c).__name__ for c in view.children] == ["Button", "Select"]   # [사용] + 💸 판매 메뉴
    assert "리셋권" not in sent[-1]["embed"].description                       # 없는 아이템은 안 보인다
    await view.children[0].callback(inter)                                    # [응원 머플러 사용]
    bag, result = sent[-2], sent[-1]
    assert bag["embed"].title == "🎒 내 가방" and "5경기" in bag["embed"].fields[0].value   # 내 가방은 새로고침
    assert "사용" in result["embed"].title and result["ephemeral"] is False            # 사용 결과는 모두에게
    await Economy.use.callback(eco, inter, "train_reset")                    # 없는 아이템 → 나만
    assert "없어요" in sent[-1]["embed"].title and sent[-1]["ephemeral"]

    # 가방에서 판매: 1개 / 전부 · 결과는 나만 · 가방 새로고침
    await db.give_item(user.id, "muffler", 3)
    await Economy.bag.callback(eco, inter)
    sell = sent[-1]["view"].sell
    assert [o.value for o in sell.options] == ["muffler:1", "muffler:3"] and "+75,000원" in sell.options[1].label
    bal0 = await db.get_balance(user.id)
    sell._values = ["muffler:3"]
    await sent[-1]["view"]._sell(inter)
    assert "3개 판매" in sent[-1]["embed"].title and sent[-1]["ephemeral"] and "가방이 비어" in sent[-2]["embed"].description
    assert await db.get_balance(user.id) - bal0 == 75_000

    # 토토 용지: [사용] → 가진다 / 신고한다 (나만) → 결과는 모두에게 · 판매 메뉴에는 없음
    await db.give_item(user.id, "toto_slip")
    await Economy.bag.callback(eco, inter)
    assert "판매 불가" in sent[-1]["embed"].description and not hasattr(sent[-1]["view"], "sell")
    await sent[-1]["view"].children[0].callback(inter)
    slip = sent[-1]["view"]
    assert sent[-1]["ephemeral"] and "가질까요" not in sent[-1]["embed"].title and "어떻게" in sent[-1]["embed"].title
    await slip.report.callback(inter)
    assert sent[-1].get("ephemeral") is None and "토토 용지" in sent[-1]["embed"].title   # 결과는 공개
    await Economy.use.callback(eco, inter, "toto_slip")                       # 다 썼으면 /사용 도 안내만
    assert "없어요" in sent[-1]["embed"].title and sent[-1]["ephemeral"]

    # 관리자: /아이템지급 (음수=회수, 0 아래로는 안 내려감)
    target = SimpleNamespace(id=22, mention="<@22>")
    await Economy.grant_item.callback(eco, inter, target, "watch_skip", 3)
    assert "지급" in sent[-1]["embed"].title and "보유 **3개**" in sent[-1]["embed"].description
    await Economy.grant_item.callback(eco, inter, target, "watch_skip", -5)
    assert "회수" in sent[-1]["embed"].title and "보유 **0개**" in sent[-1]["embed"].description
    assert "watch_skip" not in (await db.inventory(22))[0]

    # /상점: 선수팩을 고르면 몇 장 살지 메뉴가 뜨고 → 고른 장수로 개봉 · 돌아가기
    import cogs.players_market as cpm
    shop = cpm.PlayersMarket.__new__(cpm.PlayersMarket)
    shop.money = db
    bought = []
    async def fake_buy(_inter, pack, n):
        bought.append((pack, n))
    shop._buy_pack = fake_buy
    assert not hasattr(cpm.PlayersMarket, "pack")                              # /선수팩 삭제
    assert not hasattr(cpm.PlayersMarket, "pack_simulate") and not hasattr(Economy, "hole_in_one")   # /팩시뮬 · /홀인원 삭제
    for ys in ([100, 120, 90, 130], [500, 400, 450, 300], [7, 7, 7]):          # /시세 그래프: 상승 · 하락 · 평평
        png = cpm.price_chart_png("선수", "#1 · FW", [i * 600 for i in range(len(ys))], ys, 110, 24)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
    await cpm.PlayersMarket.shop.callback(shop, inter)
    view = sent[-1]["view"]
    assert not any(o.value.startswith("item:") and o.value[5:] in edb.RESET_ITEMS for o in view.menu.options)
    view.menu._values = ["pack:골드"]
    await view._buy(inter)
    qty = sent[-1]["view"]
    assert isinstance(qty, cpm.PackQtyView) and len(qty.qty.options) == 10
    assert qty.qty.options[-1].label == "10장 · 5,000,000원"
    qty.qty._values = ["3"]
    await qty._open(inter)
    assert bought == [("골드", 3)] and isinstance(sent[-1]["view"], cpm.ShopView)
    await qty.back.callback(inter)
    assert isinstance(sent[-1]["view"], cpm.ShopView)


async def _skips():
    """스킵권: 오늘 남은 횟수를 한 번에 · 결과(+/-)는 한 판씩 굴린 합 · 순서 잠금 · 다 했으면 안 쓰인다."""
    from types import SimpleNamespace
    db = edb.EconomyDB()
    eco = Economy.__new__(Economy)
    eco.db = db
    S, T = 41, int(__import__("time").time())   # 화면(_use_embed)은 실제 시각을 쓴다
    user = SimpleNamespace(id=S, display_name="스킵", display_avatar=SimpleNamespace(url="https://x/a.png"))
    e, ok = await eco._use_embed(user, "train_skip")
    assert not ok and "없어요" in e.title                                     # 아이템 없음
    await db.give_item(S, "train_skip")
    e, ok = await eco._use_embed(user, "train_skip")
    assert not ok and "스카우트" in e.description and "그대로" in e.description   # 스카우트 전엔 잠김
    assert (await db.inventory(S))[0]["train_skip"] == 1                      # 실패하면 안 쓰인다

    # 스카우트 3번 직접 → 스킵권으로 남은 12번 (쿨타임 무시) · 돈은 각 판의 합
    for i in range(3):
        assert (await db.play_scout(S, T + i * 60, lambda lv, con: (1000, 3, {"ok": True})))["ok"]
    await db.give_item(S, "scout_skip")
    bal0 = await db.get_balance(S)
    r = await db.use_skip(S, "scout_skip", T + 130, lambda lv, con: (-500, -1, {"ok": False}))
    assert r["ok"] and r["plays"] == 12 and len(r["infos"]) == 12 and r["used"] == r["limit"] == 15
    assert r["delta"] == -6000 and await db.get_balance(S) == bal0 - 6000     # 마이너스도 그대로
    await db.give_item(S, "scout_skip")
    assert (await db.use_skip(S, "scout_skip", T + 200, lambda lv, con: (0, 0, {})))["reason"] == "done"
    assert (await db.inventory(S))[0]["scout_skip"] == 1

    # 중간에 레벨이 오르면 다음 판부터 새 레벨로 굴린다
    Q = 42
    await db.give_item(Q, "scout_skip")
    seen = []
    def roll(lv, con):
        seen.append(lv)
        return 100 * lv, edb.SCOUT_XP_NEED[0], {"ok": True}
    r = await db.use_skip(Q, "scout_skip", T, roll)
    assert seen[:3] == [1, 2, 2] and r["leveled"] >= 1 and r["delta"] == 100 * sum(seen)

    # 화면: 훈련 스킵 30회 (실제 훈련 굴림) → 결과 카드 · 다음 단계 안내 / 직관 스킵 100회
    e, ok = await eco._use_embed(user, "train_skip")
    assert ok and "**30회**" in e.description and "`/직관`" in e.description and "`정산`" in e.description
    await db.give_item(S, "watch_skip")
    e, ok = await eco._use_embed(user, "watch_skip")
    assert ok and "**100회** · 관람" in e.description and "이벤트" in e.description
    assert (await db.play_watch(S, T + 9999, lambda lv, con: (0, 0, None), 0))["reason"] == "limit"


async def _tutorial():
    """튜토리얼: 체크리스트가 오늘 진행 상황을 반영 · 목차/버튼으로 모든 장을 오간다."""
    import time as _t
    from types import SimpleNamespace
    import cogs.tutorial as tut
    db = edb.EconomyDB()
    user = SimpleNamespace(id=31, display_name="뉴비", display_avatar=SimpleNamespace(url="https://x/a.png"))
    e = await tut.checklist_embed(db, user)
    assert "0/5" in e.title
    now = int(_t.time())
    await db.claim_daily(user.id, 30_000, now)
    for i in range(3):
        await db.play_scout(user.id, now + i * 60, lambda lv, con: (0, 0, None))
    e = await tut.checklist_embed(db, user)
    assert "1/5" in e.title and "스카우트 **3/15**" in e.description and "✅ 오늘 출석" in e.description

    sent = []
    async def rec(*a, **k):
        sent.append(k)
    inter = SimpleNamespace(user=user, response=SimpleNamespace(send_message=rec, edit_message=rec))
    cog = tut.Tutorial.__new__(tut.Tutorial)
    cog.db = db
    await tut.Tutorial.tutorial.callback(cog, inter)
    view = sent[-1]["view"]
    assert sent[-1]["ephemeral"] and view.prev_btn.disabled and len(view.menu.options) == len(tut.TUTORIAL_STEPS)
    for i in range(1, len(tut.TUTORIAL_STEPS)):                                   # 모든 장이 그려진다
        view.menu._values = [str(i)]
        await view._jump(inter)
        assert sent[-1]["embed"].title.endswith(tut.TUTORIAL_STEPS[i][1])
    assert sent[-1]["view"].next_btn.disabled
    await sent[-1]["view"].prev_btn.callback(inter)
    assert sent[-1]["embed"].title.endswith(tut.TUTORIAL_STEPS[-2][1])


def test_flow():
    asyncio.run(_flow())
    asyncio.run(_items())
    asyncio.run(_item_screens())
    asyncio.run(_skips())
    asyncio.run(_tutorial())


if __name__ == "__main__":
    test_settle_line_and_training()
    test_flow()
    print("OK: economy 2.2")
