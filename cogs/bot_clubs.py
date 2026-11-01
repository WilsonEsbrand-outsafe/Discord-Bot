# cogs/bot_clubs.py — 3.7.0 🤖 봇 구단 (services/bot_clubs.py)
# 봇 감독마다 접속 시간이 달라서, 1분마다 '지금 접속할 차례인 감독'만 구단을 살펴본다 (services.bot_clubs.next_visit).
# 접속하면 유저처럼: 하루 일과 → 받은 트레이드(수락 · 거절 · 역제안) → 공식경기 → 보강 · 팩 · 되팔기 · 판매.
# 공개 전(release.released_370 아님)에는 테스트 서버 사람하고만 거래 · 경기하고, 주간 랭킹 · 상금은 공개 뒤부터.
# 공개 때 한 번: 선수 1명 1주인 전환 (PlayerMarketDB.convert_owners_370 — 유망주 빼고 선수 전부 시세로 환산 + 얼티밋팩 교환권).
import random
import time

import discord
from discord.ext import commands, tasks

import release
from services import bot_clubs as bc
from services.club_db import (ClubDB, PRIZE_STAKE_370, WEEKLY_DAILY, kst_day, official_odds, simulate_match, week_key,
                              win_probs)
from services.economy_db import EconomyDB
from services.notifier import send_notify
from services.player_market_db import PlayerMarketDB

MATCH_PLAYS = (0, 1, 1, 2, 2, 3, 4)   # 한 번 접속해서 뛰는 공식경기 수 (사람처럼 들쭉날쭉)


class BotClubs(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.pm = PlayerMarketDB()
        self.money = EconomyDB()
        self.clubs = ClubDB()
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

    def _ctx(self):
        """봇이 하는 일은 3.7.0 규칙(1명 1주인 · 봇 상대)으로 — 공개 전엔 테스트 서버 상호작용처럼."""
        return release.GUILD.set(None if release.released_370() else release.TEST_GUILD)

    # ───────────── 한 번 접속 ─────────────
    async def visit(self, bid: int, now: int, allowed=None, rng=None) -> list[str]:
        """감독 한 명이 접속해서 구단을 살펴본다 → 한 일 목록 (로그 · 테스트용)."""
        rng = rng or self.rng
        tok = self._ctx()
        try:
            return await self._visit(bid, now, allowed, rng)
        finally:
            release.GUILD.reset(tok)

    async def _visit(self, bid: int, now: int, allowed, rng) -> list[str]:
        did = []
        earn = await self._db(lambda con: bc.daily_routine(con, bid, now, rng))
        if earn:
            did.append(f"routine:+{earn}")
        did += await self._answer_trades(bid, now, rng)
        for lid in await self._db(lambda con: bc.stale_listings(con, bid, now)):   # 오래 안 팔린 매물은 내린다
            ok, _ = await self.pm.cancel_listing(listing_id=lid, seller_id=bid)
            if ok:
                did.append(f"unlist:{lid}")
        await self._db(lambda con: bc.set_lineup(con, bid))
        did += await self._play_officials(bid, now, allowed, rng)
        # 할 만한 일 — 매번 다 하지 않는다 (성향마다 의욕이 다르다)
        p = bc.PERSONALITY[bc.kind(bid)]
        for _ in range(rng.randint(1, bc.SESSION_MAX)):
            if rng.random() > p["act"]:
                break
            roll = rng.random()
            if roll < 0.5:
                up = await self._db(lambda con: bc.find_upgrade(con, bid, now, allowed))
                if up and await self._sign(bid, up, now, rng):
                    did.append(f"{up['how']}:{up['player_id']}")
                    await self._db(lambda con: bc.set_lineup(con, bid))
                    continue
            elif roll < 0.62:
                pick = await self._db(lambda con: bc.pick_pack(con, bid, rng))
                if pick:
                    ok, _msg, _res = await self.pm.buy_pack(user_id=bid, pack_type=pick[0], pulls=pick[1], now_ts=now,
                                                            get_balance=self.money.get_balance,
                                                            add_balance=self.money.add_balance)
                    if ok:
                        did.append(f"pack:{pick[0]}x{pick[1]}")
                        await self._db(lambda con: bc.set_lineup(con, bid))
                        continue
            elif roll < 0.75:
                deal = await self._db(lambda con: bc.find_bargain(con, bid, now, allowed))
                if deal and await self._sign(bid, deal, now, rng):
                    did.append(f"bargain:{deal['player_id']}")
                    continue
            sell = await self._db(lambda con: bc.pick_to_sell(con, bid, rng))
            if sell:
                ok, _ = await self.pm.create_listing(seller_id=bid, player_id=sell[0], qty=1, price_per=sell[1], now_ts=now)
                if ok:
                    did.append(f"list:{sell[0]}")
        await self._db(lambda con: (bc.set_lineup(con, bid), bc.schedule(con, bid, now, rng)))
        return did

    # ───────────── 트레이드: 수락 · 거절 · 역제안 ─────────────
    async def _answer_trades(self, bid: int, now: int, rng) -> list[str]:
        did, name = [], bc.bot_name(bid)
        for tid in await self._db(lambda con: bc.pending_trades(con, bid)):
            v = await self._db(lambda con: bc.trade_value(con, bid, tid))
            if v is None:
                continue
            get, give = v
            if get >= give:
                ok, _msg, proposer = await self.pm.accept_trade(trade_id=tid, receiver_id=bid, now_ts=now,
                                                                get_balance=self.money.get_balance,
                                                                add_balance=self.money.add_balance)
                if ok:
                    did.append(f"trade:{tid}:accept")
                    await self._notify(proposer, "트레이드_결과", discord.Embed(
                        title="🤝 트레이드 수락됨",
                        description=f"**{name}**: \"{bc.line(bid, 'accept', rng)}\"\n트레이드 `#{tid}` 성사!", color=0x2ecc71))
                await self._db(lambda con: bc.set_lineup(con, bid))
                continue
            prop, recv, p_cash, r_cash, proposer = await self._db(lambda con: bc.trade_items(con, tid))
            ok, _msg, _ = await self.pm.reject_trade(trade_id=tid, receiver_id=bid)
            if not ok:
                continue
            gap = give - get
            counter = None
            if get >= give * bc.COUNTER_MIN and give <= await self.money.get_balance(bid) + p_cash + gap:
                # 역제안: 같은 선수들 그대로, 모자란 만큼 상대가 돈을 더 (내가 내던 돈이면 그만큼 덜)
                want = max(0, p_cash + int(round(gap * 1.05, -3)) - r_cash)
                pay = max(0, r_cash - p_cash - int(round(gap * 1.05, -3)))
                ok2, ctid, _d = await self.pm.create_trade(
                    proposer_id=bid, receiver_id=proposer, proposer_pids=recv, receiver_pids=prop,
                    proposer_cash=pay, receiver_cash=want, now_ts=now, get_balance=self.money.get_balance)
                if ok2:
                    counter = (ctid, want, pay)
            did.append(f"trade:{tid}:" + (f"counter:{counter[0]}" if counter else "reject"))
            if counter:
                money = f"현금 **{counter[1]:,}원**을 받는 조건" if counter[1] else f"제가 **{counter[2]:,}원**만 드리는 조건"
                em = discord.Embed(title="🤝 봇 구단 역제안",
                                   description=(f"**{name}**: \"{bc.line(bid, 'counter', rng)}\"\n"
                                                f"트레이드 `#{tid}`는 거절하고, 같은 선수로 {money}을 다시 보냈어요.\n"
                                                f"`/트레이드목록`에서 `#{counter[0]}`를 수락하거나 거절할 수 있어요."),
                                   color=0xf1c40f)
            else:
                em = discord.Embed(title="🤝 트레이드 거절됨",
                                   description=f"**{name}**: \"{bc.line(bid, 'reject', rng)}\"\n트레이드 `#{tid}`를 거절했어요.",
                                   color=0xe74c3c)
            await self._notify(proposer, "트레이드_결과", em)
        return did

    # ───────────── 공식경기 ─────────────
    async def _play_officials(self, bid: int, now: int, allowed, rng) -> list[str]:
        done = await self._db(lambda con: con.execute(
            "SELECT day_key, day_n FROM club_official_week WHERE user_id=? AND week=?", (bid, week_key(now))).fetchone())
        today_n = int(done[1]) if done and int(done[0]) == kst_day(now) else 0
        n = min(rng.choice(MATCH_PLAYS), max(0, WEEKLY_DAILY - today_n))
        if n <= 0:
            return []
        me = await self.clubs.match_team(bid, auto_fill=True)
        if not me or not me.get("filled"):
            return []
        pool = []
        for uid in await self.clubs.official_opponents(bid):
            if not bc.is_bot(uid) and allowed is not None and not allowed(int(uid)):
                continue
            t = await self.clubs.match_team(uid)
            if t and t.get("filled"):
                pool.append((abs(t["rating"] - me["rating"]), uid, t))
        if not pool:
            return []
        pool.sort(key=lambda x: x[0])
        out, weekly = [], release.released_370()
        for _ in range(n):
            _gap, opp, t = rng.choice(pool[:5])
            h = {"name": me["name"], "rating": me["rating"], "xi": [s for s in me["lineup"] if s.get("player_id")]}
            a = {"name": t["name"], "rating": t["rating"], "xi": [s for s in t["lineup"] if s.get("player_id")]}
            res = simulate_match(h, a, rng)
            odds = official_odds(win_probs(h["rating"], a["rating"]))
            # 공개 전: 기록만 (주간 랭킹 · 상금은 모든 서버가 같이 보므로 공개 뒤부터)
            await self.clubs.record_official(bid, opp, res["home"], res["away"], now, 0, "W", 1.0,
                                             weekly=weekly, prize_odds=odds if weekly else None, stake=PRIZE_STAKE_370)
            out.append(f"match:{opp}:{res['home']}-{res['away']}")
        return out

    # ───────────── 영입 ─────────────
    async def _sign(self, bid: int, up: dict, now: int, rng=random) -> bool:
        name = bc.bot_name(bid)
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
                description=(f"**{name}**: \"{bc.line(bid, 'offer', rng)}\"\n"
                             f"**{pname}**을(를) **{up['price']:,}원**에 데려가고 싶대요.\n"
                             f"`/트레이드목록`에서 수락하거나 거절할 수 있어요. (`#{tid}`)"),
                color=0x3498db))
        return ok

    async def run_once(self, now: int, allowed=None, rng=None) -> dict:
        """접속할 차례인 감독들만 → {bot_id: 한 일}."""
        rng = rng or self.rng
        await self._db(lambda con: bc.bootstrap(con, now, rng))
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
            await self.clubs.fill_empty(uid)
            await self._notify(uid, "선수_뉴스", discord.Embed(
                title="🔁 선수 1명 1주인 — 선수 카드 정산",
                description=(f"이제 선수 한 명은 한 사람만 가질 수 있어요. 새 출발을 위해 가지고 있던 선수 카드 "
                             f"**{info['cards']}장**(유망주 제외)을 정리했어요.\n"
                             f"💰 지금 시세로 **{info['money']:,}원**을 넣어 드렸고, "
                             f"🎫 **얼티밋팩 교환권 {info['tickets']}장**도 드렸어요 (`/선수팩상점`에서 얼티밋팩을 공짜로).\n"
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
