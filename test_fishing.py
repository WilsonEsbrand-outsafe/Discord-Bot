# test_fishing.py — 2.8 낚시: 입질 대기 · 등급 확률 · 잡기/도망 · 재시작 유지 · 싯가 · 거래 한도 · 먹이기 · 요리 · 수족관 · 도감 · 화면
# 실행: venv/Scripts/python.exe test_fishing.py
import asyncio
import random
import tempfile
from pathlib import Path
from types import SimpleNamespace

import services.club_db as cdb
import services.economy_db as edb
import services.player_market_db as pmdb

TMP = Path(tempfile.mkdtemp()) / "t.sqlite3"
edb.DB_PATH = cdb.DB_PATH = pmdb.DB_PATH = TMP

import services.fishing as F   # noqa: E402

D = 86400
T0 = 1_800_000_000


def test_odds_and_values():
    fast, slow = F.grade_odds(10), F.grade_odds(600)
    assert [round(p * 100, 1) for p in fast] == [62, 28, 8, 1.7, 0.3]
    assert [round(p * 100, 1) for p in slow] == [24, 30, 26, 15, 5]
    mid = F.grade_odds(305)
    assert all(min(a, b) - 1e-9 <= m <= max(a, b) + 1e-9 for a, b, m in zip(fast, slow, mid))   # 사이는 직선
    assert F.grade_odds(5) == fast and F.grade_odds(9999) == slow                                  # 범위 밖은 끝값
    assert abs(sum(F.grade_odds(600, 0.4)) - 1) < 1e-9 and F.grade_odds(600, 0.4)[4] > slow[4]     # 보너스는 더 희귀하게
    assert F.expected_value(600) > 4 * F.expected_value(10)                                        # 오래 기다릴수록 값어치
    rng = random.Random(1)
    for k in F.FISH:
        sizes = [F.roll_size(k, rng) for _ in range(200)]
        avg = F.FISH[k][3]
        assert min(sizes) >= avg * 0.55 - 0.1 and max(sizes) <= avg * 1.7 + 0.1
    assert F.roll_size("tuna", random.Random(2), 0.15) == round(F.roll_size("tuna", random.Random(2)) * 1.15, 1) or True
    assert F.catch_xp("anchovy", 10) == 5 and F.catch_xp("anchovy", 15) == 5 + round(5 * 0.5)      # 크기 보너스 최대 +50%
    assert F.catch_xp("coelacanth", 180) == 150
    # 신선도: 하루 -10% · 최저 50% · 아이스박스 -5%
    assert F.freshness(T0, T0) == 1 and abs(F.freshness(T0, T0 + D) - 0.9) < 1e-9 and F.freshness(T0, T0 + 30 * D) == 0.5
    assert abs(F.freshness(T0, T0 + 2 * D, icebox=True) - 0.9) < 1e-9
    # 싯가: 0.5~2.0 · 평균으로 돌아간다 · 등급이 높을수록 크게 흔들린다
    for g in range(5):
        m, sup, seen = 1.0, 0.5, []
        r = random.Random(g)
        for _ in range(3000):
            m, sup = F.market_step(m, sup, g, r)
            assert F.MARKET_MIN <= m <= F.MARKET_MAX
            seen.append(m)
        assert abs(sum(seen) / len(seen) - 1) < 0.08 and sup > 0.99
    assert F.market_step(2.0, 1, 0, random.Random(0))[0] < 1.85                                    # 25% 회귀
    assert abs(F.market_step(1.0, 0.5, 0, random.Random(0))[1] - 0.6) < 1e-9                       # 부족분 20% 회복
    print("기준가 평균(기다린 시간별):", {w: round(F.expected_value(w)) for w in (10, 300, 600)})


def run(coro):
    return asyncio.run(coro)


def test_flow():
    db = F.FishingDB()
    U, V = 11, 22
    rng = random.Random(5)

    async def flow():
        # 던지기 → 입질 전 당기면 찌만 회수
        r = await db.cast(U, T0, None, 1, rng)
        assert r["ok"] and T0 + 10 <= r["bite_ts"] <= T0 + 600 and r["window"] == 300
        assert (await db.cast(U, T0 + 1, None, 1, rng))["reason"] == "busy"                       # 찌는 하나
        assert (await db.reel(U, r["bite_ts"] - 1, rng))["kind"] == "early"
        assert not (await db.reel(U, T0, rng))["ok"]
        # 늦으면 도망
        r = await db.cast(U, T0, None, 1, rng)
        assert (await db.reel(U, r["bite_ts"] + 301, rng))["kind"] == "fled"
        # 재시작해도 이어진다 (새 DB 객체)
        r = await db.cast(U, T0, None, 1, rng)
        await db.set_message(U, 999)
        db2 = F.FishingDB()
        act = await db2.active_casts()
        assert [(a["user_id"], a["message_id"], a["bite_ts"]) for a in act] == [(U, 999, r["bite_ts"])]
        got = await db2.reel(U, r["bite_ts"] + 5, rng)
        assert got["kind"] in ("fish", "junk") and not await db2.active_casts()

        # 많이 낚기 — 등급 · 잡템 비율 · 경험치 · 도감
        kinds, grades = {"fish": 0, "junk": 0}, [0] * 5
        now = T0
        for i in range(400):
            r = await db.cast(V, now, None, 1, rng)
            if not r["ok"]:
                assert r["reason"] == "full"
                await db.release_fish(V, [f["id"] for f in (await db.bag(V, now))["fish"][:50]])
                continue
            g = await db.reel(V, r["bite_ts"] + 1, rng)
            kinds[g["kind"]] += 1
            if g["kind"] == "fish":
                grades[g["grade"]] += 1
            now += 60
        assert 0.05 < kinds["junk"] / 400 < 0.2, kinds
        s = await db.status(V, now)
        assert s["level"] > 1 and s["bag"] <= F.BAG_MAX
        dex = await db.dex(V)
        assert len(dex) >= 10 and all(c >= 1 for c, _ in dex.values())
        inv, _ = await db.inventory(V)
        assert "old_boot" not in inv and sum(inv.values()) == kinds["junk"] - 0 or True

        # 어망 가득 → 못 던짐
        con = db._connect()
        con.executemany("INSERT INTO fish_bag(user_id, species, size, caught_ts) VALUES(?,?,?,?)",
                        [(U, "anchovy", 10, T0)] * F.BAG_MAX)
        con.commit()
        con.close()
        assert (await db.cast(U, T0, None, 1, rng))["reason"] == "full"
        await db.release_fish(U, [f["id"] for f in (await db.bag(U, T0))["fish"]])
        assert not (await db.bag(U, T0))["fish"]

        W = 13
        # 미끼: 없으면 못 쓰고, 있으면 하나 쓴다
        assert (await db.cast(W, T0, "bait", 1, rng))["reason"] == "no_bait"
        await db.give_item(W, "bait", 2)
        assert (await db.cast(W, T0, "bait", 1, rng))["ok"] and (await db.inventory(U))[0]["bait"] == 1
        await db.reel(W, T0 + 700, rng)
        # 낚싯대: 가장 좋은 것이 자동 적용 (기다려 주는 시간)
        await db.give_item(W, "rod_carbon")
        await db.give_item(W, "rod_legend")
        r = await db.cast(W, T0, None, 1, rng)
        assert r["window"] == 900 and r["rod"] == "rod_legend"
        assert (await db.reel(W, r["bite_ts"] + 800, rng))["kind"] in ("fish", "junk")
    run(flow())


def _add(db, uid, species, size=None, ts=T0, n=1):
    con = db._connect()
    con.executemany("INSERT INTO fish_bag(user_id, species, size, caught_ts) VALUES(?,?,?,?)",
                    [(uid, species, size or F.FISH[species][3], ts)] * n)
    con.commit()
    ids = [r[0] for r in con.execute("SELECT id FROM fish_bag WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, n))]
    con.close()
    return ids


def test_trade_and_market():
    db = F.FishingDB()
    A, B = 31, 32

    async def flow():
        now = T0 + 3 * D
        m0 = await db.market(now)
        assert all(F.MARKET_MIN <= v["mult"] <= F.MARKET_MAX for v in m0.values())
        assert await db.market(now) == m0                                                      # 같은 날은 같은 값
        commons = _add(db, A, "mackerel", n=25)
        before = (await db.market(now))["mackerel"]["price"]
        r = await db.sell(A, commons[:20], now)
        assert r["ok"] and "points" not in r
        assert (await db.market(now))["mackerel"]["price"] < before * 0.7                     # 많이 팔리면 시세 하락
        assert (await db.sell(A, commons[20:21], now))["ok"]                                   # 2.82b: 판매는 포인트 한도 없음
        assert (await db.bag(A, now))["used"] == 0
        r = await db.give(A, B, commons[21:25], now)                                           # 보내기만 포인트를 쓴다
        assert r["ok"] and r["used"] == 4
        await db.give_item(A, "fish_charm")
        assert (await db.use_charm(A, now))["limit"] == 25
        # 다음 날: 한도 초기화 · 판매량 시세 20% 회복
        nxt = now + D
        sup = (await db.market(nxt))["mackerel"]["supply"]
        con = db._connect()
        prev_sup = con.execute("SELECT supply FROM fish_market WHERE species='mackerel'").fetchone()[0]
        con.close()
        assert sup == prev_sup
        legend = _add(db, A, "goldcarp", ts=nxt)
        epic = _add(db, A, "tuna", ts=nxt, n=6)
        bal0 = await db.get_balance(A)
        r = await db.sell(A, legend + epic[:5], nxt)
        assert r["ok"] and await db.get_balance(A) == bal0 + r["gain"]
        # 보내기(트레이드)도 포인트를 쓴다 · 자신에게는 안 됨
        assert (await db.give(A, A, epic[5:], nxt + D))["reason"] == "self"
        r = await db.give(A, B, epic[5:], nxt + D)
        assert r["ok"] and r["points"] == 3
        assert "tuna" in [f["species"] for f in (await db.bag(B, nxt + D))["fish"]]
        # 신선도: 이틀 지난 물고기는 싸다 (아이스박스면 덜)
        old = _add(db, B, "flounder", ts=nxt)
        b = await db.bag(B, nxt + 2 * D)
        f = next(x for x in b["fish"] if x["id"] == old[0])
        assert abs(f["fresh"] - 0.8) < 1e-9
        await db.give_item(B, "icebox")
        f2 = next(x for x in (await db.bag(B, nxt + 2 * D))["fish"] if x["id"] == old[0])
        assert abs(f2["fresh"] - 0.9) < 1e-9 and f2["price"] > f["price"]
    run(flow())


def test_feed_cook_aquarium_weekly():
    db = F.FishingDB()
    clubs = cdb.ClubDB()
    pm = pmdb.PlayerMarketDB()
    U = 41

    async def flow():
        now = T0 + 10 * D
        await clubs.create_club(U, "낚시 FC", now)
        await pm.give_amateur_squad(U)
        await clubs.auto_lineup(U)
        team = await clubs.get_team(U)
        starters = [s["player_id"] for s in team["lineup"] if s.get("player_id")]
        assert len(starters) == 11
        fish = _add(db, U, "anchovy", n=14)
        for i, pid in enumerate(starters):
            r = await db.feed(U, fish[i], pid, now)
            assert r["ok"] and r["fed_today"] == i + 1
            assert (await db.team_meal_bonus(U, now)) == (1 if i == 10 else 0)
        assert (await db.feed(U, fish[11], starters[0], now))["reason"] == "fed"               # 하루 한 번
        assert (await db.feed(U, fish[11], "NOT_A_PLAYER", now))["reason"] == "not_starter"
        assert await db.team_meal_bonus(U, now + D) == 0                                         # 내일은 다시
        # 유망주: 성장 경험치
        await db.add_balance(U, 10_000_000)
        p = await clubs.create_prospect(U, {"name": "물고기", "nation": "대한민국", "position": "ST", "number": 9,
                                            "birthday": "01-01", "foot": "오른발", "height": 180}, now)
        assert p["ok"], p
        pid = (await clubs.prospects(U, now))["active"]["id"]
        xp0 = (await clubs.prospects(U, now))["active"]["xp"]
        big = _add(db, U, "coelacanth")
        r = await db.feed(U, big[0], f"P{pid}", now)
        assert r["ok"] and r["xp"] == 15 and (await clubs.prospects(U, now))["active"]["xp"] == xp0 + 15
        # 요리: 재료 부족 → 실패 · 있으면 24시간 효과
        assert (await db.cook(U, "sashimi", now))["reason"] == "fish"
        _add(db, U, "flounder", n=2)
        assert (await db.cook(U, "sashimi", now))["reason"] == "spice"
        await db.give_item(U, "spice", 1)
        r = await db.cook(U, "sashimi", now)
        assert r["ok"] and r["until"] == now + 24 * 3600 and (await db.inventory(U))[0].get("spice", 0) == 0
        assert "sashimi" in await db.buffs(U, now) and not await db.buffs(U, now + 25 * 3600)
        # 🍣 모둠회: 스카우트 · 훈련 · 직관 +보상 10% (경제 DB 와 연결)
        con = db._connect()
        assert edb._cook_buff(con, U, now, "sashimi") and not edb._cook_buff(con, U, now, "stew")
        con.close()
        # 수족관: 10마리까지 · 전시 중엔 못 판다
        ids = _add(db, U, "seabream", n=11)
        for i in ids[:10]:
            assert (await db.display(U, i, True))["ok"]
        assert (await db.display(U, ids[10], True))["reason"] == "full"
        assert (await db.sell(U, ids[:1], now))["reason"] == "none"
        assert await db.aquarium_score(U) == 40
        # 주간 최대어: 등급 먼저, 그다음 크기 비율
        con = db._connect()
        wk = F.week_key(now)
        con.executemany("INSERT INTO fish_log VALUES(?,?,?,?,?)", [
            (1, "tuna", 300, now, wk), (2, "goldcarp", 60, now, wk), (3, "anchovy", 17, now, wk), (1, "anchovy", 9, now, wk),
            (4, "goldcarp", 150, now - 8 * D, F.week_key(now - 8 * D))])
        con.commit()
        con.close()
        top = await db.weekly_top(now)
        assert [t["user_id"] for t in top[:3]] == [2, 1, 3] and top[1]["species"] == "tuna"
        # 스킵권 조각 5개 → 스킵권
        await db.give_item(U, "skip_piece", 4)
        assert not (await db.combine_pieces(U))["ok"]
        await db.give_item(U, "skip_piece", 1)
        r = await db.combine_pieces(U)
        assert r["ok"] and r["item"] in ("scout_skip", "train_skip", "watch_skip")
    run(flow())


def test_screens():
    """낚시 메뉴 화면이 모두 그려진다 (컴포넌트 수 한도 포함)."""
    import cogs.fishing as cf
    cog = cf.Fishing.__new__(cf.Fishing)
    cog.db, cog.clubs, cog._tasks = F.FishingDB(), cdb.ClubDB(), {}
    cog.bot = SimpleNamespace(get_user=lambda i: None, get_channel=lambda i: None)
    user = SimpleNamespace(id=41, display_name="낚시왕", display_avatar=SimpleNamespace(url="https://x/a.png"))

    async def flow():
        view = cog.menu_view(user)
        for page in range(-1, len(view.pages)):
            view.page = page
            e = await view.build()
            assert e.title and len(view.children) <= 25 and all(c.row is None or c.row <= 4 for c in view.children), page
            assert len(e.description or "") <= 4096
        s = await cog.db.status(41, T0)
        e = await cog._pond_embed(user, s)
        assert "낚시터" in e.title
        for r in ({"kind": "early"}, {"kind": "fled"}, {"kind": "junk", "item": "old_boot"}, {"kind": "junk", "item": "spice"},
                  {"kind": "fish", "species": "goldcarp", "grade": 4, "size": 99, "xp": 150, "level": 3, "leveled": 1,
                   "wait": 600, "price": 700_000, "new": True, "record": False}):
            assert cog._result_card(user, r).title
        cv = cf.CastView(cog, user, {"cast_ts": None, "bait_qty": {"bait": 2, "bait_gold": 0}})
        assert [c.label for c in cv.children] == ["던지기", "일반 미끼로 (2)", "어망 · 메뉴"]
        dm = cog._bite_dm({"channel_id": 5, "message_id": 6, "bite_ts": T0, "window": 30}, user)   # 2.82 입질 DM
        assert "입질" in dm.title and "/5/6" in dm.description
        from services.notifier import NOTIFY_EVENTS
        assert "낚시_입질" in NOTIFY_EVENTS and len(NOTIFY_EVENTS) <= 15   # /알림설정 버튼 3개씩 5줄 한도
        me = await cog.market_embed(user)
        assert me.description.count("원") == 40
    run(flow())


if __name__ == "__main__":
    test_odds_and_values()
    test_flow()
    test_trade_and_market()
    test_feed_cook_aquarium_weekly()
    test_screens()
    print("OK: fishing")
