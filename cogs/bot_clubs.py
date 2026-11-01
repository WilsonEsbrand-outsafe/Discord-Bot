# cogs/bot_clubs.py — 3.7.0 🤖 봇 구단 (services/bot_clubs.py)
# 봇 감독마다 접속 시간이 달라서, 1분마다 '지금 접속할 차례인 감독'만 구단을 살펴본다 (services.bot_clubs.next_visit).
# 공개 전(release.released_370 아님)에는 테스트 서버 사람하고만 거래한다 (테스트 서버 먼저).
# 공개 때 한 번: 선수 1명 1주인 전환 (PlayerMarketDB.convert_owners_370 — 유망주 빼고 선수 전부 시세로 환산 + 선수팩 교환권).
import random
import time

import discord
from discord.ext import commands, tasks

import release
from services import bot_clubs as bc
from services.club_db import ClubDB
from services.economy_db import EconomyDB
from services.notifier import send_notify
from services.player_market_db import PlayerMarketDB


class BotClubs(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.pm = PlayerMarketDB()
        self.money = EconomyDB()
        ClubDB()   # clubs · club_lineup 테이블
        self.rng = random.Random()
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    # ── 공개 전엔 테스트 서버 사람만 ──
    def _allowed(self):
        if release.released_370():
            return None
        g = self.bot.get_guild(release.TEST_GUILD) if self.bot else None
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

    async def _notify(self, uid: int, key: str, em: discord.Embed):
        if self.bot is not None and not bc.is_bot(uid):
            await send_notify(self.bot, self.money, uid, key, em)

    async def visit(self, bid: int, now: int, allowed=None, rng=None) -> list[str]:
        """감독 한 명이 접속해서 구단을 살펴본다 → 한 일 목록 (로그 · 테스트용)."""
        rng = rng or self.rng
        did = []
        name = bc.bot_name(bid)
        # 1) 받은 트레이드 제안
        for tid in await self._db(lambda con: bc.pending_trades(con, bid)):
            verdict = await self._db(lambda con: bc.judge_trade(con, bid, tid))
            if verdict is None:
                continue
            if verdict:
                ok, _msg, proposer = await self.pm.accept_trade(trade_id=tid, receiver_id=bid, now_ts=now,
                                                                get_balance=self.money.get_balance,
                                                                add_balance=self.money.add_balance)
            else:
                ok, _msg, proposer = await self.pm.reject_trade(trade_id=tid, receiver_id=bid)
            if ok:
                did.append(f"trade:{tid}:{'accept' if verdict else 'reject'}")
                if proposer:
                    await self._notify(proposer, "트레이드_결과", discord.Embed(
                        title="🤝 트레이드 수락됨" if verdict else "🤝 트레이드 거절됨",
                        description=f"**{name}**이(가) 트레이드 `#{tid}`를 {'수락했어요!' if verdict else '거절했어요.'}",
                        color=0x2ecc71 if verdict else 0xe74c3c))
        # 오래 안 팔린 매물은 내린다
        for lid in await self._db(lambda con: bc.stale_listings(con, bid, now)):
            ok, _ = await self.pm.cancel_listing(listing_id=lid, seller_id=bid)
            if ok:
                did.append(f"unlist:{lid}")
        await self._db(lambda con: bc.set_lineup(con, bid))
        # 2) 할 만한 일 — 매번 다 하지 않는다 (성향마다 의욕이 다르다)
        p = bc.PERSONALITY[bc.kind(bid)]
        for _ in range(rng.randint(1, bc.SESSION_MAX)):
            if rng.random() > p["act"]:
                break
            if rng.random() < 0.6:
                up = await self._db(lambda con: bc.find_upgrade(con, bid, now, allowed))
                if up and await self._sign(bid, up, now):
                    did.append(f"{up['how']}:{up['player_id']}")
                    await self._db(lambda con: bc.set_lineup(con, bid))
                    continue
            sell = await self._db(lambda con: bc.pick_to_sell(con, bid, rng))
            if sell:
                ok, _ = await self.pm.create_listing(seller_id=bid, player_id=sell[0], qty=1, price_per=sell[1], now_ts=now)
                if ok:
                    did.append(f"list:{sell[0]}")
        await self._db(lambda con: (bc.set_lineup(con, bid), bc.schedule(con, bid, now, rng)))
        return did

    async def _sign(self, bid: int, up: dict, now: int) -> bool:
        name = bc.bot_name(bid)
        tok = release.GUILD.set(release.TEST_GUILD if not release.released_370() else None)   # 봇 거래는 1명 1주인 규칙으로
        try:
            if up["how"] == "free":
                if not self.pm._is_market_open(now):
                    return False
                ok, _ = await self.pm.buy_from_market(user_id=bid, player_id=up["player_id"], qty=1, now_ts=now,
                                                      get_balance=self.money.get_balance, add_balance=self.money.add_balance)
                return ok
            if up["how"] == "listing":
                ok, _msg, info = await self.pm.buy_listing(listing_id=up["listing_id"], buyer_id=bid, qty=1, now_ts=now,
                                                           get_balance=self.money.get_balance, add_balance=self.money.add_balance)
                if ok and info:
                    await self._notify(info["seller_id"], "매물_판매", discord.Embed(
                        title="🏷️ 이적시장 매물 판매됨",
                        description=(f"**{name}**(봇 구단)이 **{info['name']}**을(를) 샀어요.\n"
                                     f"판매가: **{info['price']:,}원** · 수령액: **{info['seller_gets']:,}원** (수수료 5% 제외)"),
                        color=0x2ecc71))
                return ok
            ok, tid, details = await self.pm.create_trade(
                proposer_id=bid, receiver_id=up["user_id"], proposer_pids=[], receiver_pids=[up["player_id"]],
                proposer_cash=up["price"], receiver_cash=0, now_ts=now, get_balance=self.money.get_balance)
            if ok:
                await self._db(lambda con: bc.log_offer(con, bid, up["user_id"], up["player_id"], now))
                pname = details["receiver_items"][0][1] if details.get("receiver_items") else up["player_id"]
                await self._notify(up["user_id"], "트레이드_수신", discord.Embed(
                    title="🤝 봇 구단 이적 제안",
                    description=(f"**{name}**이(가) **{pname}**을(를) **{up['price']:,}원**에 데려가고 싶대요.\n"
                                 f"`/트레이드목록`에서 수락하거나 거절할 수 있어요. (`#{tid}`)"),
                    color=0x3498db))
            return ok
        finally:
            release.GUILD.reset(tok)

    async def run_once(self, now: int, allowed=None, rng=None) -> dict:
        """접속할 차례인 감독들만 → {bot_id: 한 일}."""
        rng = rng or self.rng
        await self._db(lambda con: (bc.bootstrap(con, now, rng), bc.weekly_budget(con, now)))
        out = {}
        for bid in await self._db(lambda con: bc.due_bots(con, now)):
            out[bid] = await self.visit(bid, now, allowed, rng)
        return out

    async def convert(self, now: int) -> int:
        """3.7.0 공개 후 한 번 — 선수 1명 1주인 전환 + DM."""
        res = await self.pm.convert_owners_370(now)
        for uid, info in res.items():
            if bc.is_bot(uid):
                continue
            await ClubDB().fill_empty(uid)
            await self._notify(uid, "선수_뉴스", discord.Embed(
                title="🔁 선수 1명 1주인 — 선수 카드 정산",
                description=(f"이제 선수 한 명은 한 사람만 가질 수 있어요. 새 출발을 위해 가지고 있던 선수 카드 "
                             f"**{info['cards']}장**(유망주 제외)을 정리했어요.\n"
                             f"💰 지금 시세로 **{info['money']:,}원**을 넣어 드렸고, "
                             f"🎫 **골드팩 교환권 {info['tickets']}장**도 드렸어요 (`/선수팩상점`에서 골드팩을 공짜로).\n"
                             "선발 빈자리는 아마추어 선수로 채워 뒀어요."),
                color=0xf1c40f))
        if res:
            print(f"[3.7.0] 선수 1명 1주인 전환: {len(res)}명 정산")
            await self._db(lambda con: con.execute("DELETE FROM bot_club_state"))   # 봇 구단도 새로 시작
        return len(res)

    @tasks.loop(minutes=1)
    async def loop(self):
        now = int(time.time())
        try:
            if release.released_370(now):
                await self.convert(now)
            r = await self.run_once(now, self._allowed())
            for bid, did in r.items():
                if did:
                    print(f"[봇 구단] {bc.bot_name(bid)}: {', '.join(did)}")
        except Exception as e:
            print("❌ [봇 구단] loop error:", repr(e))

    @loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(BotClubs(bot))
