# test_sponsor.py — 스폰서 계약·정산·등급·구단 보너스·자동 재계약·해지 검증.  실행: venv/Scripts/python.exe test_sponsor.py
import asyncio
import math
import random
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import services.club_db as cdb
import services.economy_db as edb

edb.DB_PATH = cdb.DB_PATH = Path(tempfile.mkdtemp()) / "t.sqlite3"

import cogs.sponsor as cs  # noqa: E402
import services.sponsor_db as sdb  # noqa: E402
from cogs.sponsor import Sponsor  # noqa: E402

NOW = int(time.time()) - 400 * 86400   # 과거에 맺은 계약 — 명령어 화면에선 전부 만기
U, D = 77, 86400


def test_rules():
    days = sorted(sdb.TERMS)
    assert days == [1, 7, 30, 90, 365] and len(sdb.SPONSORS) == 5
    per_day = [math.log1p(sdb.TERMS[d]) / d for d in days]
    assert per_day == sorted(per_day), per_day                    # 짧은 계약 복리 < 긴 계약
    assert sdb.SPONSORS["bank"][3:5] == (1.0, 1.0)                # 원금 보장 스폰서가 하나는 있다
    rng = random.Random(1)
    for key in sdb.SPONSORS:
        for d in days:
            for _ in range(200):
                payout, perf = sdb.settle_amount(key, d, 1_000_000, 0.35, rng)
                assert payout >= 0                                  # 원금 이상은 잃지 않는다
                if sdb.SPONSORS[key][3] >= 0:
                    assert payout >= 1_000_000                      # 하한이 0 이상이면 원금 보장
    # 보너스는 플러스 수익에만: 골든뱅크 30일 100만원 = +12만 → 보너스 25%면 +15만
    assert sdb.settle_amount("bank", 30, 1_000_000, 0.25)[0] == 1_150_000
    assert sdb.cancel_refund(100_000) == 95_000
    # 등급: 신뢰도 구간 · 한도와 보너스가 오를수록 커진다
    assert [sdb.grade_of(r)[0] for r in (0, 29, 30, 119, 120, 365, 999, 1000, 5000)] == [0, 0, 1, 1, 2, 3, 3, 4, 4]
    assert [g[2] for g in sdb.GRADES] == sorted(g[2] for g in sdb.GRADES) and sdb.MAX_AMOUNT == sdb.GRADES[-1][2]
    assert (sdb.club_bonus(None), sdb.club_bonus(59), sdb.club_bonus(60), sdb.club_bonus(75), sdb.club_bonus(90)) == \
        (0.0, 0.0, 0.03, 0.06, 0.10)


async def _flow():
    db = sdb.SponsorDB()
    await db.add_balance(U, 500_000_000)
    r = await db.open(U, "bank", 7, 1_000_000, NOW)
    assert r["ok"] and r["balance"] == 499_000_000 and r["grade"].endswith("신규")
    assert (await db.open(U, "rocket", 1, 10**12, NOW))["reason"] == "limit"     # 신규 등급 한도 1,000만원
    assert (await db.open(U, "rocket", 1, 10_000_000, NOW))["ok"]
    for _ in range(sdb.MAX_ACTIVE - 2):
        assert (await db.open(U, "tv", 30, 1_000_000, NOW))["ok"]
    assert (await db.open(U, "tv", 30, 1_000_000, NOW))["reason"] == "full"

    res = (await db.settle(NOW + D * 6, U))[U]["done"]
    assert [c["sponsor"] for c in res] == ["rocket"]                          # 1일짜리만 만기
    res = (await db.settle(NOW + D * 7, U))[U]["done"]
    assert len(res) == 1 and res[0]["payout"] == 1_025_000 and not res[0]["renewed"]
    assert await db.settle(NOW + D * 7, U) == {}                               # 두 번 정산되지 않는다

    tv = (await db.active(U))[0]["id"]
    r = await db.cancel(U, tv, NOW + D)
    assert r["ok"] and r["refund"] == 950_000
    assert (await db.cancel(U, tv, NOW + D))["reason"] == "missing"
    other = (await db.active(U))[0]["id"]
    assert (await db.cancel(U, other, NOW + D * 30))["reason"] == "matured"
    assert (await db.cancel(999, other, NOW))["reason"] == "missing"          # 남의 계약은 해지 불가
    await db.settle(NOW + D * 30, U)

    # 등급: 골든뱅크 100만원 이상 30일 채우면 파트너 → 한도 1,500만원 · 수익 +5%
    V = 88
    await db.add_balance(V, 100_000_000)
    assert (await db.open(V, "bank", 30, 900_000, NOW))["ok"]                 # 100만원 미만은 신뢰도 X
    assert (await db.open(V, "bank", 30, 1_000_000, NOW))["ok"]
    res = (await db.settle(NOW + D * 30, V))[V]["done"]
    assert [c["grade_up"] for c in res] == [None, "🥈 파트너"]
    assert await db.reputation(V) == {"bank": 30}
    assert (await db.open(V, "bank", 30, 15_000_000, NOW + D * 30))["grade_bonus"] == 0.05
    assert (await db.open(V, "bank", 30, 15_000_001, NOW + D * 30))["reason"] == "limit"

    # 자동 재계약: 같은 조건으로 다시 맺고, 원금은 지급액에서 뗀다 · 구단 보너스는 이어진다
    W = 99
    await db.add_balance(W, 10_000_000)
    cid = (await db.open(W, "bank", 7, 2_000_000, NOW, club_b=0.10, auto=True))["id"]
    res = (await db.settle(NOW + D * 7, W))[W]
    c = res["done"][0]
    assert c["payout"] == 2_000_000 + round(2_000_000 * 0.025 * 1.10) and c["renewed"] and c["renew_amount"] == 2_000_000
    new = (await db.active(W))[0]
    assert new["id"] == c["renewed"] != cid and new["auto"] == 1 and new["club_bonus"] == 0.10
    assert res["balance"] == 8_000_000 + c["payout"] - 2_000_000
    assert await db.set_auto(W, new["id"], False) and not (await db.active(W))[0]["auto"]
    assert not await db.set_auto(U, new["id"], True)                          # 남의 계약은 못 바꾼다

    # 자동 정산 루프: 만기된 계약을 정산하고 DM 알림을 보낸다
    cog = Sponsor.__new__(Sponsor)
    cog.db, cog.clubs, cog.bot = db, cdb.ClubDB(), None
    dms = []
    async def fake_notify(bot, _db, uid, key, embed):
        dms.append((uid, key, embed))
    real, cs.send_notify = cs.send_notify, fake_notify
    try:
        await Sponsor.settle_task.coro(cog)
    finally:
        cs.send_notify = real
    assert {(uid, key) for uid, key, _ in dms} >= {(V, "스폰서_만기"), (W, "스폰서_만기")}
    assert all("만기 정산" in e.title for *_, e in dms)

    # 명령어 화면: 계약 → 현황 → 재계약 토글 → 해지 확인까지 깨지지 않는다
    user = SimpleNamespace(id=U, display_name="구단주", display_avatar=SimpleNamespace(url="https://x/a.png"))
    sent = []
    async def rec(*a, **k):
        sent.append(k)
    async def noop(*a, **k):
        pass
    inter = SimpleNamespace(user=user, response=SimpleNamespace(defer=noop, send_message=rec, edit_message=rec),
                            followup=SimpleNamespace(send=rec))
    await Sponsor.open_contract.callback(cog, inter, "rocket", 365, 2_000_000, True)
    e = sent[-1]["embed"]
    assert "계약 체결" in e.title and "-2,000,000원" in e.description and "자동 재계약" in e.description
    await Sponsor.overview.callback(cog, inter)
    assert len(sent[-1]["embeds"]) == 1 and "🔁" in sent[-1]["embeds"][0].description
    cid = (await db.active(U))[0]["id"]
    await Sponsor.toggle_auto.callback(cog, inter, str(cid), False)
    assert "OFF" in sent[-1]["embed"].title
    before = await db.get_balance(U)
    await Sponsor.cancel_contract.callback(cog, inter, str(cid))
    await sent[-1]["view"].children[0].callback(inter)                        # [해지하기]
    assert "해지" in sent[-1]["embed"].title and await db.get_balance(U) == before + 1_900_000


def test_flow():
    asyncio.run(_flow())


if __name__ == "__main__":
    test_rules()
    test_flow()
    print("OK: sponsor")
