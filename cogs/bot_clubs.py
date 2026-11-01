# cogs/bot_clubs.py — 3.7.0 🤖 봇 구단 (services/bot_clubs.py)
# 시장이 열린 시간에 10분마다 봇 구단이 매물을 사고, 선수를 올리고, 유저에게 이적 제안을 한다.
# 공개 전(release.released_370 아님)에는 테스트 서버 사람하고만 거래한다 (테스트 서버 먼저).
# 공개 때 한 번: 선수 1명 1주인 정리(중복 카드 환불) — PlayerMarketDB.dedupe_owners_370.
import random
import time

import discord
from discord.ext import commands, tasks

import release
from services import bot_clubs as bc
from services.economy_db import EconomyDB
from services.notifier import send_notify
from services.player_market_db import PlayerMarketDB

BUY_SHARE = 0.6   # 움직일 때 매물 사기 쪽 비율 (나머지는 선수 올리기)


class BotClubs(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.pm = PlayerMarketDB()
        self.money = EconomyDB()
        self.rng = random.Random()
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    # ── 공개 전엔 테스트 서버 사람만 ──
    def _allowed(self):
        if release.released_370():
            return None
        g = self.bot.get_guild(release.TEST_GUILD)
        if g is None:
            return lambda uid: False
        return lambda uid: g.get_member(int(uid)) is not None

    async def _db(self, fn):
        async with self.pm._lock:
            def work():
                con = self.pm._connect()
                try:
                    r = fn(con)
                    con.commit()
                    return r
                finally:
                    con.close()
            return await self.pm._run(work)

    async def run_once(self, now: int, allowed=None, rng=None, notify: bool = True) -> dict:
        """봇 구단 한 차례 — 테스트에서 바로 부른다. → {"buy": n, "list": n, "offer": n}"""
        rng = rng or self.rng
        out = {"buy": 0, "list": 0, "offer": 0}
        await self._db(lambda con: (bc.bootstrap(con, now, rng), bc.weekly_budget(con, now)))
        for bid in bc.BOT_IDS:
            if rng.random() >= bc.ACT_PROB:
                continue
            if rng.random() < BUY_SHARE:
                pick = await self._db(lambda con: bc.pick_listing(con, bid, now, allowed, rng))
                if pick:
                    ok, _msg, info = await self.pm.buy_listing(
                        listing_id=pick[0], buyer_id=bid, qty=1, now_ts=now,
                        get_balance=self.money.get_balance, add_balance=self.money.add_balance)
                    if ok:
                        out["buy"] += 1
                        if notify and info:
                            em = discord.Embed(
                                title="🏷️ 이적시장 매물 판매됨",
                                description=(f"**{bc.bot_name(bid)}**(봇 구단)이 **{info['name']}**을(를) 샀어요.\n"
                                             f"판매가: **{info['price']:,}원** · 수령액: **{info['seller_gets']:,}원** (수수료 5% 제외)"),
                                color=0x2ecc71)
                            await send_notify(self.bot, self.money, info["seller_id"], "매물_판매", em)
            else:
                pick = await self._db(lambda con: bc.pick_to_list(con, bid, rng))
                if pick:
                    ok, _msg = await self.pm.create_listing(seller_id=bid, player_id=pick[0], qty=1,
                                                            price_per=pick[1], now_ts=now)
                    out["list"] += int(ok)
            if rng.random() < bc.OFFER_PROB:
                pick = await self._db(lambda con: bc.pick_offer(con, bid, now, allowed, rng))
                if pick:
                    uid, pid, cash = pick
                    ok, tid, details = await self.pm.create_trade(
                        proposer_id=bid, receiver_id=uid, proposer_pids=[], receiver_pids=[pid],
                        proposer_cash=cash, receiver_cash=0, now_ts=now, get_balance=self.money.get_balance)
                    if ok:
                        out["offer"] += 1
                        await self._db(lambda con: bc.log_offer(con, bid, uid, pid, now))
                        if notify:
                            name = details["receiver_items"][0][1] if details.get("receiver_items") else pid
                            em = discord.Embed(
                                title="🤝 봇 구단 이적 제안",
                                description=(f"**{bc.bot_name(bid)}**이(가) **{name}**을(를) **{cash:,}원**에 데려가고 싶대요.\n"
                                             f"`/트레이드목록` 에서 수락하거나 거절할 수 있어요. (`#{tid}`)"),
                                color=0x3498db)
                            await send_notify(self.bot, self.money, uid, "트레이드_수신", em)
        return out

    async def dedupe(self, now: int) -> int:
        """3.7.0 공개 후 한 번 — 중복 카드 정리 + DM."""
        refunds = await self.pm.dedupe_owners_370(now)
        for uid, name, n, amount in refunds:
            if bc.is_bot(uid):
                continue
            em = discord.Embed(
                title="🔁 선수 1명 1주인 정리",
                description=(f"이제 선수 한 명은 한 사람만 가질 수 있어요.\n"
                             f"겹친 **{name}** {n}장은 지금 시세로 **{amount:,}원** 돌려드렸어요."),
                color=0xf1c40f)
            await send_notify(self.bot, self.money, uid, "선수_뉴스", em)
        if refunds:
            print(f"[3.7.0] 선수 1명 1주인 정리: {len(refunds)}건 환불")
        return len(refunds)

    @tasks.loop(minutes=10)
    async def loop(self):
        now = int(time.time())
        try:
            if release.released_370(now):
                await self.dedupe(now)
            if not self.pm._is_market_open(now):
                return
            r = await self.run_once(now, self._allowed())
            if any(r.values()):
                print(f"[봇 구단] {r}")
        except Exception as e:
            print("❌ [봇 구단] loop error:", repr(e))

    @loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(BotClubs(bot))
