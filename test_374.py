"""3.7.4: 파산 신청 쿨타임 30분 · 변호사로 한 번 더 신청해도 돌던 쿨타임은 그대로 (테스트 서버 먼저)."""
import asyncio
import random

import release
from services import economy_db as edb


def test_switch():
    assert release.preview_374(release.TEST_GUILD)


def test_bankrupt_30min_and_lawyer_keeps_clock():
    db = edb.EconomyDB()

    async def flow():
        uid, t = 3740, 1_000_000
        await db.add_balance(uid, -1_000_000)
        r = await db.declare_bankruptcy(uid, t, random.Random(1), new=True, v311=True, v374=True)
        assert r["ok"] and r["next"] == t + 1800
        await db.add_balance(uid, -500_000 - max(0, r["balance"]))
        r = await db.declare_bankruptcy(uid, t + 600, random.Random(2), new=True, v311=True, v374=True)
        assert not r["ok"] and r["until"] == t + 1800          # 30분 쿨타임
        await db._tx(lambda con: edb.give_item(con, uid, "lawyer"))
        await db.hire_lawyer(uid, uid)
        r = await db.declare_bankruptcy(uid, t + 600, random.Random(3), new=True, v311=True, v374=True)
        assert r["ok"] and r["extra"] and r["next"] == t + 1800  # 변호사로 한 번 더 · 쿨타임 시계는 그대로
        await db.add_balance(uid, -500_000 - max(0, r["balance"]))
        r = await db.declare_bankruptcy(uid, t + 1200, random.Random(4), new=True, v311=True, v374=True)
        assert not r["ok"] and r["until"] == t + 1800
        r = await db.declare_bankruptcy(uid, t + 1800, random.Random(5), new=True, v311=True, v374=True)
        assert r["ok"] and not r["extra"]                     # 처음 신청한 지 30분 뒤엔 바로 가능
    asyncio.run(flow())
