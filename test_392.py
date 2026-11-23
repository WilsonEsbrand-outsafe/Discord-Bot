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
    assert run(db.redenominate_392()) and run(db.redenominate_392()) is None
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
        # 환전: 100원 · 하루 1억원까지
        r = run(db.exchange(A, fid, 1000, NOW))
        assert r["ok"] and r["won"] == 100_000
        r = run(db.exchange(A, fid, 1_000_000, NOW))
        assert not r["ok"] and r["reason"] == "limit" and r["left"] == 999_000
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
