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
    # 중계 장면에 선수 ID · 감점 종류(경고 · 1대1 찬스 놓침)가 붙는다
    scenes = [h for _ in range(100) for h in cc.match_highlights(
        {"goals": []}, {"name": "A", "xi": xi("a")}, {"name": "B", "xi": xi("b")}, rng)]
    assert {h["kind"] for h in scenes} == {"card", "miss", None} and all(h["player_id"] for h in scenes)
    assert all((h["kind"] == "card") == any(w in h["text"] for w in ("옐로카드", "경고")) for h in scenes)
    assert all((h["kind"] == "miss") == ("1대1" in h["text"]) for h in scenes)
    icon = {"옐로카드": "🟨", "경고": "🟨", "골키퍼가": "🧤", "골대": "🥅", "1대1": "😩", "벽": "🧱", "벗어납니다": "💨", "오프사이드": "🚩"}
    assert all(h["icon"] == next(v for k, v in icon.items() if k in h["text"]) for h in scenes)   # 장면마다 맞는 이모지
    assert {h["icon"] for h in scenes} == set(icon.values())


async def _flow():
    eco, pm, clubs = edb.EconomyDB(), pmdb.PlayerMarketDB(), cdb.ClubDB()
    injury_odds = cdb.INJURY_BASE, cdb.INJURY_PER_PRONE
    cdb.INJURY_BASE = cdb.INJURY_PER_PRONE = 0          # 부상은 아래 부상 테스트에서만
    now = int(time.time())
    A, B = 701, 702

    # 생성: 500만원 · 잔액 부족이면 실패 · 한 명만
    assert (await clubs.create_prospect(A, INFO, now))["reason"] == "balance"
    await eco.add_balance(A, 6_000_000)
    r = await clubs.create_prospect(A, INFO, now, random.Random(1))
    assert r["confidence"] == 10 and 3 <= r["proneness"] <= 14 and 4 <= r["pro"] <= 17 and not r["injured"]
    sql("UPDATE prospects SET pro=10 WHERE id=?", r["id"])                       # 프로 의식 10 = 경험치 그대로
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
    assert x["grew"] and x["xp"] == 12 and x["minus"] == []
    # 감점: 3골 차 대패 + 경고 + 찬스 놓침 → 10 - 5 - 5 - 3 - 2 = -5 · 경험치는 0 밑으로 안 간다 (OVR 그대로)
    sql("UPDATE prospects SET xp=3 WHERE id=?", r["id"])
    before = (await clubs.prospects(A, now))["active"]
    ev = [{"player_id": pid, "kind": "card"}, {"player_id": pid, "kind": "miss"}, {"player_id": "x", "kind": "card"},
          {"player_id": pid, "kind": None}]
    x = (await clubs.record_prospects([([me], 0, 3)], [], now + 86400, ev))[0]
    after = (await clubs.prospects(A, now))["active"]
    assert x["xp"] == -5 and x["minus"] == ["L", "rout", "card", "miss"] and (after["xp"], after["ovr"]) == (0, before["ovr"])
    assert cc.Club._star_line(x) == "\n🌟 **손흥민** #7 · 0골 0도움 · 경험치 **-5** (패배, 대패, 경고, 찬스 놓침)"
    x = (await clubs.record_prospects([([me], 1, 2)], [], now + 86400))[0]       # 1골 차 패배는 +5
    assert x["xp"] == 5 and x["minus"] == ["L"]
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
    assert [f.name for f in e.fields] == ["📊 능력치", "📈 커리어", "🔒 히든 능력치"] and "`골`" in e.fields[1].value
    assert "`자신감`" in e.fields[2].value and "`프로 의식` 보통" in e.fields[2].value
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
    sql("UPDATE prospects SET proneness=17 WHERE id=?", r["id"])
    s = await clubs.use_steroid(A, now, Forced("fragile"))                         # 🦴 부작용: 부상 빈도 +3~5 (최대 20)
    assert (s["prone0"], s["prone"]) == (17, 20) and (await clubs.prospects(A, now))["active"]["proneness"] == 20
    assert (s["ovr"], s["pot"]) == (72, 88)
    assert "부상 빈도 **매우 높음 → 매우 높음**" in Economy._steroid_result(Economy.__new__(Economy), inter.user, s).description
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

    # 삭제: 확인 → 기록 없이 사라짐 (명예의 전당 X) · 명단에서 빠짐 · 새로 만들 수 있음
    D = 704
    await eco.add_balance(D, 10_000_000)
    await clubs.create_club(D, "삭제 FC", now)
    rd = await clubs.create_prospect(D, INFO, now)
    await clubs.set_slot(D, lw, rd["pid"])
    di = SimpleNamespace(user=member(D, "삭제러"), response=SimpleNamespace(send_message=rec, edit_message=rec))
    await cp.Prospect.delete.callback(cog, di)
    assert "삭제할까요" in sent[-1]["embed"].title and "환불" not in sent[-1]["embed"].title
    await sent[-1]["view"].cancel.callback(di)
    assert (await clubs.prospects(D, now))["active"]["id"] == rd["id"]                # 취소하면 그대로
    await cp.Prospect.delete.callback(cog, di)
    await sent[-1]["view"].confirm.callback(di)
    assert "삭제 완료" in sent[-1]["embed"].title
    assert await clubs.prospects(D, now) == {"active": None, "retired": []}
    assert rd["pid"] not in {s.get("player_id") for s in (await clubs.get_team(D))["lineup"]}
    assert await clubs.delete_prospect(D, rd["id"], now) is None
    await cp.Prospect.delete.callback(cog, di)
    assert sent[-1]["ephemeral"] and "없어요" in sent[-1]["embed"].title
    assert (await clubs.create_prospect(D, INFO, now))["ok"]

    # 히든 능력치: 자신감은 경기 결과로 오르내리고(1~20) 골 · 도움 기회에, 프로 의식은 경험치 배율에
    E = 705
    await eco.add_balance(E, 20_000_000)
    await clubs.create_club(E, "부상 FC", now)
    await pm.give_amateur_squad(E)
    await clubs.auto_lineup(E)
    re_ = await clubs.create_prospect(E, INFO, now)
    pe = {"player_id": re_["pid"]}
    await clubs.set_slot(E, lw, re_["pid"])
    sql("UPDATE prospects SET pro=20, ovr=60, pot=90, xp=0 WHERE id=?", re_["id"])
    x = (await clubs.record_prospects([([pe], 3, 1)], [{"scorer_id": re_["pid"]}, {"assist_id": re_["pid"]}], now))[0]
    assert x["xp"] == round((10 + 6 + 4 + 5) * 1.3) and (x["conf0"], x["conf"]) == (10, 13)   # 골 · 도움 · 승 → +3
    x = (await clubs.record_prospects([([pe], 0, 4)], [], now, [{"player_id": re_["pid"], "kind": "card"}]))[0]
    assert x["xp"] == 10 - 5 - 5 - 3 and x["conf"] == 13 - 3                     # 감점엔 프로 의식 배율 X · 자신감 -3
    sql("UPDATE prospects SET confidence=1 WHERE id=?", re_["id"])
    assert (await clubs.record_prospects([([pe], 0, 4)], [], now))[0]["conf"] == 1      # 1 밑으로는 안 내려간다
    sql("UPDATE prospects SET confidence=20 WHERE id=?", re_["id"])
    squad = {p["player_id"]: p for p in await clubs.squad(E)}
    assert abs(squad[re_["pid"]]["conf_mult"] - 1.4) < 1e-9
    xi = lambda pre: [{"name": f"{pre}{i}", "pos": "FW", "ovr": 70, "player_id": f"{pre}{i}"} for i in range(4)]  # noqa: E731
    hot = xi("h")
    hot[0]["conf_mult"] = 1.4
    rng = random.Random(3)
    scored = [g["scorer_id"] for _ in range(400) for g in cc.simulate_match(
        {"name": "A", "rating": 70, "xi": hot}, {"name": "B", "rating": 70, "xi": xi("b")}, rng)["goals"] if g["side"] == "home"]
    assert scored.count("h0") > scored.count("h1") * 1.2                          # 자신감 높은 선수가 더 많이 넣는다

    # 부상: 확률(부상 빈도) · 등급 · 결장(의료진 치료능력만큼 단축) · 심각 → OVR/잠재력 하락 · 3번째 부상마다 고질병
    cdb.INJURY_BASE, cdb.INJURY_PER_PRONE = injury_odds
    class Hurt:   # 부상 확정 · 정해진 등급 · 범위 최솟값
        def __init__(self, grade):
            self.grade = grade
        def random(self):
            return 0.0
        def choices(self, population, weights):
            return [self.grade]
        def randint(self, a, b):
            return a
        def choice(self, seq):
            return seq[0]
    assert (await clubs.hire_medic(E, "doctor"))["ok"]
    assert (await clubs.hire_medic(E, "doctor"))["reason"] == "same" and (await clubs.hire_medic(999, "intern"))["reason"] == "no_club"
    sql("UPDATE prospects SET ovr=70, pot=85, injuries=0 WHERE id=?", re_["id"])
    x = (await clubs.record_prospects([([pe], 1, 1)], [], now, rng=Hurt("minor")))[0]
    j = x["injury"]
    assert j["kind"] == "minor" and j["name"] == "발목 염좌" and j["heal"] == 45 and j["until"] == now + round(6 * 3600 * 0.55)
    assert (j["ovr"], j["pot"], j["chronic"]) == (70, 85, False) and j["hours"] == 3
    p = (await clubs.prospects(E, now))["active"]
    assert p["injured"] and p["injuries"] == 1 and p["injury"] == "발목 염좌 (경미)"
    # 부상 중: 명단엔 자리만 지키고 빈자리(전력 30)로 · 선발에 못 넣는다 · 카드에 복귀 시각
    team = await clubs.get_team(E)
    slot = next(s for s in team["lineup"] if s.get("injured"))
    assert slot["player_id"] is None and slot["injured_id"] == re_["pid"] and re_["pid"] not in {p["player_id"] for p in await clubs.squad(E)}
    assert "🚑 손흥민(부상)" in cc._team_embed(team, member(E)).description and "의료진` 🩺 스포츠 의학 박사" in cc._team_embed(team, member(E)).description
    ok, msg = await clubs.set_slot(E, 3, re_["pid"])
    assert not ok and "부상" in msg
    assert "🚑 **부상** — 발목 염좌 (경미)" in cp.prospect_embed(p, member(E), now).description
    assert "🚑 **발목 염좌** (경미) · 3시간 결장 (의료진 -45%)" in cc.Club._star_line(x)
    sql("UPDATE prospects SET injured_until=? WHERE id=?", now - 1, re_["id"])  # 회복하면 원래 자리로 돌아온다
    assert re_["pid"] in {s.get("player_id") for s in (await clubs.get_team(E))["lineup"]}
    # 심각한 부상: OVR -1~3 · 잠재력 -2~5 (최솟값 -1 / -2)
    j = (await clubs.record_prospects([([pe], 1, 1)], [], now, rng=Hurt("severe")))[0]["injury"]
    assert (j["ovr0"], j["ovr"], j["pot0"], j["pot"], j["chronic"]) == (70, 69, 85, 83, False) and j["hours"] == 40
    # 3번째 부상은 가벼워도 고질병: OVR -1 · 잠재력 -2
    sql("UPDATE prospects SET injured_until=0, xp=0 WHERE id=?", re_["id"])
    x = (await clubs.record_prospects([([pe], 1, 1)], [], now, rng=Hurt("minor")))[0]
    j = x["injury"]
    assert j["chronic"] and (j["ovr"], j["pot"]) == (68, 81) and (await clubs.prospects(E, now))["active"]["injuries"] == 3
    assert "고질병 OVR 69→68 · 잠재력 83→81" in cc.Club._star_line(x)
    cdb.INJURY_BASE = cdb.INJURY_PER_PRONE = 0
    # /의료진 화면: 목록 · 영입
    club.money = eco
    ei = SimpleNamespace(user=member(E, "부상러"), response=SimpleNamespace(defer=noop), followup=SimpleNamespace(send=rec))
    await cc.Club.medic.callback(club, ei, None)
    assert "✅ 🩺 **스포츠 의학 박사**" in sent[-1]["embed"].description
    bal = await eco.get_balance(E)
    await cc.Club.medic.callback(club, ei, "physio")
    assert "합류" in sent[-1]["embed"].title and await eco.get_balance(E) == bal - cdb.MEDICS["physio"][3]
    assert (await clubs.get_team(E))["medic"] == "physio"

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


def test_hidden_migration():
    """히든 능력치 전에 만든 유망주(기본값 8 · 10)는 한 번만 랜덤으로 · 스테로이드로 오른 부상 빈도는 얹는다."""
    cdb.ClubDB()
    old = cdb.HIDDEN_SINCE - 3600
    cols = "user_id, name, nation, position, number, birthday, foot, height, ovr, pot, aged, peak_ovr, peak_age, created_ts"
    for uid, prone in ((801, 8), (802, 11), (803, 8)):
        sql(f"INSERT INTO prospects({cols}, proneness, pro) VALUES(?, 'x', 'y', 'ST', 9, '01-01', '오른발', 180, 55, 80, 17, 55, 17, ?, ?, 10)",
            uid, old if uid != 803 else cdb.HIDDEN_SINCE + 60, prone)
    sql("DELETE FROM club_migrations WHERE name='prospect_hidden_random'")
    cdb.ClubDB()
    got = {u: (pr, p) for u, pr, p in sql("SELECT user_id, proneness, pro FROM prospects WHERE user_id IN (801, 802, 803)")}
    assert 3 <= got[801][0] <= 14 and 6 <= got[802][0] <= 17 and all(4 <= got[u][1] <= 17 for u in (801, 802))
    assert got[803] == (8, 10)                                                    # 배포 뒤에 만든 유망주는 그대로
    sql("UPDATE prospects SET proneness=8, pro=10 WHERE user_id=801")
    cdb.ClubDB()                                                                  # 두 번째 부팅부터는 안 건드린다
    assert sql("SELECT proneness, pro FROM prospects WHERE user_id=801") == [(8, 10)]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
