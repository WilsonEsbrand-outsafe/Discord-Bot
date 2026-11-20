"""3.9.1 (코드 이름 384): 출석 · 스카우트 · 훈련 · 직관 100배, 낚시 10배, 수능 버닝 낚시 효과 + 버닝 미션, 대선 11/22 21:00 개표 · 공개 때 새로 시작."""
import asyncio
import calendar
import random
import sqlite3
import tempfile
from pathlib import Path

import services.economy_db as edb

edb.DB_PATH = Path(tempfile.mkdtemp()) / "t391.sqlite3"

import release  # noqa: E402
release.RELEASE_384_TS = release.RELEASE_LOBBY_TS = None   # 공개 전(테스트 서버만) 기준으로 본다 — 공개 뒤 동작은 테스트 안에서 시각을 넣어 본다
from services import burning as B  # noqa: E402
from services import fishing as FI  # noqa: E402
from services import lobby as L  # noqa: E402

MID = calendar.timegm((2026, 11, 24, 3, 0, 0))   # 버닝 중


def run(coro):
    return asyncio.run(coro)


def _con():
    return sqlite3.connect(edb.DB_PATH)


def _bal(uid):
    con = _con()
    r = con.execute("SELECT balance FROM wallets WHERE user_id=?", (uid,)).fetchone()
    con.close()
    return r[0] if r else 0


def _in(guild):
    return release.GUILD.set(guild)


def test_pay_switch_and_hidden_command():
    tok = _in(release.TEST_GUILD)
    try:
        assert edb.pay_384("attend") == 100 and edb.pay_384("fish") == 10
    finally:
        release.GUILD.reset(tok)
    tok = _in(1)
    try:
        assert edb.pay_384("train") == 1
    finally:
        release.GUILD.reset(tok)
    assert "버닝" in release.hidden_commands(1) and "버닝" not in release.hidden_commands(release.TEST_GUILD)


def test_attend_100x_with_bonus():
    db = edb.EconomyDB()
    U = 39101
    tok = _in(release.TEST_GUILD)
    try:
        con = _con()
        con.execute("INSERT OR IGNORE INTO daily_claims(user_id, last_claim_ts, total_days) VALUES(?, 0, 6)", (U,))
        con.commit(); con.close()
        reward = 30_000 * edb.pay_384("attend")
        t = calendar.timegm((2026, 11, 20, 0, 0, 0))   # 버닝 전
        ok, bal, _, total, bonus = run(db.claim_daily(U, reward, t))
        assert ok and total == 7 and bonus == edb.ATTEND_BONUS[7] * 100 and bal == 3_000_000 + 5_000_000
    finally:
        release.GUILD.reset(tok)


def test_grind_rolls_scaled():
    from cogs.economy import Economy
    eco = Economy.__new__(Economy)
    random.seed(3)
    edb.EconomyDB()
    con = _con()
    tok = _in(release.TEST_GUILD)
    try:
        for _ in range(30):
            d, _, info = eco._train_roll(1, None, True)
            assert abs(d) >= 1000 * 100 and info["base"] % 100 == 0
            d, _, info = eco._watch_roll(1, con, 1)
            assert info.get("item") or abs(d) >= 200 * 100
    finally:
        release.GUILD.reset(tok)
        con.close()
    tok = _in(1)
    try:
        d, _, _ = eco._train_roll(1, None, True)
        assert abs(d) <= 30_000                                                    # 다른 서버는 그대로
    finally:
        release.GUILD.reset(tok)


def test_fish_price_10x():
    tok = _in(1)
    try:
        base = FI.fish_price("crucian", 25, 1.0, 1.0, 1.0)
    finally:
        release.GUILD.reset(tok)
    tok = _in(release.TEST_GUILD)
    try:
        assert FI.fish_price("crucian", 25, 1.0, 1.0, 1.0) == base * 10
    finally:
        release.GUILD.reset(tok)
    assert B.BUFFS["fish_sell"] == 1.5 and B.BUFFS["fish_xp"] == 2.0


def test_burning_missions():
    FI.FishingDB()   # fish_log 표
    db = edb.EconomyDB()
    U, V = 39111, 39112
    con = _con()
    B.bump(con, U, "attend", 1, B.START - 1)                                     # 기간 밖은 안 센다
    for _ in range(3):
        B.bump(con, U, "attend", 1, MID)
    B.bump(con, U, "harvest", 60, MID)
    for i in range(55):
        con.execute("INSERT INTO fish_log VALUES(?,?,?,?,?)", (U, "crucian", 20.0, MID + i, 0))
    con.execute("INSERT INTO fish_log VALUES(?,?,?,?,?)", (U, "crucian", 20.0, B.START - 5, 0))   # 기간 전
    con.commit(); con.close()
    B._SEEN.clear(); B.take_pending()
    for m in range(35):
        B.touch(U, MID + m * 60)
        B.touch(U, MID + m * 60 + 10)                                             # 같은 분은 한 번만
    pend = B.take_pending()
    assert pend == {U: 35}
    run(db._tx(lambda c: B.add_play(c, pend, MID)))
    rows = {m["key"]: m for m in run(db._tx(lambda c: B.status(c, U)))}
    assert rows["attend3"]["done"] and not rows["attend6"]["done"] and rows["attend3"]["value"] == 3
    assert rows["fish50"]["done"] and rows["fish200"]["value"] == 55
    assert rows["harvest50"]["done"] and rows["play30"]["done"] and not rows["play60"]["done"]

    L.TESTERS.clear()
    r = run(db._tx(lambda c: B.claim(c, U, MID)))
    assert r["claimed"] == []                                                     # 공개 전엔 테스트 서버 사람만
    L.TESTERS.add(U)
    try:
        r = run(db._tx(lambda c: B.claim(c, U, MID)))
        assert len(r["claimed"]) == 4 and r["items"] == {"scout_skip": 1, "auto_fish": 2, "watch_skip": 1}
        con = _con()
        inv = dict(con.execute("SELECT item, qty FROM inventory WHERE user_id=?", (U,)).fetchall())
        con.close()
        assert inv == {"scout_skip": 1, "auto_fish": 2, "watch_skip": 1}
        assert all(k in edb.ITEMS and k not in edb.ITEM_PRICES for m in B.MISSIONS for k in m[4])   # 상점에서 못 사는 것만
        assert run(db._tx(lambda c: B.claim(c, U, MID)))["claimed"] == []          # 한 번씩만
        assert run(db._tx(lambda c: B.claim(c, U, B.END + B.CLAIM_GRACE)))["claimed"] == []
    finally:
        L.TESTERS.clear()
    old = release.RELEASE_384_TS
    release.RELEASE_384_TS = MID - 10
    try:
        assert run(db._tx(lambda c: B.claim(c, V, MID)))["claimed"] == []          # 채운 게 없으면 없다
    finally:
        release.RELEASE_384_TS = old


def test_burning_ui_smoke():
    from cogs.burning import burning_embed

    class U:
        id, display_name, mention, guild = 1, "테스터", "<@1>", None
        class display_avatar:
            url = "https://example.com/a.png"
    rows = [{"key": k, "name": n, "what": w, "goal": g, "items": m, "value": 0, "done": False, "claimed": False}
            for k, n, w, g, m in B.MISSIONS]
    e = burning_embed(U(), rows, MID, "")
    assert e.title == B.NAME and len(e.fields) == 2 and "변호사 선임권" in e.fields[1].value and "얼티밋팩 교환권" in e.fields[1].value
    assert len(e.fields[1].value) <= 1024


def test_election_restart_at_release():
    db = L.LobbyDB()
    pre = calendar.timegm((2026, 11, 21, 0, 0, 0))   # 공개 전 (테스트 서버)
    A, Bb = 39121, 39122
    con = _con()
    for u in (A, Bb):
        con.execute("INSERT OR IGNORE INTO wallets(user_id, balance) VALUES(?, 50000000)", (u,))
    con.commit(); con.close()

    async def flow():
        s = await db.state(A, pre)
        assert s["no"] == L.FIRST_NO and s["closes_ts"] == L.FIRST_CLOSE
        assert (await db.vote(A, 0, 5, pre))["ok"]                                # 4표 = 400만원
        assert (await db.bet(Bb, 1, 2_000_000, pre))["ok"]
        assert _bal(A) == 46_000_000 and _bal(Bb) == 48_000_000
        rel = pre + 3600
        st = await db.restart(rel, random.Random(2))
        assert st["no"] == L.FIRST_NO and st["closes_ts"] == L.FIRST_CLOSE and st["refunds"] == {A: 4_000_000, Bb: 2_000_000}
        assert _bal(A) == 50_000_000 and _bal(Bb) == 50_000_000
        s = await db.state(A, rel)
        assert s["no"] == L.FIRST_NO and s["my_vote"] is None and sum(s["votes"]) == 0
        assert await db.restart(rel + 60) is None                                  # 한 번만
    run(flow())


def test_farm_cap_and_tool_price():
    import services.farm as F
    db = F.FarmDB()
    U, O = 39131, 39132
    t = MID
    L.TESTERS.clear()
    L.TESTERS.add(U)
    try:
        assert F.farm_cap(U, t) == 7 and F.farm_cap(O, t) is None                  # 공개 전엔 테스트 서버 사람만
        con = _con()
        for i in range(7):
            con.execute("INSERT INTO farms(owner, land, energy, energy_ts, plots, created_ts) VALUES(?,?,?,?,?,?)",
                        (U, "plain", 100, int(t), 6, int(t) + i))
        cur = con.execute("INSERT INTO farms(owner, land, energy, energy_ts, plots, created_ts) VALUES(?,?,?,?,?,?)",
                          (O, "plain", 100, int(t), 6, int(t)))
        other = cur.lastrowid
        mine = con.execute("SELECT id FROM farms WHERE owner=? ORDER BY id LIMIT 1", (U,)).fetchone()[0]
        con.commit(); con.close()
        r = run(db.create_more(U, "plain", mine, int(t)))
        assert not r["ok"] and r["reason"] == "max" and r["max"] == 7
        r = run(db.offer_farm(U, other, 10, int(t), mine, auto=False))
        assert not r["ok"] and r["reason"] == "max"
    finally:
        L.TESTERS.clear()
    tok = _in(release.TEST_GUILD)
    try:
        assert F.shop_price("drink") == F.TOOLS["drink"][1] * 5 and F.tool_price("fert", False) == F.TOOLS["fert"][1]
    finally:
        release.GUILD.reset(tok)
