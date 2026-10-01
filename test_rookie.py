# test_rookie.py — 2.5 뉴비 친화: 신인 부스트(7일 · 스카우트/훈련/직관 +보상 ×2 · 첫 유망주 반값) · 루키 미션
# 실행: venv/Scripts/python.exe test_rookie.py
import asyncio
import sqlite3
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import services.club_db as cdb
import services.economy_db as edb
import services.player_market_db as pmdb

TMP = Path(tempfile.mkdtemp()) / "t.sqlite3"
edb.DB_PATH = cdb.DB_PATH = pmdb.DB_PATH = TMP

import cogs.tutorial as tut  # noqa: E402

D = 86400
V, N = 1, 2   # 2.5 전부터 있던 유저 · 새 유저


def sql(q, *a):
    con = sqlite3.connect(TMP)
    rows = con.execute(q, a).fetchall()
    con.commit(); con.close()
    return rows


async def _flow():
    now = int(time.time())
    # 2.5 전부터 있던 유저: 첫 부팅 때 신인이 아닌 것(start 0)으로 남는다 — 부스트 없음
    con = sqlite3.connect(TMP)
    con.execute("CREATE TABLE wallets (user_id INTEGER PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0)")
    con.execute("INSERT INTO wallets VALUES (?, 0)", (V,))
    con.commit(); con.close()
    db, pm, clubs = edb.EconomyDB(), pmdb.PlayerMarketDB(), cdb.ClubDB()
    edb.EconomyDB()                                                               # 다시 부팅해도 새 유저를 기존 유저로 만들지 않는다
    win = lambda lv, con: (1000, 1, {"ok": True})                                 # noqa: E731
    lose = lambda lv, con: (-500, 0, {"ok": False})                               # noqa: E731
    r = await db.play_scout(V, now, win)
    assert r["delta"] == 1000 and not r["boost"]
    r = await db.play_scout(N, now, win)                                          # 새 유저: 첫 플레이부터 7일 ×2
    assert r["delta"] == 2000 and r["boost"]
    assert (await db.play_scout(N, now + 60, lose))["delta"] == -500              # 손실은 그대로
    r = await db.play_scout(N, now + 7 * D + 10, win)                              # 7일 뒤 끝
    assert r["delta"] == 1000 and not r["boost"]
    assert sql("SELECT start_ts FROM rookie WHERE user_id IN (?, ?) ORDER BY user_id", V, N) == [(0,), (now,)]
    from cogs.economy import Economy
    assert "🚀 신인 부스트 ×2" in Economy._settle_line(2000, {}, True) and "신인" not in Economy._settle_line(1, {})

    # 첫 유망주 반값은 신인 기간 · 첫 유망주만
    info, _ = cdb.prospect_input("루키", "대한민국", "ST", 9, "1-1")
    assert await clubs.prospect_price(N, now) == cdb.PROSPECT_PRICE // 2
    assert await clubs.prospect_price(N, now + 8 * D) == cdb.PROSPECT_PRICE
    assert await clubs.prospect_price(V, now) == cdb.PROSPECT_PRICE

    # 루키 미션: 진행 상황 → 깬 것만 한 번에 받기 → 다시 받기 X
    s = await db.rookie_status(N, now)
    assert [m["key"] for m in s["missions"]] == [m[0] for m in edb.ROOKIE_MISSIONS] and s["boost_until"] == now + 7 * D
    assert not any(m["done"] for m in s["missions"] if m["key"] != "watch")
    await clubs.create_club(N, "루키 FC", now)
    await clubs.record_match(N, V, 1, 0)
    for d in range(3):
        await db.claim_daily(N, 30_000, now + d * D)
    bal = await db.get_balance(N)
    r = await db.claim_rookie(N, now)
    assert {m["key"] for m in r["claimed"]} == {"club", "match", "attend"}
    assert r["money"] == 200_000 + 300_000 and r["balance"] == bal + 500_000 and r["items"] == {"muffler": 3}
    assert (await db.inventory(N))[0]["muffler"] == 3
    assert (await db.claim_rookie(N, now))["claimed"] == []                       # 한 번씩만
    s = await db.rookie_status(N, now)
    assert {m["key"] for m in s["missions"] if m["claimed"]} == {"club", "match", "attend"}

    # 나머지: 첫 직관 · 첫 선수 카드 · 공식경기 첫 승 · 유망주 데뷔(반값) · 10경기
    sql("INSERT OR REPLACE INTO spectating(user_id, last_play_ts) VALUES(?, ?)", N, now)
    pid = sql("SELECT player_id FROM pm_players WHERE player_id LIKE 'AMT_%' LIMIT 1")[0][0]
    sql("INSERT INTO pm_players(player_id, name, nation, position, age, ovr, pot, pot_grade, base_value) "
        "VALUES('900', '카드', '한국', 'FW', 20, 60, 70, 'C', 50000)")
    sql("INSERT INTO pm_holdings(user_id, player_id, qty) VALUES(?, '900', 1)", N)
    assert pid.startswith("AMT_")
    await clubs.record_official(N, V, 2, 0, now, 1_000, "W", 2.0)
    await db.add_balance(N, 10_000_000)
    bal = await db.get_balance(N)
    p = await clubs.create_prospect(N, info, now)
    assert p["price"] == cdb.PROSPECT_PRICE // 2 and p["balance"] == bal - p["price"]
    sql("UPDATE prospects SET apps=10 WHERE user_id=?", N)

    # 화면: /루키미션 (나만) → [보상 받기] → 화면 새로고침 + 받은 보상은 모두에게
    sent = []
    async def rec(*a, **k):
        sent.append(k)
    user = SimpleNamespace(id=N, display_name="뉴비", display_avatar=SimpleNamespace(url="https://x/a.png"))
    inter = SimpleNamespace(user=user, response=SimpleNamespace(send_message=rec, edit_message=rec),
                            followup=SimpleNamespace(send=rec))
    cog = tut.Tutorial.__new__(tut.Tutorial)
    cog.db = db
    await tut.Tutorial.rookie.callback(cog, inter)
    e, view = sent[-1]["embed"], sent[-1]["view"]
    assert sent[-1]["ephemeral"] and "3/8" in e.title and "🚀 **신인 부스트**" in e.description
    assert e.description.count("🎁") == 5 and view.claim.label == "보상 받기 (5개)" and not view.claim.disabled
    await view.claim.callback(inter)
    assert "8/8" in sent[-2]["embed"].title and sent[-2]["view"].claim.disabled
    assert "보상 5개" in sent[-1]["embed"].title and sent[-1].get("ephemeral") is None
    inv = (await db.inventory(N))[0]
    assert inv["scout_skip"] == inv["train_skip"] == inv["watch_skip"] == inv["watch_reset"] == 1

    # 기존 유저도 미션은 할 수 있다 (부스트만 없음) · 진행 표시
    other = SimpleNamespace(id=V, display_name="고인물", display_avatar=SimpleNamespace(url="https://x/a.png"))
    e, ready = await tut.rookie_embed(db, other)
    assert "신인 부스트" not in e.description and "(0/3)" in e.description and ready == 0   # 구단이 없으면 아직 0
    await db.claim_daily(V, 30_000, now)
    assert "(1/3)" in (await tut.rookie_embed(db, other))[0].description


def test_flow():
    asyncio.run(_flow())


if __name__ == "__main__":
    test_flow()
    print("OK: rookie")
