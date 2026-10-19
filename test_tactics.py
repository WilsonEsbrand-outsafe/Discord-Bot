# test_tactics.py — 2.8 전술 상성: 상성 · 효과 공식 · 구간 경기(승률) · 하프타임 규칙 · 전환 손실 · 적응도(+5 · 하루 상한 · 방치 감소)
# · 전술 노트 · 분석 리포트 · 공식경기 배당 · 하프타임 버튼 중계 · 공개 전 서버는 2.7 그대로 · 허브 화면
# 실행: venv/Scripts/python.exe test_tactics.py
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

import cogs.club as CC   # noqa: E402
import cogs.hub as CH   # noqa: E402
import release   # noqa: E402
release.RELEASE_TS = release.MAINTENANCE = None   # 2.8 공개 전 상태로 고정: 다른 서버(999 등)는 2.7, 테스트 서버만 2.8
import services.tactics as T   # noqa: E402
from services.fishing import FishingDB   # noqa: E402

D = 86400
T0 = 1_800_000_000


def side(r, t, fit=100.0, rules=None, tag="x"):
    xi = [{"name": f"{tag}{i}", "pos": p, "ovr": r, "player_id": f"{tag}{i}"}
          for i, p in enumerate(["GK"] + ["DF"] * 4 + ["MF"] * 4 + ["FW"] * 2)]
    return {"name": tag, "uid": 0, "rating": r, "xi": xi, "tactic": t, "rules": rules or {},
            "fit": {k: fit for k in T.TACTICS}, "comp": {k: 1.0 for k in T.TACTICS}, "switch": 0.7}


def rate(h, a, n=30000, seed=1, **kw):
    rng = random.Random(seed)
    w = d = 0
    for _ in range(n):
        m = T.simulate(h, a, rng, **kw)
        w += m["home"] > m["away"]
        d += m["home"] == m["away"]
    return w / n, d / n


def test_rules_and_engine():
    assert [T.advantage("press", "possession"), T.advantage("possession", "counter"), T.advantage("counter", "press")] == [1, 1, 1]
    assert T.advantage("possession", "press") == -1 and T.advantage("press", "press") == 0
    assert T.effect(100, 1.0) == 1.0 and T.effect(0, 1.0) == 0.5 and T.effect(100, 1.2) == 1.2
    assert T.switch_mult(None) == 0.70 and T.switch_mult("rookie") == 0.73 and T.switch_mult("legend") == 0.82
    assert T.rule_state(0, 1) == "losing" and T.rule_state(1, 1) == "draw" and T.rule_state(2, 1) == "winning"
    s = side(60, "possession", rules={"losing": "press"})
    assert T.ht_choice(s, 0, 1) == "press" and T.ht_choice(s, 1, 1) == "possession"
    # 구성: 수비수 많으면 압박(피지컬)이 유리 · 0.8~1.2
    xi = side(70, "press")["xi"]
    assert 0.8 <= T.composition(xi, "counter") <= T.composition(xi, "press") <= 1.2
    # 방치 감소: 3일까지 그대로, 그 뒤 하루 -2, 최저 20
    assert T.decayed(60, T0, T0 + 3 * D) == 60 and T.decayed(60, T0, T0 + 5 * D) == 56 and T.decayed(30, T0, T0 + 60 * D) == 20
    # 승률: 같은 전술은 2.7과 같은 분포, 유리하면 설계표대로 (40.9% · 39.0%)
    old = random.Random(1)
    w27 = sum((m := cdb.simulate_match({"name": "a", "rating": 60, "xi": side(60, "x")["xi"]},
                                         {"name": "b", "rating": 60, "xi": side(60, "y")["xi"]}, old))["home"] > m["away"]
              for _ in range(30000)) / 30000
    same, _ = rate(side(60, "possession"), side(60, "possession"))
    full, _ = rate(side(60, "press", 100), side(60, "possession", 100))
    half, _ = rate(side(60, "press", 0), side(60, "possession", 0))
    print(f"전술 승률 — 2.7 {w27:.3f} · 같은 전술 {same:.3f} · 유리(효과 1.0) {full:.3f} · 유리(효과 0.5) {half:.3f}")
    assert abs(same - w27) < 0.015 and abs(full - 0.409) < 0.012 and abs(half - 0.390) < 0.012
    pw, _, _ = cdb.win_probs(60, 60, *T.base_mults(side(60, "press"), side(60, "possession")))
    assert abs(pw - 0.409) < 0.002                                          # 배당은 상성까지 넣은 확률
    # 하프타임 전환: 전반 불리 → 후반 유리로 바꾸면 그대로 둘 때보다 낫지만, 처음부터 유리한 것보다는 못하다 (15분 손실)
    stay, _ = rate(side(60, "possession"), side(60, "press"), home_ht="possession", seed=3)
    switch, _ = rate(side(60, "possession"), side(60, "press"), home_ht="counter", seed=3)
    good, _ = rate(side(60, "counter"), side(60, "press"), home_ht="counter", seed=3)
    print(f"하프타임 — 불리 유지 {stay:.3f} · 유리로 전환 {switch:.3f} · 처음부터 유리 {good:.3f}")
    assert stay < switch < good
    m = T.simulate(side(60, "counter"), side(60, "press", rules={"draw": "counter", "losing": "counter", "winning": "counter"}),
                   random.Random(5), home_ht="possession")
    assert m["tactics"] == {"home": ("counter", "possession"), "away": ("press", "counter")} and "ht" in m
    assert all(1 <= g["minute"] <= 90 for g in m["goals"]) and m["home"] + m["away"] == len(m["goals"])


def test_db():
    db = T.TacticsDB()
    U = 5

    async def flow():
        s = await db.settings(U)
        assert s == {"tactic": "possession", "rules": {}, "report": False}
        await db.set_tactic(U, "press")
        s = await db.set_rule(U, "losing", "counter")
        assert s["tactic"] == "press" and s["rules"] == {"losing": "counter"}
        assert (await db.set_rule(U, "losing", None))["rules"] == {}
        team = {"lineup": side(60, "press", tag="p")["xi"], "manager": "legend"}
        sd = {"xi": team["lineup"]}
        await db.prepare(U, team, sd, T0)
        assert sd["tactic"] == "press" and sd["fit"]["press"] == T.FIT_START and sd["switch"] == 0.82
        # 경기: 시작 전술 · 후반 전술 모두 +5
        await db.after_match(U, sd, ("press", "counter"), T0)
        f = await db.player_fits(U, ["p1"], T0)
        assert f["p1"]["press"] == 45 and f["p1"]["counter"] == 45 and f["p1"]["possession"] == 40
        # 하루 상한 100 (= 20경기)
        for _ in range(30):
            await db.after_match(U, sd, ("press", "press"), T0)
        assert (await db.player_fits(U, ["p1"], T0))["p1"]["press"] == 100
        await db.after_match(U, sd, ("possession", "possession"), T0 + D)
        assert (await db.player_fits(U, ["p1"], T0 + D))["p1"]["possession"] == 45
        # 방치하면 내려간다
        assert (await db.player_fits(U, ["p1"], T0 + 10 * D))["p1"]["press"] == 100 - 2 * 7
        # 스쿼드 B 는 적응도 70 고정 · 기록만
        sb = {"xi": team["lineup"]}
        await db.prepare(U, {"lineup": team["lineup"], "squad": "B"}, sb, T0)
        assert sb["fit"]["counter"] == T.ELITE_FIT
        assert (await db.recent(U, 5))[0] == ("possession", "possession") and len(await db.recent(U, 5)) == 5
        # 아이템
        assert not (await db.use_note(U, ["p1"], T0 + D))["ok"]
        await db.give_item(U, "tactic_note", 1)
        r = await db.use_note(U, ["p1", "p2"], T0 + D)
        assert r["ok"] and r["tactic"] == "press"
        f = await db.player_fits(U, ["p1", "p2"], T0 + D)
        assert f["p1"]["press"] == 100 and f["p2"]["press"] == 100   # p2 도 이미 100 이었다
        assert not await db.use_report(U)
        await db.give_item(U, "analysis_report", 1)
        assert await db.use_report(U) and (await db.settings(U))["report"]
        assert await db.consume_report(U) and not await db.consume_report(U)
    asyncio.run(flow())


class Msg:
    def __init__(self, log, press=None):
        self.log, self.press = log, press

    async def edit(self, **k):
        self.log.append(k)
        v = k.get("view")
        if isinstance(v, CC.HalftimeView) and self.press:
            btn = next(b for b in v.children if b.label.startswith(T.TACTICS[self.press][1]))
            other = SimpleNamespace(user=SimpleNamespace(id=999), response=SimpleNamespace(send_message=_noop))
            await btn.callback(other)                                       # 다른 사람은 못 고른다
            assert v.choice is None
            await btn.callback(SimpleNamespace(user=v.user, response=SimpleNamespace(defer=_noop)))


async def _noop(*a, **k):
    pass


def test_match_flow():
    clubs, eco, pm = cdb.ClubDB(), edb.EconomyDB(), pmdb.PlayerMarketDB()
    A, B = 11, 12
    club = CC.Club.__new__(CC.Club)
    club.bot = None
    club.clubs, club.money, club.pm, club.fish, club.tac, club._playing = clubs, eco, pm, FishingDB(), T.TacticsDB(), set()
    club.BROADCAST_GAP, club.HT_WAIT = 0, 0.05

    def member(i, n):
        return SimpleNamespace(id=i, display_name=n, display_avatar=SimpleNamespace(url="https://x/a.png"), bot=False,
                               guild=SimpleNamespace(id=release.TEST_GUILD))

    async def flow():
        for u in (A, B):
            await clubs.create_club(u, f"FC{u}", T0)
            await pm.give_amateur_squad(u)
            await clubs.auto_lineup(u)
        await club.tac.set_tactic(B, "press")
        await eco.give_item(A, "analysis_report", 1)
        await club.tac.use_report(A)
        for guild, press in ((release.TEST_GUILD, "counter"), (release.TEST_GUILD, None), (999, None)):
            edits, sent = [], []

            async def send(*a, **k):
                sent.append(k)
                return Msg(edits, press)
            inter = SimpleNamespace(guild_id=guild, user=member(A, "나"), response=SimpleNamespace(defer=_noop),
                                    followup=SimpleNamespace(send=send))
            n0 = len(await club.tac.recent(A, 50))
            await CC.Club.friendly.callback(club, inter, member(B, "상대"))
            kick, final = sent[0]["embed"].description, edits[-1]["embed"].description
            if guild == 999:                                                  # 공개 전 서버: 2.7 경기 그대로
                assert "전술" not in kick and "`전술`" not in final and len(await club.tac.recent(A, 50)) == n0
                assert not any(isinstance(e.get("view"), CC.HalftimeView) for e in edits)
                continue
            assert "⚔️ 내 전술 **🎯 점유**" in kick
            assert ("📊 분석 리포트: FC12 **🔥 압박**" in kick) == (press == "counter")   # 리포트는 한 번만
            ht = next(e for e in edits if isinstance(e.get("view"), CC.HalftimeView))
            assert "🔍 상대" in ht["embed"].description and "🔥 압박" in ht["embed"].description
            rows = await club.tac.recent(A, 1)
            if press:
                assert rows[0] == ("possession", "counter") and "→ ⚡ 역습" in final and "전술 변경" in final
            else:
                assert rows[0] == ("possession", "possession")               # 안 누르면 규칙(없으면 유지)
            assert (await club.tac.recent(B, 1))[0][0] == "press" and "`전술`" in final
            fits = await club.tac.player_fits(A, [p["player_id"] for p in (await clubs.get_team(A))["lineup"]], int(CC.time.time()))
            assert all(f["possession"] > T.FIT_START for f in fits.values())
    asyncio.run(flow())


def test_hub_pages():
    hubs = CH.Hubs.__new__(CH.Hubs)
    hubs.bot, hubs.db, hubs.clubs, hubs.tac = None, edb.EconomyDB(), cdb.ClubDB(), T.TacticsDB()
    user = SimpleNamespace(id=11, display_name="나", display_avatar=SimpleNamespace(url="https://x/a.png"))

    async def flow():
        v = hubs.club_view(user, [p.title for p in hubs.club_view(user).pages].index("전술"))
        e = await v.build()
        assert "기본 전술" in e.description and "하프타임 규칙" in e.description
        assert len([c for c in v.children if c.row in (0, 1, 2, 3)]) == 4
        v.ctx["scout"] = 12
        v.page += 1
        e = await v.build()
        assert "최근" in e.description and "🔥 압박" in e.description
    asyncio.run(flow())


if __name__ == "__main__":
    test_rules_and_engine()
    test_db()
    test_match_flow()
    test_hub_pages()
    print("OK: tactics")
