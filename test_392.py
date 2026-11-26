"""3.9.2 (코드 이름 392): 돈 버는 행동 수입 맞추기 · 송금 수수료 · 큰 돈 세금 · 숫자 1/100 · 돈 흐름 기록 · 명령어 삭제,
농사 환전 100원 · 하루 환전 한도 · 인챈트 3배 · 동물 창고 한도 · 같은 동물 5마리 · 고급 사료 · 유지비."""
import asyncio
import calendar
import sqlite3
import tempfile
from pathlib import Path

import services.economy_db as edb

edb.DB_PATH = Path(tempfile.mkdtemp()) / "t392.sqlite3"

import release  # noqa: E402
import services.farm as F  # noqa: E402
from services import lobby as L  # noqa: E402

release.RELEASE_392_TS = None
NOW = calendar.timegm((2026, 11, 23, 3, 0, 0))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _test_guild():
    """3.9.2 농사 규칙은 테스트 서버에서 온 상호작용에서만 켜진다."""
    tok = release.GUILD.set(release.TEST_GUILD)
    yield
    release.GUILD.reset(tok)


def run(coro):
    return asyncio.run(coro)


def _con():
    return sqlite3.connect(edb.DB_PATH)


def _set(uid, bal):
    con = _con()
    con.execute("INSERT OR REPLACE INTO wallets(user_id, balance) VALUES(?, ?)", (uid, bal))
    con.commit()
    con.close()


def _bal(uid):
    con = _con()
    r = con.execute("SELECT balance FROM wallets WHERE user_id=?", (uid,)).fetchone()
    con.close()
    return r[0]


def test_pay_and_hidden():
    db = edb.EconomyDB()   # noqa: F841 표 만들기
    tok = release.GUILD.set(release.TEST_GUILD)
    try:
        assert {k: edb.pay_384(k) for k in edb.PAY_392} == {"attend": 10, "scout": 8, "train": 5, "watch": 20, "fish": 1}
        from services import fishing as FI
        assert FI.fish_price("crucian", 25, 1.0, 1.0, 1.0) == FI.FISH["crucian"][4]
    finally:
        release.GUILD.reset(tok)
    tok = release.GUILD.set(1)
    try:
        assert edb.pay_384("attend") == (100 if release.released_384() else 1)   # 다른 서버는 3.9.1 그대로
    finally:
        release.GUILD.reset(tok)
    gone = {"달팽이", "조정", "양궁", "ufc", "농구", "야구"}
    assert gone <= release.hidden_commands(release.TEST_GUILD) and not gone & release.hidden_commands(1)


def test_transfer_fee_tax_redenom_and_log():
    db = edb.EconomyDB()
    A, B, C = 392000000000001, 392000000000002, 392000000000003
    _set(A, 10_000_000)
    _set(B, 0)
    tok = release.GUILD.set(release.TEST_GUILD)
    try:
        assert run(db.transfer(A, B, 1_000_000, NOW)) is None
    finally:
        release.GUILD.reset(tok)
    assert _bal(A) == 9_000_000 and _bal(B) == 950_000                        # 5% 수수료
    con = _con()
    rows = con.execute("SELECT user_id, delta FROM money_log WHERE user_id IN (?, ?) ORDER BY rowid", (A, B)).fetchall()
    con.close()
    assert (A, -1_000_000) in rows and (B, 950_000) in rows                   # 돈 흐름 기록 (트리거)
    # 큰 돈 세금: 10억 넘는 부분의 1%, 하루 한 번, 관리자 빼고
    _set(C, 3_000_000_000)
    _set(673864024030445569, 50_000_000_000)
    r = run(db.wealth_tax(NOW, {C, 673864024030445569}))
    assert r["taxed"] == [(C, 20_000_000)] and _bal(C) == 2_980_000_000 and _bal(673864024030445569) == 50_000_000_000
    assert run(db.wealth_tax(NOW + 60, {C})) is None
    # 명령어 기록
    run(db.log_commands([(A, "출석", NOW)]))
    con = _con()
    assert con.execute("SELECT name FROM cmd_log WHERE user_id=?", (A,)).fetchone()[0] == "출석"
    con.close()
    # 숫자 1/100 (빚도)
    _set(B, -12_345_678)
    con = _con()
    con.execute("INSERT OR REPLACE INTO inventory(user_id, item, qty) VALUES(?, 'reward_cash', 250000000)", (B,))
    con.commit()
    con.close()
    assert run(db.redenominate_392()) and run(db.redenominate_392()) is None
    con = _con()
    assert con.execute("SELECT qty FROM inventory WHERE user_id=? AND item='reward_cash'", (B,)).fetchone()[0] == 2_500_000
    con.execute("DELETE FROM inventory WHERE user_id=? AND item='reward_cash'", (B,))
    con.commit()
    con.close()
    assert _bal(A) == 90_000 and _bal(B) == -123_456 and _bal(C) == 29_800_000


def _farm(db, uid, **cols):
    fid = run(db.create(uid, "plain", NOW))["id"]
    con = _con()
    if cols:
        con.execute(f"UPDATE farms SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?", (*cols.values(), fid))
    con.commit()
    con.close()
    return fid


def test_farm_392():
    db = F.FarmDB()
    A = 392000000000011
    L.TESTERS.clear()
    L.TESTERS.add(A)
    try:
        assert F.rate(A) == 100 and F.rate(1) == F.RATE
        fid = _farm(db, A, energy=1000, plots=24)
        con = _con()
        con.execute("INSERT OR REPLACE INTO farm_purse(owner, coins) VALUES(?, 5000000)", (A,))
        con.commit()
        con.close()
        # 환전: 100원 · 하루 한도 없음 (11/24 규완님 '하루 환전 한도 없애줘')
        r = run(db.exchange(A, fid, 1000, NOW))
        assert r["ok"] and r["won"] == 100_000
        r = run(db.exchange(A, fid, 2000, NOW))
        assert r["ok"] and r["won"] == 200_000
        # 같은 동물 5마리까지
        con = _con()
        con.execute("INSERT INTO farm_seeds(farm_id, key, qty) VALUES(?, 'chicken', 10)", (fid,))
        con.commit()
        con.close()
        for _ in range(5):
            assert run(db.plant(A, fid, "chicken", NOW))["ok"]
        assert run(db.plant(A, fid, "chicken", NOW))["reason"] == "species"
        # 창고 한도: 오래 두어도 마리당 10개까지만 쌓인다
        f = run(db.get(fid, NOW + 100_000))
        eggs = sum(q for k, t, q in f["items"] if k == "egg")
        assert eggs == 5 * F.AUTO_CAP_392
        assert run(db.get(fid, NOW + 200_000)) and sum(q for k, t, q in run(db.get(fid, NOW + 200_000))["items"] if k == "egg") == eggs
        h = run(db.harvest(A, fid, NOW + 200_000))                                  # 거두면 다시 쌓기 시작
        assert h["ok"] and h["count"] == 5
        con = _con()
        assert con.execute("SELECT MAX(auto) FROM farm_fields WHERE farm_id=?", (fid,)).fetchone()[0] == 0
        con.close()
        # 공작은 고급 사료가 있어야 만든다
        con = _con()
        con.execute("DELETE FROM farm_fields WHERE farm_id=?", (fid,))
        con.execute("INSERT INTO farm_fields(farm_id, slot, kind, key, ready_ts) VALUES(?, 0, 'animal', 'peacock', ?)", (fid, NOW))
        con.execute("DELETE FROM farm_items WHERE farm_id=?", (fid,))
        con.commit()
        con.close()
        f = run(db.get(fid, NOW + 10_000))
        assert not [1 for k, t, q in f["items"] if k == "fan"]
        assert run(db.harvest(A, fid, NOW + 10_000))["reason"] == "nofeed"
        assert run(db.buy(A, fid, "pfeed", 3, NOW))["ok"]
        f = run(db.get(fid, NOW + 20_000))
        assert sum(q for k, t, q in f["items"] if k == "fan") == 3 and F.FarmDB._pfeed_have(_con(), A) == 0
        # 인챈트 3배
        con = _con()
        con.execute("DELETE FROM farm_items WHERE farm_id=?", (fid,))
        con.execute("INSERT INTO farm_items(farm_id, key, tier, qty) VALUES(?, 'potato_e', 1, 10)", (fid,))
        con.commit()
        con.close()
        r = run(db.sell(A, fid, NOW))
        assert r["coins"] == F.CROPS["potato"][4] * 3 * 10
        # 유지비: 농장 3개면 0 + 2만 + 4만
        for _ in range(2):
            con = _con()
            con.execute("INSERT INTO farms(owner, land, energy, energy_ts, plots, created_ts) VALUES(?, 'plain', 100, ?, 6, ?)", (A, NOW, NOW))
            con.commit()
            con.close()
        before = F.FarmDB._purse(_con(), A)
        assert run(db.pay_upkeep(NOW, {A})) == [(A, 60_000, 3)] and F.FarmDB._purse(_con(), A) == before - 60_000
        assert run(db.pay_upkeep(NOW + 60, {A})) == []
    finally:
        L.TESTERS.clear()


def test_staff():
    db = F.FarmDB()
    A = 392000000000021
    L.TESTERS.clear()
    L.TESTERS.add(A)
    try:
        fid = _farm(db, A, energy=1000, plots=6)
        con = _con()
        con.execute("INSERT OR REPLACE INTO farm_purse(owner, coins) VALUES(?, 20000)", (A,))
        con.commit()
        con.close()
        assert run(db.hire(A, fid, "skilled", NOW))["ok"] and F.FarmDB._purse(_con(), A) == 5_000
        assert run(db.staff_due(NOW)) == []                                          # 아직 차례가 아님
        d = run(db.staff_due(NOW + 10))
        assert d and d[0]["fid"] == fid and not d[0]["quit"] and "replant" in d[0]["jobs"]
        d = run(db.staff_due(NOW + 3700))                                            # 다음 시급 15,000 이 모자라 그만둔다
        assert d and d[0]["quit"] and run(db.staff(fid)) is None
    finally:
        L.TESTERS.clear()


class _Rng:
    """random 대신: 늘 같은 값."""
    def __init__(self, v):
        self.v = v

    def random(self):
        return self.v

    def choices(self, seq, weights=None):
        return [seq[0]]


def test_level_dex_orders_mutant():
    db = F.FarmDB()
    A = 392000000000031
    L.TESTERS.clear()
    L.TESTERS.add(A)
    try:
        assert F.level_of(0) == (1, 0, 100) and F.level_of(100) == (2, 0, 200) and F.level_of(10**9)[0] == F.LEVEL_MAX
        assert F.week_end(NOW) > NOW and F.week_of(F.week_end(NOW)) == F.week_of(NOW) + 1
        fid = _farm(db, A, energy=1000, plots=24)
        con = _con()
        for i in range(3):
            con.execute("INSERT INTO farm_fields(farm_id, slot, kind, key, ready_ts) VALUES(?, ?, 'crop', 'carrot', ?)", (fid, i, NOW))
        con.commit()
        con.close()
        # 희귀 변이 (rng 0.0 → 늘 나온다) · 경험치 · 도감
        h = run(db.harvest(A, fid, NOW + 10, rng=_Rng(0.0)))
        assert h["ok"] and h["mut"] == 3 and all(k == "carrot" + F.MUT for (k, t) in h["got"])
        assert run(db.level(A))["xp"] == sum(h["got"].values())
        assert "carrot" + F.MUT in run(db.dex(A))
        # 레벨이 오르면 은 상자
        con = _con()
        c0 = con.execute("SELECT c_silver FROM farms WHERE id=?", (fid,)).fetchone()[0]
        con.close()
        o = run(db.orders(A, NOW))
        assert len(o) == 3 and o == run(db.orders(A, NOW)) and [x["chest"] for x in o] == ["wood", "silver", "gold"]
        k, q = o[0]["key"], o[0]["qty"]
        assert run(db.deliver(A, fid, 0, NOW))["reason"] == "few"
        con = _con()
        con.execute("INSERT INTO farm_items(farm_id, key, tier, qty) VALUES(?, ?, 1, ?)", (fid, k, q - 1))
        con.execute("INSERT INTO farm_items(farm_id, key, tier, qty) VALUES(?, ?, 2, 5)", (fid, k))
        con.commit()
        con.close()
        r = run(db.deliver(A, fid, 0, NOW))
        assert r["ok"] and r["levelup"] == [2, 3] and run(db.deliver(A, fid, 0, NOW))["reason"] == "done"
        con = _con()
        assert con.execute("SELECT qty FROM farm_items WHERE farm_id=? AND key=? AND tier=2", (fid, k)).fetchone()[0] == 4
        assert con.execute("SELECT c_silver FROM farms WHERE id=?", (fid,)).fetchone()[0] == c0 + 2
        con.close()
    finally:
        L.TESTERS.clear()


def test_season_weather_pets_deco_trade():
    db = F.FarmDB()
    A, B = 392000000000041, 392000000000042
    L.TESTERS.clear()
    L.TESTERS.update({A, B})
    try:
        assert F.season_of(NOW)[0] in {"봄", "여름", "가을", "겨울"} and F.weather_of(NOW) == F.weather_of(NOW + 60)
        assert {F.season_of(NOW + 7 * 86400 * i)[0] for i in range(4)} == {"봄", "여름", "가을", "겨울"}
        k = F.season_of(NOW)[1][0]
        wx = F.WEATHER_GROW.get(F.weather_of(NOW), 1.0)
        assert abs(F.grow_mult(k, NOW) - F.SEASON_FAST * wx) < 1e-9
        fa, fb = _farm(db, A, energy=1000, plots=24), _farm(db, B, energy=1000, plots=24)
        # 동물 이름 · 친밀도
        con = _con()
        con.execute("INSERT INTO farm_fields(farm_id, slot, kind, key, ready_ts, love) VALUES(?, 0, 'animal', 'chicken', ?, 99)", (fa, NOW))
        con.execute("INSERT OR REPLACE INTO farm_purse(owner, coins) VALUES(?, 200000)", (A,))
        con.commit()
        con.close()
        assert run(db.name_pet(A, fa, 0, "꼬꼬", NOW))["ok"] and run(db.name_pet(A, fa, 1, "x", NOW))["reason"] == "slot"
        f = run(db.get(fa, NOW + 1))   # 저절로 만들어도 친밀도 +1
        p = f["fields"][0]
        assert p["pet"] == "꼬꼬" and p["love"] == 100
        assert p["ready_ts"] - (NOW + 1) <= int(F.ANIMALS["chicken"][4] * F.time_mult(f["res"]["grass"]) * 0.8) + 1
        # 꾸미기: 처음엔 사고, 다시 걸 땐 공짜
        assert run(db.deco(A, fa, "statue", NOW))["reason"] == "coins"
        r = run(db.deco(A, fa, "flower", NOW))
        assert r["ok"] and r["paid"] == 100_000
        assert run(db.get(fa, NOW))["deco"] == "flower" and run(db.decos(A)) == {"flower"}
        assert run(db.deco(A, fa, "flower", NOW))["paid"] == 0
    finally:
        L.TESTERS.clear()


def test_trade():
    db = F.FarmDB()
    A, B = 392000000000051, 392000000000052
    fa, fb = _farm(db, A), _farm(db, B)
    con = _con()
    con.execute("INSERT INTO farm_items(farm_id, key, tier, qty) VALUES(?, 'potato', 1, 10)", (fa,))
    con.execute("INSERT INTO farm_items(farm_id, key, tier, qty) VALUES(?, 'egg', 2, 3)", (fb,))
    con.commit()
    con.close()

    def qty(fid, key):
        c = _con()
        r = c.execute("SELECT COALESCE(SUM(qty), 0) FROM farm_items WHERE farm_id=? AND key=?", (fid, key)).fetchone()[0]
        c.close()
        return r
    assert run(db.offer_trade(A, fa, A, "potato", 1, 5, "egg", 3, NOW))["reason"] == "self"
    assert run(db.offer_trade(A, fa, B, "potato", 1, 11, "egg", 3, NOW))["reason"] == "few"
    t = run(db.offer_trade(A, fa, B, "potato", 1, 5, "egg", 3, NOW))
    assert t["ok"] and qty(fa, "potato") == 5
    assert [x["id"] for x in run(db.trades(B, NOW))["in"]] == [t["id"]]
    r = run(db.answer_trade(B, fb, t["id"], True, NOW))
    assert r["ok"] and qty(fb, "potato") == 5 and qty(fa, "egg") == 3 and qty(fb, "egg") == 0
    # 거절하면 돌아온다, 만료도
    t2 = run(db.offer_trade(A, fa, B, "potato", 1, 5, "egg", 1, NOW))
    assert qty(fa, "potato") == 0 and run(db.answer_trade(B, fb, t2["id"], False, NOW))["ok"] and qty(fa, "potato") == 5
    t3 = run(db.offer_trade(A, fa, B, "potato", 1, 2, "egg", 1, NOW))
    assert run(db.trades(A, NOW + F.TRADE_HOURS * 3600 + 10))["out"] == [] and qty(fa, "potato") == 5
    assert run(db.answer_trade(B, fb, t3["id"], True, NOW + F.TRADE_HOURS * 3600 + 20))["reason"] == "gone"


def test_help_fits():
    import cogs.farm as CF

    class U:
        id = 392000000000061
        display_name = "x"
        class display_avatar:
            url = "https://x"
        mention = "<@1>"
    L.TESTERS.clear()
    L.TESTERS.add(U.id)
    try:
        e = CF.help_embed(U(), True, True)
        assert len(e.fields) <= 25 and len(e) <= 6000
    finally:
        L.TESTERS.clear()


def test_facility_titles_emblems():
    import services.club_db as CD
    CD.DB_PATH = edb.DB_PATH
    db = CD.ClubDB()
    A = 392000000000071
    tok = release.GUILD.set(release.TEST_GUILD)
    try:
        assert CD.facility_max() == 8 and CD.facility_costs()[5] == 2_000_000_000
        assert "🏆" in CD.emblems() and CD.emblem_price("🏆") == CD.EMBLEM_PRICE_392 and CD.emblem_price("🦁") == CD.EMBLEM_PRICE
        _set(A, 150_000_000)
        assert run(db.set_title(A, "veteran", NOW))["reason"] == "balance"
        r = run(db.set_title(A, "rising", NOW))
        assert r["ok"] and r["fee"] == 100_000_000 and _bal(A) == 50_000_000
        from services import ui
        assert ui.TITLES[A] == "떠오르는 구단주"
        assert run(db.set_title(A, None, NOW))["ok"] and A not in ui.TITLES
        assert run(db.set_title(A, "rising", NOW))["fee"] == 0 and run(db.titles(A)) == {"own": {"rising"}, "now": "떠오르는 구단주"}
        # 창단 멤버는 다른 칭호를 달아도 다시 달 수 있다
        F_ = 392000000000072
        con = _con()
        con.execute("INSERT INTO user_titles(user_id, title, source, ts) VALUES(?, ?, 'founder', 0)", (F_, edb.FOUNDER_TITLE))
        con.commit()
        con.close()
        _set(F_, 100_000_000)
        assert run(db.set_title(F_, "rising", NOW))["ok"] and "founder" in run(db.titles(F_))["own"]
        assert run(db.set_title(F_, "founder", NOW))["title"] == edb.FOUNDER_TITLE
    finally:
        release.GUILD.reset(tok)
        con = _con()   # 다른 테스트(창단 멤버 칭호)가 같은 DB 를 보므로 치운다
        con.execute("DELETE FROM user_titles WHERE user_id IN (?, ?)", (A, F_))
        con.commit()
        con.close()
        ui.TITLES.pop(A, None)
        ui.TITLES.pop(F_, None)
    tok = release.GUILD.set(1)
    try:
        assert CD.facility_max() == 5 and "🏆" not in CD.emblems()
    finally:
        release.GUILD.reset(tok)


def test_league_392():
    import random as _r
    from cogs.economy import Economy as E
    w = E.league_weights_392()
    assert len(w) == 20 and abs(sum(w) - 100) < 1e-9 and w[0] == 1                     # 우승 1%
    ev = sum(p / 100 * E.league_payout(r, True, True)[0] for r, p in zip(range(1, 21), w))
    assert abs(ev + 0.05) < 1e-9                                                         # 기대값 -5%
    rng = _r.Random(5)
    for rank in (1, 10, 20):
        for _ in range(300):
            p = E.league_path_392(rank, 6, rng)
            assert all(1 <= x <= 20 for x in p) and abs(p[-1] - rank) <= 4
            assert all(abs(a - b) <= 6 for a, b in zip(p, p[1:]))                        # 한 번에 크게 뛰지 않는다
    assert "1%" in E.league_odds_text_392() and "·" not in E.league_odds_text_392()


def test_open_all_chests():
    db = F.FarmDB()
    A = 392000000000081
    fid = _farm(db, A, c_wood=3, c_silver=2, c_gold=1)
    r = run(db.open_all(A, fid, NOW))
    assert r["ok"] and r["opened"] == {"wood": 3, "silver": 2, "gold": 1} and r["rewards"]
    assert sum(run(db.get(fid, NOW))["chests"].values()) == 0 and run(db.open_all(A, fid, NOW))["reason"] == "empty"
    # 행운 열기는 돈이 되는 만큼만
    con = _con()
    con.execute("UPDATE farms SET c_wood=3 WHERE id=?", (fid,))
    con.commit()
    con.close()
    _set(A, F.LUCKY_FEE["wood"] * 2)
    r = run(db.open_all(A, fid, NOW, lucky=True))
    assert r["opened"] == {"wood": 2} and r["stop"] and _bal(A) == 0


def test_lobby_short_392():
    from types import SimpleNamespace
    from cogs import lobby as CL
    db = L.LobbyDB()
    u = SimpleNamespace(id=1, display_name="x", display_avatar=SimpleNamespace(url="https://x"), guild=SimpleNamespace(id=release.TEST_GUILD))
    s = run(db.state(39201, NOW))
    short, full = CL.lobby_embed(u, s), CL.lobby_embed(None, s)
    assert len(short) < len(full) and "당선 확률" in "".join(f.value for f in short.fields)
    for page in ("cands", "history", "news", "rules"):
        e = CL.detail_embed(u, s, page)
        assert len(e) < 6000 and "·" not in (e.description or "") + "".join(f.value for f in e.fields)


def test_v392_only_in_test_guild():
    A = 392000000000091
    L.TESTERS.clear()
    L.TESTERS.add(A)
    try:
        assert F.v392(A) and F.rate(A) == F.RATE_392
        tok = release.GUILD.set(1)   # 본 서버에서는 테스트 서버 멤버라도 예전 규칙
        try:
            assert not F.v392(A) and F.rate(A) == F.RATE
        finally:
            release.GUILD.reset(tok)
    finally:
        L.TESTERS.clear()


def test_refund_coins_once():
    db = F.FarmDB()
    O = 392000000000099
    now = 1_800_000_000
    key = f"test-refund-{O}"

    def setup(con):
        con.execute("DELETE FROM gift_once WHERE key=?", (key,)) if con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='gift_once'").fetchone() else None
        con.execute("INSERT OR REPLACE INTO farm_purse(owner, coins) VALUES(?, ?)", (O, 100))
    run(db._tx(setup))
    try:
        assert run(db.refund_coins_once(((key, O, 40),), now)) == [(O, 40)]
        assert run(db.refund_coins_once(((key, O, 40),), now)) == []
        assert run(db._tx(lambda con: con.execute("SELECT coins FROM farm_purse WHERE owner=?", (O,)).fetchone()[0])) == 140
    finally:
        def clean(con):
            con.execute("DELETE FROM farm_purse WHERE owner=?", (O,))
            con.execute("DELETE FROM gift_once WHERE key=?", (key,))
        run(db._tx(clean))


def test_dismiss_staff_once():
    db = F.FarmDB()
    O = 392000000000098
    key = f"test-staff-{O}"
    fid = _farm(db, O, coins=0)

    def setup(con):
        con.execute("DELETE FROM farm_staff")
        con.execute("DELETE FROM gift_once WHERE key=?", (key,)) if con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='gift_once'").fetchone() else None
        con.execute("INSERT INTO farm_staff(farm_id, tier, hired_ts, paid_until, next_ts, work, wages) VALUES(?, 'skilled', 0, 0, 0, 0, 45000)", (fid,))
    run(db._tx(setup))
    try:
        assert run(db.dismiss_staff_once(key, NOW)) == [(O, fid, 45000)]
        assert run(db.dismiss_staff_once(key, NOW)) == []
        assert run(db._tx(lambda con: con.execute("SELECT COUNT(*) FROM farm_staff").fetchone()[0])) == 0
        assert run(db._tx(lambda con: con.execute("SELECT coins FROM farms WHERE id=?", (fid,)).fetchone()[0])) == 45000
    finally:
        run(db._tx(lambda con: con.execute("DELETE FROM gift_once WHERE key=?", (key,))))


def test_feed_off_shop_392():
    from cogs import farm as CF
    assert "feed" not in CF._tools_for(release.TEST_GUILD) and "pfeed" in CF._tools_for(release.TEST_GUILD)
    assert "feed" in CF._tools_for(1) and "pfeed" not in CF._tools_for(1)
    db = F.FarmDB()
    O = 392000000000097
    fid = _farm(db, O, coins=10_000)
    assert run(db.buy(O, fid, "feed", 1, NOW))["reason"] == "key"
    assert run(db.buy(O, fid, "drink", 1, NOW))["ok"]


def test_refund_feed_392():
    db = F.FarmDB()
    O = 392000000000096
    fid = _farm(db, O, coins=0)

    def setup(con):
        con.execute("DELETE FROM gift_once WHERE key='refund-392-feed'") if con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='gift_once'").fetchone() else None
        con.execute("DELETE FROM farm_bag WHERE key='feed'")
        con.execute("DELETE FROM farm_seeds WHERE key='feed'")
        con.execute("INSERT INTO farm_seeds(farm_id, key, qty) VALUES(?, 'feed', 4)", (fid,))
    run(db._tx(setup))
    try:
        assert run(db.refund_feed_392(NOW)) == [(O, 4, 4 * F.tool_price("feed", True))]
        assert run(db.refund_feed_392(NOW)) is None
        assert run(db._tx(lambda con: con.execute("SELECT coins FROM farms WHERE id=?", (fid,)).fetchone()[0])) == 1000
    finally:
        run(db._tx(lambda con: con.execute("DELETE FROM gift_once WHERE key='refund-392-feed'")))


def test_legacy_392_commands():
    hid = release.hidden_commands(release.TEST_GUILD)
    assert {"2군", "유스경기", "봇구단", "달팽이"} <= hid
    assert "봇구단" not in release.hidden_commands(1)


def test_no_reserve_392():
    from services.club_db import in_reserve
    p = {"ovr": 52, "enh": 0}
    assert not in_reserve(p)
    tok = release.GUILD.set(1)
    try:
        assert in_reserve(p)
    finally:
        release.GUILD.reset(tok)


def test_dungeon_coins_392():
    import random
    import services.dungeon as D
    s = {"atk": 80, "mob": 80, "dfn": 80, "luck": 80}
    a = D.simulate(s, n=300, rng=random.Random(3), v311=True)["coins"]
    b = D.simulate(s, n=300, rng=random.Random(3), v311=True, v392=True)["coins"]
    assert abs(b / a - D.COIN_392) < 0.5


def test_restore_coins_401():
    import asyncio
    import services.farm as F
    db = F.FarmDB()
    con = F.FarmDB._con() if hasattr(F.FarmDB, "_con") else None
    owner = 730000000000000401
    async def go():
        def setup(c):
            c.execute("DELETE FROM farm_purse WHERE owner=?", (owner,))
            c.execute("INSERT INTO farm_purse(owner, coins) VALUES(?, 5)", (owner,))
            c.execute("DELETE FROM gift_once WHERE key='restore-401-coins'") if c.execute(
                "SELECT 1 FROM sqlite_master WHERE name='gift_once'").fetchone() else None
        await db._tx(setup)
        first = await db.restore_coins_401(1)
        again = await db.restore_coins_401(2)
        coins = await db._tx(lambda c: F.FarmDB._purse(c, owner))
        await db._tx(lambda c: c.execute("DELETE FROM farm_purse WHERE owner=?", (owner,)))
        return first, again, coins
    first, again, coins = asyncio.run(go())
    assert first is not None and again is None and coins == 500
