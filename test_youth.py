# test_youth.py — 2.8 유스: 등급 확률 · 지원자 · 입단 테스트(합격 · 불합격 환불 · 하루 1번 · 슬롯) · 훈련(하루 2번 · 특훈권)
# · 주간 연습경기 · 잠재력 공개 · 판매가(졸업 뒤 감소) · 1군 등록(등록권 · 등록비 · 상한 · 주 1회 · 명단 · 35세 은퇴)
# · 기대 수익 시뮬레이션 · 허브 🎓 유스 화면 버튼 · 계정 삭제
# 실행: venv/Scripts/python.exe test_youth.py
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

import cogs.hub as CH   # noqa: E402
import release   # noqa: E402
import services.youth as Y   # noqa: E402

D = 86400
T0 = 1_800_000_000
U = 77


def test_pure():
    for lv in range(6):
        o = Y.grade_odds(lv)
        assert abs(sum(o) - 1) < 1e-9
    assert [round(x * 100) for x in Y.grade_odds(0)] == [50, 35, 13, 2]
    assert [round(x * 100) for x in Y.grade_odds(5)] == [25, 40, 27, 8]
    rng = random.Random(1)
    a = Y.make_applicant(3, rng)
    assert 15 <= a["age"] <= 16 and a["grade"] == Y.grade_of(a["pot"]) and a["personality"] in Y.PERSONALITIES
    # 성장은 잠재력을 넘지 않는다
    assert 69.9 < Y.grow(50.0, 70, 1000, 2.0) <= 70
    assert 50 < Y.grow(50.0, 70, 1, 1.0) < 51
    # 졸업 뒤 판매가 감소 · 시장 열기 범위
    y = {"id": 1, "join_age": 16, "joined_ts": T0, "ovr": 66.0, "pot": 72}
    p19 = Y.sell_price(y, T0 + 3 * Y.YEAR)
    p19w = Y.sell_price(y, T0 + 3 * Y.YEAR + 7 * D)
    h1, h2 = Y.heat(T0 + 3 * Y.YEAR), Y.heat(T0 + 3 * Y.YEAR + 7 * D)
    assert abs(p19w / h2 - p19 / h1 * Y.AFTER_GRAD_DECAY) / (p19 / h1) < 0.01
    assert all(0.8 <= Y.heat(T0 + i * D) <= 1.3 for i in range(60))
    # 기대 수익: 레벨이 높을수록 크고 Lv.0 에서도 조금 남는다 (테스트비 포함)
    s0, s5 = Y.simulate_value(0, 6000), Y.simulate_value(5, 6000)
    assert 0 < s0["ev"] < s5["ev"] < 600_000, (s0["ev"], s5["ev"])
    assert s5["by_grade"]["S"][0] <= Y.REGISTER_CAP[5] + 2


def test_db():
    pmdb.PlayerMarketDB()
    db = Y.YouthDB()
    clubs = cdb.ClubDB()

    async def flow():
        await clubs.create_club(U, "유스 FC", 1)
        await db.add_balance(U, 5_000_000)
        st = await db.status(U, T0)
        assert st["slots"] == 2 and st["level"] == 0 and not st["youth"]
        r = await db.open_tryout(U, T0, random.Random(2))
        assert r["ok"] and len(r["applicants"]) == Y.APPLICANTS[0]
        again = await db.open_tryout(U, T0, random.Random(3))
        assert again["applicants"] == r["applicants"]   # 같은 날은 같은 지원자
        # 꼭 합격 / 불합격하게 장면 확률 고정
        Y.SCENE_PASS, keep = (1, 1, 1, 1), Y.SCENE_PASS
        t = await db.tryout(U, 0, T0)
        assert t["ok"] and t["passed"] and t["refund"] == 0 and t["balance"] == 5_000_000 - Y.TRYOUT_FEE
        assert (await db.tryout(U, 1, T0))["reason"] == "done"
        assert (await db.open_tryout(U, T0))["reason"] == "done"
        Y.SCENE_PASS = (0, 0, 0, 0)
        await db.open_tryout(U, T0 + D, random.Random(4))
        t2 = await db.tryout(U, 0, T0 + D)
        assert not t2["passed"] and t2["refund"] == Y.TRYOUT_FEE // 2
        assert t2["balance"] == 5_000_000 - Y.TRYOUT_FEE - Y.TRYOUT_FEE // 2
        Y.SCENE_PASS = (1, 1, 1, 1)
        # 쿠폰: 무료 + 2명
        await db.give_item(U, "scout_coupon")
        c = await db.open_tryout(U, T0 + 2 * D, random.Random(5), coupon=True)
        assert len(c["applicants"]) == Y.APPLICANTS[0] + 2 and c["applicants"][0]["free"]
        bal = (await db.status(U, T0 + 2 * D))["items"]
        assert bal["scout_coupon"] == 0
        t3 = await db.tryout(U, 0, T0 + 2 * D)
        assert t3["passed"] and t3["fee"] == 0
        # 슬롯 2개 꽉 참
        assert (await db.open_tryout(U, T0 + 3 * D))["reason"] == "full"
        Y.SCENE_PASS = keep
        st = await db.status(U, T0 + 3 * D)
        y = st["youth"][0]
        # 훈련: 하루 2번 + 특훈권 1번
        assert (await db.train(U, y["id"], T0 + 3 * D))["ok"]
        assert (await db.train(U, y["id"], T0 + 3 * D))["left"] == 0
        assert (await db.train(U, y["id"], T0 + 3 * D))["reason"] == "limit"
        assert (await db.train(U, y["id"], T0 + 3 * D, ticket=True))["reason"] == "ticket"
        await db.give_item(U, "special_training")
        assert (await db.train(U, y["id"], T0 + 3 * D, ticket=True))["ok"]
        assert (await db.train(U, y["id"], T0 + 4 * D))["ok"]   # 다음 날 다시
        # 주간 연습경기: 전원 +3 훈련 · 주 1회
        m = await db.weekly_match(U, T0 + 4 * D, random.Random(1))
        assert m["ok"] and len(m["grown"]) == 2 and all(b > a for _, a, b in m["grown"])
        assert (await db.weekly_match(U, T0 + 4 * D))["reason"] == "done"
        st = await db.status(U, T0 + 4 * D)
        y = next(v for v in st["youth"] if v["id"] == y["id"])
        assert y["trains"] == 4 + Y.MATCH_TRAINS
        # 잠재력 공개 단계
        assert Y.pot_text({**y, "trains": 0}).endswith("등급")
        assert "~" in Y.pot_text({**y, "trains": 15}) and str(y["pot"]) in Y.pot_text({**y, "trains": 30})
        # 1군 등록: 등록권 · 상한 · 등록비 · 주 1회
        other = st["youth"][1]
        assert (await db.register(U, y["id"], T0 + 4 * D))["reason"] in ("item", "cap")
        await db.give_item(U, "first_team", 2)
        con = db._connect()
        con.execute("UPDATE youth_players SET ovr=? WHERE id=?", (Y.REGISTER_CAP[0] + 1, y["id"]))
        con.execute("UPDATE youth_players SET ovr=? WHERE id=?", (Y.REGISTER_CAP[0] - 1, other["id"]))
        con.commit()
        con.close()
        assert (await db.register(U, y["id"], T0 + 4 * D))["reason"] == "cap"
        bal0 = await db.get_balance(U)
        reg = await db.register(U, other["id"], T0 + 4 * D)
        assert reg["ok"] and reg["ovr"] == Y.REGISTER_CAP[0] - 1 and reg["balance"] == bal0 - Y.REGISTER_FEE
        assert (await db.register(U, y["id"], T0 + 4 * D))["reason"] in ("week", "cap")
        # 구단 명단에 들어간다 · 선수 카드(pm_players)에는 없다
        sq = await clubs.squad(U)
        yt = [p for p in sq if p["player_id"] == f"YT{other['id']}"]
        assert yt and yt[0]["youth"] and yt[0]["ovr"] == Y.REGISTER_CAP[0] - 1
        con = db._connect()
        assert not con.execute("SELECT 1 FROM pm_players WHERE player_id LIKE 'YT%'").fetchone()
        con.close()
        ok, msg = await clubs.set_slot(U, 10, f"YT{other['id']}")
        assert ok, msg
        # 판매
        bal1 = await db.get_balance(U)
        s = await db.sell(U, y["id"], T0 + 4 * D)
        assert s["ok"] and s["balance"] == bal1 + s["price"] and s["price"] > 0
        assert (await db.sell(U, y["id"], T0 + 4 * D))["reason"] == "none"
        # 35세 은퇴: 시간이 흘러가면 명단에서 빠진다
        con = db._connect()
        con.execute("UPDATE youth_players SET joined_ts=? WHERE id=?", (int(Y.now()) - 25 * Y.YEAR, other["id"]))
        con.commit()
        con.close()
        assert not [p for p in await clubs.squad(U) if p["player_id"] == f"YT{other['id']}"]
        # 계정 삭제
        await db.delete_user(U)
        con = db._connect()
        assert not con.execute("SELECT 1 FROM youth_players WHERE user_id=?", (U,)).fetchone()
        con.close()
    asyncio.run(flow())


class Resp:
    def __init__(self, log):
        self.log, self.done = log, False

    def is_done(self):
        return self.done

    async def send_message(self, *a, **k):
        self.done = True
        self.log.append((k.get("embeds") or [k.get("embed")], k.get("view")))

    edit_message = send_message

    async def defer(self, *a, **k):
        self.done = True


def _inter(log, user):
    async def edit_orig(*a, **k):
        log.append((k.get("embeds") or [k.get("embed")], k.get("view")))
    return SimpleNamespace(user=user, guild_id=release.TEST_GUILD, response=Resp(log), edit_original_response=edit_orig)


def _btn(v, label):
    return next(c for c in v.children if (getattr(c, "label", "") or "").startswith(label))


def test_hub_page():
    uid = 78
    user = SimpleNamespace(id=uid, display_name="유스", display_avatar=SimpleNamespace(url="https://x/a.png"))
    hubs = CH.Hubs.__new__(CH.Hubs)
    hubs.bot, hubs.db, hubs.clubs, hubs.youth = None, edb.EconomyDB(), cdb.ClubDB(), Y.YouthDB()
    hubs.SCENE_DELAY = 0

    async def flow():
        await hubs.clubs.create_club(uid, "유스 허브", 1)
        await hubs.db.add_balance(uid, 3_000_000)
        v = hubs.club_view(user)
        v.page = next(i for i, p in enumerate(v.pages) if p.title == "유스")
        e = await v.build()
        assert "슬롯 **0/2**" in e.description
        log = []
        await _btn(v, "지원자 보기").callback(_inter(log, user))
        e = log[-1][0][-1]
        assert "오늘의 지원자" in e.description
        sel = next(c for c in v.children if isinstance(c, CH.discord.ui.Select) and c.row == 3)
        sel._values = ["0"]   # noqa: SLF001 — 테스트에서 고른 값 넣기
        Y.SCENE_PASS, keep = (1, 1, 1, 1), Y.SCENE_PASS
        await sel.callback(_inter(log, user))
        Y.SCENE_PASS = keep
        note, page = log[-1][0]
        assert "합격" in note.description and "1/2" in page.description
        # 훈련 버튼
        await _btn(v, "훈련").callback(_inter(log, user))
        assert "훈련!" in log[-1][0][0].title
        # 판매는 두 번 눌러야
        await _btn(v, "판매").callback(_inter(log, user))
        assert len(log[-1][0]) == 1
        await _btn(v, "정말 판매").callback(_inter(log, user))
        assert "판매" in log[-1][0][0].title and "0/2" in log[-1][0][1].description
        # 연습경기: 선수가 없으면 비활성
        assert _btn(v, "주간 연습경기").disabled
        rows = {}
        for c in v.children:
            rows[c.row] = rows.get(c.row, 0) + (5 if isinstance(c, CH.discord.ui.Select) else 1)
        assert all(n <= 5 for n in rows.values())
    asyncio.run(flow())


if __name__ == "__main__":
    test_pure()
    test_db()
    test_hub_page()
    s0, s5 = Y.simulate_value(0), Y.simulate_value(5)
    print(f"기대 수익(테스트 1번당): Lv.0 {s0['ev']:,.0f}원 · Lv.5 {s5['ev']:,.0f}원")
    for g in "CBAS":
        print(f"  {g}: Lv.0 졸업 OVR {s0['by_grade'][g][0]:.1f} 판매가 {s0['by_grade'][g][1]:,.0f} · "
              f"Lv.5 {s5['by_grade'][g][0]:.1f} {s5['by_grade'][g][1]:,.0f}")
    print("OK: youth")
