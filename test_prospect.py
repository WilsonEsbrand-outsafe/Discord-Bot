# test_prospect.py — 2.4 유망주: 생성 · 구단 명단 · 경기 기록/성장 · 노화/은퇴 · 스테로이드 · 영구결번 · 화면
# 실행: venv/Scripts/python.exe test_prospect.py
import asyncio
import random
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

import cogs.club as cc  # noqa: E402
import cogs.prospect as cp  # noqa: E402
from cogs.economy import Economy  # noqa: E402

Y = cdb.PROSPECT_YEAR
INFO, _ = cdb.prospect_input("손흥민", "대한민국", "LW", 7, "7월 8일", "양발", 183)


def sql(q, *a):
    con = sqlite3.connect(TMP)
    rows = con.execute(q, a).fetchall()
    con.commit(); con.close()
    return rows


def member(uid, name="유저"):
    return SimpleNamespace(id=uid, display_name=name, display_avatar=SimpleNamespace(url="https://x/a.png"), bot=False)


def recorder():
    sent = []
    async def rec(*a, **k):
        sent.append(k)
        return SimpleNamespace(edit=rec)
    async def noop(*a, **k): pass
    return sent, rec, noop


class Forced:   # 스테로이드 결과를 정해 놓고 굴린다 (변화량은 최대치)
    def __init__(self, kind):
        self.kind = kind
    def choices(self, population, weights):
        return [self.kind]
    def randint(self, a, b):
        return b


def test_input():
    assert INFO == {"name": "손흥민", "nation": "대한민국", "position": "LW", "number": 7, "birthday": "07-08",
                    "foot": "양발", "height": 183}
    for text, want in (("3-15", "03-15"), ("03/15", "03-15"), ("3.15", "03-15"), ("0315", "03-15"),
                       ("3 15", "03-15"), ("2월 29일", "02-29"), ("2-30", None), ("13-1", None), ("아무날", None)):
        assert cdb.parse_birthday(text) == want, text
    assert cdb.prospect_input("**굵게**", "한국", "ST", 9, "1-1")[0]["name"] == "굵게"        # 마크다운 기호 제거
    for bad in (("", "한국", "ST", 9, "1-1"), ("가" * 13, "한국", "ST", 9, "1-1"), ("이름", "한국", "XX", 9, "1-1"),
                ("이름", "한국", "ST", 0, "1-1"), ("이름", "한국", "ST", 9, "2-30")):
        assert cdb.prospect_input(*bad)[0] is None, bad
    assert cdb.prospect_input("이름", "한국", "ST", 9, "1-1", height=149)[0] is None
    assert set(cdb.PROSPECT_POSITIONS) <= set(cdb.SLOT_GROUP)
    assert cdb.prospect_xp_need(50) == 20 and cdb.prospect_xp_need(70) == 80
    assert abs(sum(cdb.STEROID_TABLE.values()) - 1) < 1e-9
    attrs = cdb.prospect_attrs("YP1", "FW", 70)
    assert len(attrs) == 6 and attrs == cdb.prospect_attrs("YP1", "FW", 70)                     # 선수마다 고정
    assert [v + 5 for _, v in attrs] == [v for _, v in cdb.prospect_attrs("YP1", "FW", 75)]     # OVR 따라 같이 오른다
    assert dict(attrs)["슛"] > dict(attrs)["수비"]


def test_assists():
    xi = lambda pre: [{"name": f"{pre}{i}", "pos": p, "ovr": 70, "player_id": f"{pre}{i}"}  # noqa: E731
                      for i, p in enumerate(["GK", "DF", "DF", "MF", "MF", "FW", "FW"])]
    rng, goals = random.Random(5), []
    for _ in range(300):
        goals += cc.simulate_match({"name": "A", "rating": 70, "xi": xi("a")}, {"name": "B", "rating": 70, "xi": xi("b")}, rng)["goals"]
    assisted = [g for g in goals if g.get("assist_id")]
    assert 0.6 < len(assisted) / len(goals) < 0.9
    assert all(g["assist_id"] != g["scorer_id"] and g["assist_id"][0] == g["scorer_id"][0] for g in assisted)   # 같은 팀 다른 선수
    assert not any(g["assist_id"].endswith("0") for g in assisted)                                            # 골키퍼는 도움 X
    hl = cc.match_highlights({"goals": assisted[:1]}, {"name": "A", "xi": []}, {"name": "B", "xi": []})
    assert f"(도움 {assisted[0]['assist']})" in next(h for h in hl if h["goal"])["text"]


async def _flow():
    eco, pm, clubs = edb.EconomyDB(), pmdb.PlayerMarketDB(), cdb.ClubDB()
    now = int(time.time())
    A, B = 701, 702

    # 생성: 500만원 · 잔액 부족이면 실패 · 한 명만
    assert (await clubs.create_prospect(A, INFO, now))["reason"] == "balance"
    await eco.add_balance(A, 6_000_000)
    r = await clubs.create_prospect(A, INFO, now, random.Random(1))
    assert r["ok"] and r["balance"] == 1_000_000 and await eco.get_balance(A) == 1_000_000
    assert r["pid"] == f"YP{r['id']}" and r["group"] == "FW" and r["age"] == 17 and 50 <= r["ovr"] <= 58 and 75 <= r["pot"] <= 94
    assert (await clubs.create_prospect(A, INFO, now))["reason"] == "exists"
    pid = r["pid"]
    # 이적시장과 분리: 선수 테이블 · 보유 카드 어디에도 없다
    assert not sql("SELECT 1 FROM pm_players WHERE player_id=?", pid) and not sql("SELECT 1 FROM pm_holdings WHERE player_id=?", pid)

    # 구단 명단: 내 구단에 넣고 주장까지
    for uid in (A, B):
        await clubs.create_club(uid, f"{uid} FC", now)
        await pm.give_amateur_squad(uid)
        await clubs.auto_lineup(uid)
    squad = {p["player_id"]: p for p in await clubs.squad(A)}
    assert squad[pid]["prospect"] and squad[pid]["number"] == 7 and squad[pid]["pos"] == "FW"
    lw = cdb.FORMATIONS["4-4-2"].index("ST")
    assert (await clubs.set_slot(A, lw, pid))[0]
    ok, msg = await clubs.set_captain(A, pid)
    assert ok and "손흥민" in msg
    team = await clubs.get_team(A)
    assert team["captain"] == pid and any(s.get("player_id") == pid for s in team["lineup"])
    e = cc._team_embed(team, member(A))
    assert "🌟**손흥민** #7" in e.description

    # 경기 기록: 골 · 도움 · 출전 · 성장 (상대 팀 유망주도) — 하루 20경기까지만 성장
    await eco.add_balance(B, 5_000_000)
    rb = await clubs.create_prospect(B, {**INFO, "name": "상대", "number": 10}, now)
    assert (await clubs.set_slot(B, lw, rb["pid"]))[0]
    me = {"player_id": pid}
    opp = {"player_id": rb["pid"]}
    goals = [{"scorer_id": pid, "assist_id": "x"}, {"scorer_id": "x", "assist_id": pid}, {"scorer_id": pid}]
    out = await clubs.record_prospects([([me], 3, 1), ([opp], 1, 3)], goals, now)
    mine = next(x for x in out if x["name"] == "손흥민")
    assert (mine["goals"], mine["assists"]) == (2, 1) and mine["grew"] and len(out) == 2
    p = (await clubs.prospects(A, now))["active"]
    assert (p["apps"], p["goals"], p["assists"]) == (1, 2, 1)
    ovr, xp = r["ovr"], 10 + 2 * 6 + 4 + 5                                         # 출전 + 2골 + 1도움 + 승
    while xp >= cdb.prospect_xp_need(ovr):
        xp, ovr = xp - cdb.prospect_xp_need(ovr), ovr + 1
    assert (p["ovr"], p["xp"]) == (ovr, xp)
    sql("UPDATE prospects SET day_n=?, day_key=? WHERE id=?", cdb.PROSPECT_DAILY_GROWTH, cdb.kst_day(now), r["id"])
    before = (await clubs.prospects(A, now))["active"]
    x = (await clubs.record_prospects([([me], 1, 0)], [{"scorer_id": pid}], now))[0]
    after = (await clubs.prospects(A, now))["active"]
    assert not x["grew"] and (after["ovr"], after["xp"]) == (before["ovr"], before["xp"]) and after["goals"] == before["goals"] + 1
    x = (await clubs.record_prospects([([me], 0, 0)], [], now + 86400))[0]       # 다음 날 다시 성장
    assert x["grew"]
    # 잠재력까지만: 잠재력에 닿으면 경험치는 0
    sql("UPDATE prospects SET ovr=pot-1, xp=0 WHERE id=?", r["id"])
    for _ in range(30):
        await clubs.record_prospects([([me], 5, 0)], [{"scorer_id": pid}] * 5, now + 2 * 86400)
    p = (await clubs.prospects(A, now + 2 * 86400))["active"]
    assert p["ovr"] == p["pot"] and p["xp"] == 0 and p["peak_ovr"] == p["pot"]

    # 화면: /유망주 · 친선경기 결과에 🌟 기록 줄
    sent, rec, noop = recorder()
    cog = cp.Prospect.__new__(cp.Prospect)
    cog.clubs = clubs
    inter = SimpleNamespace(user=member(A, "흥민맘"), response=SimpleNamespace(defer=noop, send_message=rec, edit_message=rec),
                            followup=SimpleNamespace(send=rec))
    await cp.Prospect.show.callback(cog, inter, None)
    e = sent[-1]["embed"]
    assert "손흥민 #7" in e.title and "`나이` **17세**" in e.description and "LW 왼쪽 윙어" in e.description
    assert [f.name for f in e.fields] == ["📊 능력치", "📈 커리어"] and "`골`" in e.fields[1].value
    club = cc.Club.__new__(cc.Club)
    club.clubs, club.money, club.pm, club._playing = clubs, eco, pm, set()
    real_sleep, cc.asyncio.sleep = cc.asyncio.sleep, (lambda s: real_sleep(0))
    try:
        await cc.Club.friendly.callback(club, inter, member(B, "상대"))
    finally:
        cc.asyncio.sleep = real_sleep
    assert "🌟 **손흥민** #7" in sent[-1]["embed"].description and "🌟 **상대** #10" in sent[-1]["embed"].description

    # 생성 화면: 이미 있으면 막힌다
    await cp.Prospect.create.callback(cog, inter, "새선수", "브라질", "ST", 9, "1-1")
    assert sent[-1]["ephemeral"] and "한 명만" in sent[-1]["embed"].description

    # 스테로이드: 유망주에게만 · 결과별 능력치 · 은퇴
    assert (await clubs.use_steroid(A, now))["reason"] == "none"
    await eco.give_item(A, "steroid", 10)
    assert (await clubs.use_steroid(999, now))["reason"] == "none"
    await eco.give_item(999, "steroid")
    assert (await clubs.use_steroid(999, now))["reason"] == "no_prospect"
    assert (await eco.inventory(999))[0]["steroid"] == 1                          # 실패하면 안 쓰인다
    sql("UPDATE prospects SET ovr=70, pot=80 WHERE id=?", r["id"])
    peak0 = (await clubs.prospects(A, now))["active"]["peak_ovr"]
    want = {"ovr": (73, 80), "pot": (73, 85), "awaken": (76, 88), "none": (76, 88), "doping": (72, 88)}
    for kind, (o, pt) in want.items():
        s = await clubs.use_steroid(A, now, Forced(kind))
        p = (await clubs.prospects(A, now))["active"]
        assert s["ok"] and s["kind"] == kind and (p["ovr"], p["pot"]) == (o, pt) == (s["ovr"], s["pot"]), kind
    assert p["peak_ovr"] == max(peak0, 76)                                         # 최고 기록은 떨어져도 남는다
    econ = Economy.__new__(Economy)
    econ.db = eco
    econ.clubs = SimpleNamespace(prospects=clubs.prospects,                         # 화면 테스트는 '효과 없음'으로 고정
                                 use_steroid=lambda uid, ts: clubs.use_steroid(uid, ts, Forced("none")))
    await econ._steroid_prompt(inter, inter.user)
    assert sent[-1]["ephemeral"] and "손흥민 #7에게 주사할까요" in sent[-1]["embed"].title
    await sent[-1]["view"].inject.callback(inter)
    assert sent[-2]["view"] is None and "💉" in sent[-1]["embed"].title and "유망주 `#7`" in sent[-1]["embed"].description
    lonely = member(999, "빈손")
    await econ._steroid_prompt(SimpleNamespace(user=lonely, response=SimpleNamespace(send_message=rec)), lonely)
    assert "유망주가 없어요" in sent[-1]["embed"].title

    # 은퇴 + 영구결번 → 명예의 전당 · 그 번호로는 새로 못 만든다 · /구단에 걸린다
    await cp.Prospect.retire.callback(cog, inter)
    view = sent[-1]["view"]
    await view.children[0].callback(inter)                                         # 🏅 은퇴 + 영구결번
    assert "전성기 커리어" in sent[-1]["embed"].title and "영구결번" in sent[-1]["embed"].description
    data = await clubs.prospects(A, now)
    assert data["active"] is None and data["retired"][0]["retired_number"] == 1 and data["retired"][0]["retire_reason"] == "self"
    assert (await clubs.get_team(A))["retired_numbers"] == [7]
    assert "`영구결번` 🏅 #7" in cc._team_embed(await clubs.get_team(A), member(A)).description
    assert pid not in {s.get("player_id") for s in (await clubs.get_team(A))["lineup"]}   # 명단에서 빠진다
    await eco.add_balance(A, 20_000_000)
    assert (await clubs.create_prospect(A, INFO, now))["reason"] == "retired_number"
    await cp.Prospect.create.callback(cog, inter, "새선수", "브라질", "ST", 7, "1-1")
    assert "영구결번" in sent[-1]["embed"].description

    # 새 유망주 → 스테로이드 부작용 은퇴 → /영구결번 (현역이 같은 번호면 막힌다)
    await cp.Prospect.create.callback(cog, inter, "새선수", "브라질", "ST", 9, "1-1")
    assert "만들까요" in sent[-1]["embed"].title
    await sent[-1]["view"].confirm.callback(inter)
    assert "입단" in sent[-1]["embed"].title and sent[-1].get("ephemeral") is None
    new = (await clubs.prospects(A, now))["active"]
    s = await clubs.use_steroid(A, now, Forced("retire"))
    assert s["kind"] == "retire" and (await clubs.prospects(A, now))["active"] is None
    gone = next(x for x in (await clubs.prospects(A, now))["retired"] if x["id"] == new["id"])
    assert gone["id"] == new["id"] and gone["retire_reason"] == "steroid" and not gone["retired_number"]
    r2 = await clubs.create_prospect(A, {**INFO, "number": 9}, now)              # 은퇴만 한 #9 는 다시 쓸 수 있다
    assert r2["ok"]
    assert (await clubs.retire_number(A, new["id"], now))["reason"] == "wearing"
    await clubs.retire_prospect(A, r2["id"], now, False)
    choices = await cog.retired_autocomplete(inter, "")
    assert {c.value for c in choices} == {str(new["id"]), str(r2["id"])}
    await cp.Prospect.retire_number.callback(cog, inter, str(new["id"]))
    assert "🏅 #9 영구결번" in sent[-1]["embed"].title
    assert (await clubs.retire_number(A, r2["id"], now))["reason"] == "taken"
    assert (await clubs.retire_number(A, new["id"], now))["reason"] == "done"
    assert (await clubs.retire_number(B, new["id"], now))["reason"] == "none"     # 남의 선수
    await cp.Prospect.show.callback(cog, inter, None)
    assert "명예의 전당" in sent[-1]["embed"].fields[-1].name and sent[-1]["embed"].fields[-1].value.count("🏅") == 2

    # 노화: 1살 = 7일 · 30세까지는 그대로 · 31세부터 해마다 -1~3 · 40세에 은퇴
    C = 703
    await eco.add_balance(C, 5_000_000)
    rc = await clubs.create_prospect(C, INFO, now)
    sql("UPDATE prospects SET ovr=90, pot=90, peak_ovr=90 WHERE id=?", rc["id"])
    sql("UPDATE prospects SET created_ts=? WHERE id=?", now - 13 * Y - 10, rc["id"])
    p = (await clubs.prospects(C, now))["active"]
    assert p["age"] == 30 and p["ovr"] == 90
    sql("UPDATE prospects SET created_ts=? WHERE id=?", now - 16 * Y - 10, rc["id"])
    p = (await clubs.prospects(C, now))["active"]
    assert p["age"] == 33 and 81 <= p["ovr"] <= 87 and p["aged"] == 33
    assert not (await clubs.record_prospects([([{"player_id": p["pid"]}], 9, 0)], [], now))[0]["grew"]   # 31세부터 성장 X
    sql("UPDATE prospects SET created_ts=? WHERE id=?", now - 23 * Y - 10, rc["id"])
    assert (await clubs.prospects(C, now))["active"] is None
    old = (await clubs.prospects(C, now))["retired"][0]
    assert old["retire_reason"] == "age" and old["age"] == 40 and old["peak_ovr"] == 90 and old["ovr"] < 81


def test_flow():
    asyncio.run(_flow())


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
