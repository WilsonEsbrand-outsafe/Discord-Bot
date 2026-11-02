# cogs/bot_clubs.py — 3.7.0 🤖 봇 구단 (services/bot_clubs.py)
# 봇 감독마다 접속 시간이 달라서, 1분마다 '지금 접속할 차례인 감독'만 구단을 살펴본다 (services.bot_clubs.next_visit).
# 접속하면: 하루 일과 → 받은 트레이드(수락 · 거절 · 역제안) → 보강 · 되팔기 · 판매 (이적시장 일만 한다).
# 공개 전(release.released_370 아님)에는 테스트 서버 사람하고만 거래한다.
# 공개 때 한 번: 선수 1명 1주인 전환 (PlayerMarketDB.convert_owners_370 — 유망주 빼고 선수 전부 시세로 환산 + 얼티밋팩 교환권).
import random
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import release
from services import bot_clubs as bc
from services.club_db import ClubDB
from services.economy_db import EconomyDB
from services.notifier import send_notify
from services.player_market_db import HOLD_CAP_370, PlayerMarketDB, held_count

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
            elif roll < 0.7:
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

    # ───────────── 3.7.1 /봇구단 바로 사고팔기 ─────────────
    async def club_page(self, user_id: int, bid: int):
        def read(con):
            p = bc.PERSONALITY[bc.kind(bid)]
            lineup = {r[0] for r in con.execute("SELECT player_id FROM club_lineup WHERE user_id=?", (bid,))}
            theirs = sorted((x for x in bc.squad(con, bid) if not x["amateur"]), key=lambda x: -x["ovr"])
            for x in theirs:
                x["quote"], x["start"] = bc.quote_buy(con, bid, x["player_id"]), x["player_id"] in lineup
            listed = {r[0] for r in con.execute("SELECT player_id FROM pm_listings WHERE seller_id=? AND status='active'",
                                                (int(user_id),))}
            mine = sorted((x for x in bc.squad(con, user_id) if not x["amateur"] and x["player_id"] not in listed),
                          key=lambda x: -x["price"])
            return p["label"], bc.balance(con, bid), theirs, mine
        label, bal, theirs, mine = await self._db(read)
        e, n = bc.BOT_CLUBS[bid][0], bc.BOT_CLUBS[bid][1]
        rows = [f"{'⭐' if x['start'] else '▫️'} **{x['name']}** {x['pos']} · OVR {x['ovr']} · {x['age']}세 — {_won(x['quote'])}"
                for x in theirs[:15]]
        em = discord.Embed(title=f"{e} {n}", color=0x3498db, description=(
            f"{label} · 구단 자금 {_won(bal)}\n"
            "아래 메뉴에서 고르면 그 자리에서 바로 거래돼요. (⭐ 선발 선수는 조금 더 비싸요)\n\n"
            + ("\n".join(rows) or "지금 팔 선수가 없어요.")))
        if len(theirs) > 15:
            em.set_footer(text=f"외 {len(theirs) - 15}명 — 사기 메뉴에서 볼 수 있어요")
        return em, BotClubView(self, user_id, bid, theirs, mine)

    async def quote(self, bid: int, pid: str, buy: bool):
        def read(con):
            r = con.execute("SELECT name FROM pm_players WHERE player_id=?", (pid,)).fetchone()
            name = r[0] if r else pid
            if buy:
                q = bc.quote_buy(con, bid, pid)
                return q, ("그 선수는 이제 우리 구단에 없어요." if q is None else ""), name
            q, why = bc.quote_sell(con, bid, pid)
            return q, why, name
        return await self._db(read)

    async def deal(self, user_id: int, bid: int, pid: str, buy: bool) -> tuple[bool, str]:
        """바로 거래 — 값을 다시 매기고 트레이드를 만들어 그 자리에서 수락한다."""
        now = int(time.time())
        price, why, name = await self.quote(bid, pid, buy)
        if price is None:
            return False, f"{bc.bot_name(bid)}: {why}"
        if buy:
            if await self._db(lambda con: held_count(con, user_id)) >= HOLD_CAP_370:
                return False, f"선수는 최대 **{HOLD_CAP_370}명**까지 가질 수 있어요. 먼저 팔거나 방출해 주세요."
            if await self.money.get_balance(user_id) < price:
                return False, f"잔액이 부족해요. 필요 {_won(price)}"
            ok, tid, _d = await self.pm.create_trade(
                proposer_id=user_id, receiver_id=bid, proposer_pids=[], receiver_pids=[pid],
                proposer_cash=price, receiver_cash=0, now_ts=now, get_balance=self.money.get_balance)
        else:
            if await self.pm.get_holding(user_id, pid) <= 0:
                return False, "그 선수를 갖고 있지 않아요."
            ok, tid, _d = await self.pm.create_trade(
                proposer_id=user_id, receiver_id=bid, proposer_pids=[pid], receiver_pids=[],
                proposer_cash=0, receiver_cash=price, now_ts=now, get_balance=self.money.get_balance)
        if not ok:
            return False, str(tid)
        ok, msg, _p = await self.pm.accept_trade(trade_id=tid, receiver_id=bid, now_ts=now,
                                                 get_balance=self.money.get_balance, add_balance=self.money.add_balance)
        if not ok:
            await self.pm.cancel_trade(trade_id=tid, proposer_id=user_id)
            return False, msg

        def tidy(con):
            if not buy:
                con.execute("DELETE FROM club_lineup WHERE user_id=? AND player_id=?", (int(user_id), pid))
                try:
                    con.execute("UPDATE clubs SET captain=NULL WHERE user_id=? AND captain=?", (int(user_id), pid))
                except Exception:
                    pass
            bc.set_lineup(con, bid)
        await self._db(tidy)
        line = bc.line(bid, "accept")
        return True, (f"**{name}**을(를) {_won(price)}에 데려왔어요." if buy else
                      f"**{name}**을(를) {_won(price)}에 팔았어요.") + f"\n**{bc.bot_name(bid)}**: \"{line}\""

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


# ───────────── 3.7.1 /봇구단 — 구단을 고르고 버튼으로 바로 사고판다 ─────────────
def _won(n: int) -> str:
    return f"{int(n):,}원"


class _ConfirmView(discord.ui.View):
    def __init__(self, cog: "BotClubs", user_id: int, bid: int, pid: str, buy: bool):
        super().__init__(timeout=120)
        self.cog, self.user_id, self.bid, self.pid, self.buy = cog, user_id, bid, pid, buy

    async def interaction_check(self, it: discord.Interaction) -> bool:
        return it.user.id == self.user_id

    @discord.ui.button(label="확인", style=discord.ButtonStyle.success)
    async def ok(self, it: discord.Interaction, _b: discord.ui.Button):
        self.stop()
        await it.response.defer(ephemeral=True)
        ok, msg = await self.cog.deal(self.user_id, self.bid, self.pid, self.buy)
        await it.edit_original_response(embed=discord.Embed(
            title=("✅ 거래 완료" if ok else "❌ 거래 실패"), description=msg,
            color=0x2ecc71 if ok else 0xe74c3c), view=None)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def no(self, it: discord.Interaction, _b: discord.ui.Button):
        self.stop()
        await it.response.edit_message(content="거래를 취소했어요.", embed=None, view=None)


class BotClubView(discord.ui.View):
    """구단 고르기 · 이 구단 선수 사기 · 내 선수 팔기 (메뉴 셋)."""

    def __init__(self, cog: "BotClubs", user_id: int, bid: int, theirs: list, mine: list):
        super().__init__(timeout=300)
        self.cog, self.user_id, self.bid = cog, user_id, bid
        pick = discord.ui.Select(placeholder="🏟️ 다른 구단 보기", row=0, options=[
            discord.SelectOption(label=f"{e} {n}"[:100], value=str(b), default=(b == bid))
            for b, (e, n, *_r) in bc.BOT_CLUBS.items()])
        pick.callback = self._pick
        self.add_item(pick)
        if theirs:
            buy = discord.ui.Select(placeholder="🛒 이 구단 선수 사기", row=1, options=[
                discord.SelectOption(label=f"{x['name']} · {x['pos']} OVR {x['ovr']}"[:100], value=x["player_id"],
                                     description=f"{_won(x['quote'])}에 살 수 있어요"[:100]) for x in theirs[:25]])
            buy.callback = self._buy
            self.add_item(buy)
        if mine:
            sell = discord.ui.Select(placeholder="💰 내 선수 팔기", row=2, options=[
                discord.SelectOption(label=f"{x['name']} · {x['pos']} OVR {x['ovr']}"[:100], value=x["player_id"],
                                     description=f"시세 {_won(x['price'])}"[:100]) for x in mine[:25]])
            sell.callback = self._sell
            self.add_item(sell)

    async def interaction_check(self, it: discord.Interaction) -> bool:
        if it.user.id != self.user_id:
            await it.response.send_message("❌ `/봇구단`을 직접 열어 주세요.", ephemeral=True)
            return False
        return True

    async def _pick(self, it: discord.Interaction):
        em, view = await self.cog.club_page(self.user_id, int(it.data["values"][0]))
        await it.response.edit_message(embed=em, view=view)

    async def _ask(self, it: discord.Interaction, buy: bool):
        pid = it.data["values"][0]
        price, why, name = await self.cog.quote(self.bid, pid, buy)
        if price is None:
            return await it.response.send_message(f"❌ {bc.bot_name(self.bid)}: {why}", ephemeral=True)
        text = (f"**{name}**을(를) **{bc.bot_name(self.bid)}**에서 **{_won(price)}**에 살까요?" if buy else
                f"**{name}**을(를) **{bc.bot_name(self.bid)}**에 **{_won(price)}**에 팔까요?")
        await it.response.send_message(embed=discord.Embed(title="🤝 거래 확인", description=text, color=0x3498db),
                                        view=_ConfirmView(self.cog, self.user_id, self.bid, pid, buy), ephemeral=True)

    async def _buy(self, it: discord.Interaction):
        await self._ask(it, True)

    async def _sell(self, it: discord.Interaction):
        await self._ask(it, False)


class BotClubShop(commands.Cog):
    def __init__(self, bot: commands.Bot, clubs: "BotClubs"):
        self.bot, self.c = bot, clubs

    @app_commands.command(name="봇구단", description="🤖 봇 구단 선수를 바로 사거나, 내 선수를 바로 팔아요.")
    @app_commands.describe(구단="볼 구단 (안 고르면 첫 구단)")
    @app_commands.choices(구단=[app_commands.Choice(name=f"{e} {n}", value=str(b)) for b, (e, n, *_r) in bc.BOT_CLUBS.items()])
    async def shop(self, interaction: discord.Interaction, 구단: str = ""):
        if not release.preview_371(interaction.guild_id):
            return await interaction.response.send_message("❌ 아직 이 서버에 열리지 않았어요.", ephemeral=True)
        await interaction.response.defer()
        em, view = await self.c.club_page(interaction.user.id, int(구단) if 구단 else bc.BOT_IDS[0])
        await interaction.followup.send(embed=em, view=view)


async def setup(bot: commands.Bot):
    clubs = BotClubs(bot)
    await bot.add_cog(clubs)
    await bot.add_cog(BotClubShop(bot, clubs))
