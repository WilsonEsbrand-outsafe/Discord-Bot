# test_archery.py — 2.8 양궁: 착탄 · 점수 · 배당 기대값(타이밍 최상도 0 이하) · 타이밍 몫 30% · 그림 · 버튼 흐름 · 정산
# 실행: venv/Scripts/python.exe test_archery.py
import asyncio
import datetime as dt
import io
import random
import tempfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

import services.economy_db as edb

edb.DB_PATH = Path(tempfile.mkdtemp()) / "t.sqlite3"
import cogs.archery as CA   # noqa: E402
import services.archery as A   # noqa: E402

N = 120_000


def test_geometry_and_table():
    assert A.score(0, 0) == (10, True) and A.score(0.49, 0) == (10, True) and A.score(0.6, 0) == (10, False)
    assert A.score(1.0, 0) == (9, False) and A.score(9.99, 0) == (1, False) and A.score(10, 0) == (0, False)
    assert A.payout(57, 0) == 1.5 and A.payout(56, 0) == 0.4 and A.payout(53, 2) == 0.4 and A.payout(52, 0) == 0
    assert A.payout(49, 0) == 0 and A.payout(48, 0) == -0.6 and A.payout(46, 0) == -0.6 and A.payout(45, 0) == -1
    assert A.payout(60, 6) == 301.5 and A.payout(58, 3) == 2.5 and A.payout(50, 4) == 6 and A.payout(52, 5) == 40
    assert A.timing_error(1.5) == 0 and A.timing_error(0) == 1 and A.timing_error(3.0) == 1 and A.timing_error(None) == 1
    assert A.timing_error(2.25, 4.5) == 0 and abs(A.timing_error(0.75) - 0.5) < 1e-9
    assert A.mult_label(0.4) == "0.4배 수익" and A.mult_label(0) == "본전" and A.mult_label(-0.6) == "0.6배 손실"
    rng = random.Random(1)
    a = A.pick_archer(rng)
    assert 0 <= a.tremor <= 100 and 0 <= a.adapt <= 100 and a.intro
    # 궁수 능력치가 실제 결과에 반영된다: 손이 떨리는 궁수 · 바람에 약한 궁수는 더 흩어진다
    calm = A.Archer("", "", 0, 100, "", "")
    shaky = A.Archer("", "", 100, 0, "", "")

    def spread(arch, wind):
        r = random.Random(3)
        return sum(abs(A.shot(0, arch, wind, r)[0]) for _ in range(20000)) / 20000
    assert spread(shaky, (6.0, 0.0)) > spread(calm, (6.0, 0.0)) + 0.1
    assert spread(calm, (0, 0)) < spread(A.Archer("", "", 0, 0, "", ""), (6.0, 0)) - 0.05


def test_ev():
    """완벽한 타이밍 · 아이템 전부 · 가장 좋은 궁수까지 기대값 0 이하. 보통 타이밍은 더 불리."""
    share = A.timing_share()
    assert 0.27 <= share <= 0.33, share                                   # 타이밍 몫 약 30%
    perfect = A.simulate(N, lambda r: 0.0)
    items = A.simulate(N, lambda r: 0.0, bow=True, windbreak=True)
    best = A.simulate(N, lambda r: 0.0, bow=True, windbreak=True, archer=A.Archer("", "", 2, 92, "", ""))
    sloppy = A.simulate(N, lambda r: r.uniform(0, 1))
    good = A.simulate(N, lambda r: min(1, abs(r.gauss(0, 0.15))))
    print(f"양궁 기대값 — 완벽 {perfect['ev']:+.3f} · 아이템 전부 {items['ev']:+.3f} · 최고 궁수+아이템 {best['ev']:+.3f} · "
          f"잘 맞춤 {good['ev']:+.3f} · 아무 때나 {sloppy['ev']:+.3f} · 평균 {perfect['mean']:.1f}점 · X {perfect['x']:.2f}개")
    print("완벽한 타이밍 배수 분포:", {k: round(v, 5) for k, v in perfect["dist"].items()})
    assert perfect["ev"] < -0.03 and items["ev"] < -0.02 and best["ev"] < 0
    assert sloppy["ev"] < good["ev"] < perfect["ev"] + 0.01
    assert 48 <= perfect["mean"] <= 52                                    # 본전 구간(49~52) 근처가 평균


def test_images():
    gif = CA.aim_gif([(0.2, -0.3), (3, 4)], (4.5, 1.0), A.SWEEP)
    im = Image.open(io.BytesIO(gif))
    assert im.format == "GIF" and im.n_frames == round(A.SWEEP * CA.FPS) + 1 and im.size == (CA.W, CA.H)
    assert len(gif) < 1_500_000
    png = CA.target_png([(0, 0), (9.5, 0)])
    assert Image.open(io.BytesIO(png)).size == (CA.W, CA.H)
    assert "**X**" in CA._scoreboard([(10, True), (9, False), (0, False)]) and "M" in CA._scoreboard([(0, False)])


def _inter(user, sent, views):
    async def arec(*a, **k):
        sent.append(k.get("embed"))
        views.append(k.get("view"))
        return SimpleNamespace(edit=arec)

    async def anoop(*a, **k):
        pass
    return SimpleNamespace(user=user, guild_id=None, response=SimpleNamespace(defer=anoop, edit_message=arec),
                           edit_original_response=arec, followup=SimpleNamespace(send=arec),
                           created_at=dt.datetime.now(dt.timezone.utc))


def test_flow():
    cog = CA.Archery.__new__(CA.Archery)
    cog.db = edb.EconomyDB()
    user = SimpleNamespace(id=5, display_name="궁사", display_avatar=SimpleNamespace(url="https://x/a.png"))
    real_wait = CA.asyncio.wait_for

    async def flow():
        await cog.db.add_balance(user.id, 100_000)
        await cog.db.give_item(user.id, "bow_pro", 1)
        await cog.db.give_item(user.id, "focus", 1)
        sent, views = [], []
        inter = _inter(user, sent, views)
        await CA.Archery.archery.callback(cog, inter, 10_000)
        assert await cog.db.get_balance(user.id) == 90_000                      # 베팅금 먼저
        start = views[-1]
        assert isinstance(start, CA.StartView) and [c.label for c in start.children][0] == "시작!"
        assert any("고급 활" in (c.label or "") for c in start.children) and not any("바람막이" in (c.label or "") for c in start.children)
        await start._toggle("bow_pro")(inter)                                   # 고급 활 켜기
        assert "bow_pro" in start.chosen

        # 발마다 그림이 뜨면 바로 가운데 시각에 누른 것으로
        async def fake_wait(aw, timeout):
            v = next(v for v in reversed(views) if isinstance(v, CA.ShootView))
            v.press_ts = CA.time.time() + A.SWEEP / 2 + A.DISPLAY_LAG
            v.pressed.set()
            return await real_wait(aw, timeout)
        CA.asyncio.wait_for = fake_wait
        try:
            await start._start(inter)
        finally:
            CA.asyncio.wait_for = real_wait
        shoots = [v for v in views if isinstance(v, CA.ShootView)]
        assert len(shoots) == A.ARROWS
        final = sent[-1]
        assert "점" in final.title and "`정산`" in final.description and "`합계`" in final.description
        inv, _ = await cog.db.inventory(user.id)
        assert inv.get("bow_pro", 0) == 0 and inv.get("focus") == 1                # 켠 아이템만 썼다
        bal = await cog.db.get_balance(user.id)
        total = int(final.description.split("`합계` **")[1].split("점")[0])
        xs = int(final.description.split("`X` **")[1].split("개")[0])
        assert bal == 90_000 + 10_000 + int(10_000 * A.payout(total, xs))

        # 60초 안에 시작 안 하면 실격 (베팅금 잃음)
        await CA.Archery.archery.callback(cog, inter, 5_000)
        await views[-1].on_timeout()
        assert "실격" in sent[-1].title and await cog.db.get_balance(user.id) == bal - 5_000
        # 잔액 부족
        poor = SimpleNamespace(id=6, display_name="빈손", display_avatar=user.display_avatar)
        await CA.Archery.archery.callback(cog, _inter(poor, sent, views), 5_000)
        assert "참가할 수 없어요" in sent[-1].title
        # 최대 지급액
        old = A.MAX_PAYOUT
        try:
            A.MAX_PAYOUT = 1
            await cog.db.set_balance(user.id, 50_000)
            await CA.Archery.archery.callback(cog, inter, 10_000)
            CA.asyncio.wait_for = fake_wait
            CA.A.payout = lambda t, x: 300.0
            await views[-1]._start(inter)
            assert await cog.db.get_balance(user.id) == 50_000 + 1 and "최대 지급액" in sent[-1].description
        finally:
            A.MAX_PAYOUT = old
            CA.asyncio.wait_for = real_wait
            CA.A.payout = PAYOUT
    asyncio.run(flow())


PAYOUT = A.payout

if __name__ == "__main__":
    test_geometry_and_table()
    test_ev()
    test_images()
    test_flow()
    print("OK: archery")
