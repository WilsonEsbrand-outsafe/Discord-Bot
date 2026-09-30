# test_sponsor.py — 스폰서 계약·정산·해지 검증.  실행: venv/Scripts/python.exe test_sponsor.py
import asyncio
import math
import random
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import services.economy_db as edb

edb.DB_PATH = Path(tempfile.mkdtemp()) / "t.sqlite3"

import services.sponsor_db as sdb  # noqa: E402
from cogs.sponsor import Sponsor  # noqa: E402

NOW = int(time.time()) - 400 * 86400   # 과거에 맺은 계약 — 명령어 화면에선 전부 만기
U = 77


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
                payout, perf = sdb.settle_amount(key, d, 1_000_000, rng)
                assert payout >= 0                                  # 원금 이상은 잃지 않는다
                if sdb.SPONSORS[key][3] >= 0:
                    assert payout >= 1_000_000                      # 하한이 0 이상이면 원금 보장
    assert sdb.cancel_refund(100_000) == 95_000


async def _flow():
    db = sdb.SponsorDB()
    await db.add_balance(U, 50_000_000)
    r = await db.open(U, "bank", 7, 1_000_000, NOW)
    assert r["ok"] and r["balance"] == 49_000_000
    assert (await db.open(U, "rocket", 1, 10**9, NOW))["reason"] == "balance"
    for _ in range(sdb.MAX_ACTIVE - 1):
        assert (await db.open(U, "tv", 30, 1_000_000, NOW))["ok"]
    assert (await db.open(U, "tv", 30, 1_000_000, NOW))["reason"] == "full"

    done, bal = await db.settle(U, NOW + 86400 * 6)                # 아직 만기 전
    assert done == [] and bal is None
    done, bal = await db.settle(U, NOW + 86400 * 7)
    assert len(done) == 1 and done[0]["payout"] == 1_025_000 and bal == 45_000_000 + 1_025_000
    assert (await db.settle(U, NOW + 86400 * 7))[0] == []          # 두 번 정산되지 않는다

    tv = (await db.active(U))[0]["id"]
    r = await db.cancel(U, tv, NOW + 86400)
    assert r["ok"] and r["refund"] == 950_000 and r["balance"] == bal + 950_000
    assert (await db.cancel(U, tv, NOW + 86400))["reason"] == "missing"
    other = (await db.active(U))[0]["id"]
    assert (await db.cancel(U, other, NOW + 86400 * 30))["reason"] == "matured"
    assert (await db.cancel(999, other, NOW))["reason"] == "missing"    # 남의 계약은 해지 불가

    # 명령어 화면: 계약 → 현황(자동 정산) → 해지 확인까지 깨지지 않는다
    cog = Sponsor.__new__(Sponsor)
    cog.db = db
    user = SimpleNamespace(id=U, display_name="구단주", display_avatar=SimpleNamespace(url="https://x/a.png"))
    sent = []
    async def rec(*a, **k):
        sent.append(k)
    async def noop(*a, **k):
        pass
    inter = SimpleNamespace(user=user, response=SimpleNamespace(defer=noop, send_message=rec, edit_message=rec),
                            followup=SimpleNamespace(send=rec))
    await Sponsor.overview.callback(cog, inter)
    embeds = sent[-1]["embeds"]
    assert len(embeds) == 2 and "만기 정산" in embeds[0].title    # 만기 지난 계약 3건이 먼저 정산된다
    assert await db.active(U) == []

    await Sponsor.open_contract.callback(cog, inter, "rocket", 365, 2_000_000)
    assert "계약 체결" in sent[-1]["embed"].title and "-2,000,000원" in sent[-1]["embed"].description
    cid = (await db.active(U))[0]["id"]
    before = await db.get_balance(U)
    await Sponsor.cancel_contract.callback(cog, inter, str(cid))
    view = sent[-1]["view"]
    await view.children[0].callback(inter)                           # [해지하기]
    assert "해지" in sent[-1]["embed"].title and await db.get_balance(U) == before + 1_900_000
    await Sponsor.overview.callback(cog, inter)
    assert len(sent[-1]["embeds"]) == 1                              # 정산할 게 없으면 현황만


def test_flow():
    asyncio.run(_flow())


if __name__ == "__main__":
    test_rules()
    test_flow()
    print("OK: sponsor")
