# cogs/lobby.py — /로비 (3.8.4, 테스트 서버 먼저): 일주일에 한 번 열리는 축구 봇 대선. 규칙 · 수치는 services/lobby.py
# 11/21 규완님 '/로비를 초안으로 되돌리기' → 첫 /로비 (PR #146) 화면으로: /로비 → 후보와 지지율 · 공약 · 최근 뉴스 → 메뉴로 투표 · 베팅.
# 몇 시간마다 터지는 사건 뉴스, 개표 결과, 대통령 임기 중 뉴스(탄핵)는 서버마다 하나 있는 '대선 뉴스' 스레드에 올린다.
import asyncio
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import release
from cogs.patch_notes import PATCH_THREADS
from services import lobby as L
from services import ui

SECTION = "대선"
NEWS_NAME = "대선 뉴스"
_WDAY = "월화수목금토일"


def kst(ts: int) -> str:
    """'11월 29일 (일) 21:00' (KST, 움직이지 않는 글자)."""
    t = time.gmtime(int(ts) + 9 * 3600)
    return f"{t.tm_mon}월 {t.tm_mday}일 ({_WDAY[t.tm_wday]}) {t.tm_hour:02d}:{t.tm_min:02d}"


def _pledges(c: dict) -> str:
    return "\n".join(f"- {L.pledge_text(k, p)}" for k, p in c["pledges"])


def _odds(o: float) -> str:
    return f"{o:g}배"


def history_text(e: dict) -> str:
    """집권 횟수 (한 번이라도 한 당) + 최근 대선 다섯 번."""
    wins = [f"{p} {n}번" for p, n in e["wins"].items() if n]
    rows = []
    for h in e["history"][-5:][::-1]:
        w = h["cands"][h["winner"]]
        rows.append(f"제{h['no']}회 {w['party']} {w['name']} {h['shares'][h['winner']]:.1f}%")
    return "집권 횟수\n" + "\n".join(wins) + "\n\n최근 대선\n" + "\n".join(rows)


def president_text(p: dict) -> str:
    w = p["cands"][p["winner"]]
    head = f"**{w['name']}** ({w['party']})\n제{p['no']}회 당선 득표율 {p['shares'][p['winner']]:.1f}%"
    if p["impeached"]:
        return head + "\n**탄핵됨**. 남은 임기 동안 공약이 멈췄어요."
    return head + f"\n국정 지지율 **{float(p['approval'] or L.TERM_START):.0f}%**\n지지자에게 적용 중인 공약\n{_pledges(w)}"


def lobby_embed(user, e: dict) -> discord.Embed:
    """한 칸에 후보 하나, 값은 한 줄에 하나 (가운뎃점으로 이어 붙이지 않는다)."""
    em = ui.card(f"제{e['no']}회 축구 봇 대선", (
        "지지하는 후보에게 투표하고, 누가 당선될지 베팅하세요.\n"
        f"개표 **{kst(e['closes_ts'])}**"), ui.INFO, user, SECTION)
    my = e["my_vote"]
    for i, c in enumerate(e["cands"]):
        mine = ""
        if my and my[0] == i:
            mine += f"내 표 **{my[1]}표**\n"
        bet = sum(a for cc, a, _ in e["my_bets"] if cc == i)
        if bet:
            mine += f"내 베팅 **{bet:,}원**\n"
        em.add_field(name=f"{i + 1}. {c['name']} ({c['party']})", value=(
            f"지지율 **{e['support'][i]:.0f}%**\n당선 확률 **{e['chance'][i] * 100:.0f}%**\n배당 **{_odds(e['odds'][i])}**\n"
            f"능력 {c['ability']}/10\n" + ("돌풍을 일으키는 중\n" if c.get("storm") else "")
            + f"받은 표 {e['votes'][i]:,}표\n" + mine + f"공약\n{_pledges(c)}"))
    if e["history"]:
        em.add_field(name="지난 대선", value=history_text(e)[:1024], inline=False)
    if e["president"]:
        em.add_field(name="지금 대통령", value=president_text(e["president"])[:1024], inline=False)
    if e["news"]:
        em.add_field(name="최근 뉴스", value="\n".join(f"{kst(n['ts'])} {n['text']}" for n in e["news"])[:1024], inline=False)
    em.set_footer(text=f"투표는 기본 1표, 그다음부터 100만원에 1표씩 최대 {L.VOTE_MAX}표까지. 지지 후보는 한 명이에요.\n"
                       "지지한 후보가 당선되면 그 후보의 공약 혜택을 받고, 표를 가장 많이 준 사람은 혜택이 두 배예요.\n"
                       "베팅 배당은 걸 때의 당선 확률로 정해져요. 가끔 로비가 적발되면 당첨금을 받지 못해요.")
    return em


def _digits(raw: str) -> Optional[int]:
    raw = str(raw).replace(",", "").replace("원", "").replace("표", "").strip()
    return int(raw) if raw.isdigit() else None


class VoteModal(discord.ui.Modal):
    def __init__(self, view: "LobbyView", cand: int, c: dict, had: int):
        super().__init__(title=f"{c['name']} 후보에게 투표"[:45])
        self.view, self.cand = view, cand
        hint = "첫 1표는 무료" if had == 0 else f"지금 {had}표, 1표에 100만원"
        self.n = discord.ui.TextInput(label=f"넣을 표 수 (최대 {L.VOTE_MAX - had}표)"[:45], max_length=3,
                                      default="1", placeholder=hint)
        self.add_item(self.n)

    async def on_submit(self, interaction: discord.Interaction):
        n = _digits(self.n.value)
        if not n:
            return await interaction.response.send_message("표 수는 1 이상의 숫자로 적어 주세요.", ephemeral=True)
        r = await self.view.cog.db.vote(interaction.user.id, self.cand, n, int(time.time()))
        if r["ok"]:
            msg = (f"**{r['cand']['name']}** 후보에게 **{r['n']}표**를 넣었어요.\n이 후보에게 준 표 {r['votes']}표"
                   + (f"\n쓴 돈 {r['cost']:,}원\n잔액 {r['balance']:,}원" if r["cost"] else "\n기본 1표라서 돈이 들지 않았어요."))
        else:
            msg = {"other": f"이미 **{r.get('cand', {}).get('name', '')}** 후보를 지지하고 있어요. 지지 후보는 한 명이에요.",
                   "max": f"한 사람은 {L.VOTE_MAX}표까지예요. 더 넣을 수 있는 표 {r.get('left', 0)}표",
                   "closed": "이번 대선은 투표가 끝났어요.",
                   "balance": f"돈이 모자라요.\n필요한 돈 {r.get('cost', 0):,}원\n잔액 {r.get('have', 0):,}원"}.get(
                r["reason"], "투표할 수 없어요.")
        await interaction.response.send_message(msg, ephemeral=True)
        if r["ok"]:
            await self.view.redraw()


class BetModal(discord.ui.Modal):
    def __init__(self, view: "LobbyView", cand: int, c: dict, odds: float):
        super().__init__(title=f"{c['name']} 당선에 베팅"[:45])
        self.view, self.cand = view, cand
        self.amount = discord.ui.TextInput(label=f"베팅 금액 (지금 배당 {_odds(odds)})"[:45], max_length=15,
                                           placeholder=f"{L.BET_MIN:,}원 이상")
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        a = _digits(self.amount.value)
        if a is None:
            return await interaction.response.send_message("금액은 숫자로 적어 주세요.", ephemeral=True)
        r = await self.view.cog.db.bet(interaction.user.id, self.cand, a, int(time.time()))
        if r["ok"]:
            msg = (f"**{r['cand']['name']}** 후보 당선에 **{r['amount']:,}원**을 걸었어요.\n배당 {_odds(r['odds'])}\n"
                   f"당선되면 {r['win']:,}원\n잔액 {r['balance']:,}원")
        else:
            msg = {"amount": f"베팅은 {L.BET_MIN:,}원부터 할 수 있어요.", "closed": "이번 대선은 베팅이 끝났어요.",
                   "balance": f"잔액이 모자라요 (잔액 {r.get('have', 0):,}원)."}.get(r["reason"], "베팅할 수 없어요.")
        await interaction.response.send_message(msg, ephemeral=True)
        if r["ok"]:
            await self.view.redraw()


class LobbyView(discord.ui.View):
    def __init__(self, cog: "Lobby", user, e: dict):
        super().__init__(timeout=600)
        self.cog, self.user, self.e, self.message = cog, user, e, None
        self._build()

    def _build(self):
        e = self.e
        def opts():
            return [discord.SelectOption(label=f"{i + 1}. {c['name']} ({c['party']})"[:100], value=str(i),
                                         description=f"당선 확률 {e['chance'][i] * 100:.0f}%, 배당 {_odds(e['odds'][i])}"[:100])
                    for i, c in enumerate(e["cands"])]
        vote = discord.ui.Select(placeholder="투표할 후보 고르기", options=opts(), row=0)
        vote.callback = self._vote
        bet = discord.ui.Select(placeholder="베팅할 후보 고르기", options=opts(), row=1)
        bet.callback = self._bet
        ref = discord.ui.Button(label="새로고침", style=discord.ButtonStyle.secondary, row=2)
        ref.callback = self._refresh_btn
        for x in (vote, bet, ref):
            self.add_item(x)

    async def _vote(self, interaction: discord.Interaction):
        i = int(interaction.data["values"][0])
        e = await self.cog.db.state(interaction.user.id, int(time.time()))
        had = e["my_vote"][1] if e["my_vote"] else 0
        if e["my_vote"] and e["my_vote"][0] != i:
            c = e["cands"][e["my_vote"][0]]
            return await interaction.response.send_message(
                f"이미 **{c['name']}** 후보를 지지하고 있어요. 지지 후보는 한 명이에요.", ephemeral=True)
        await interaction.response.send_modal(VoteModal(self, i, e["cands"][i], had))

    async def _bet(self, interaction: discord.Interaction):
        i = int(interaction.data["values"][0])
        e = await self.cog.db.state(interaction.user.id, int(time.time()))
        await interaction.response.send_modal(BetModal(self, i, e["cands"][i], e["odds"][i]))

    async def _refresh_btn(self, interaction: discord.Interaction):
        self.e = await self.cog.db.state(self.user.id, int(time.time()))
        self.clear_items()
        self._build()
        await interaction.response.edit_message(embed=lobby_embed(self.user, self.e), view=self)

    async def redraw(self):
        if self.message is None:
            return
        try:
            self.e = await self.cog.db.state(self.user.id, int(time.time()))
            self.clear_items()
            self._build()
            await self.message.edit(embed=lobby_embed(self.user, self.e), view=self)
        except discord.HTTPException:
            pass


class Lobby(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = L.LobbyDB()
        self.threads: dict[int, int] = {}   # 서버 → 대선 뉴스 스레드

    async def cog_load(self):
        self._apply(await self.db.president())
        rows = await self.db._tx(lambda con: con.execute("SELECT guild_id, thread_id FROM lobby_threads").fetchall())
        self.threads = {int(g): int(t) for g, t in rows}
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    @staticmethod
    def _apply(p: Optional[dict]):
        act, voters = L.active_from(p)
        L.ACTIVE.clear()
        L.ACTIVE.update(act)
        L.VOTERS.clear()
        L.VOTERS.update(voters)

    # ───────────── 1분마다: 테스트 서버 멤버 · 개표 · 선거운동 뉴스 · 임기 뉴스 ─────────────
    @tasks.loop(seconds=60)
    async def loop(self):
        try:
            await self._tick()
        except Exception as ex:   # 예외로 루프가 멈추지 않게
            print(f"[대선] 루프 오류: {type(ex).__name__}: {ex}")

    async def _tick(self):
        g = self.bot.get_guild(release.TEST_GUILD)
        if g is not None:
            L.TESTERS.clear()
            L.TESTERS.update(m.id for m in g.members)
        if not any(release.preview_lobby(x.id) for x in self.bot.guilds):
            return
        now = int(time.time())
        r = await self.db.settle(now)
        if r:
            self._apply(r)
            print(f"[대선] 제{r['no']}회 개표: {r['cand']['name']} 당선, 표 {r['votes']:,}, 베팅 {r['bets']:,}원, "
                  f"{r['people']}명, 적발 {len(r['caught'])}명")
            await self.post_all(self.result_embed(r))
            for uid, m in r["paid"].items():
                await self._dm(uid, ui.card(f"제{r['no']}회 대선 결과", (
                    f"베팅한 **{r['cand']['name']}** 후보가 당선됐어요.\n받은 돈 **{m:,}원**"), ui.GOLD, None, SECTION))
            for uid in r["caught"]:
                await self._dm(uid, ui.card(f"제{r['no']}회 대선 결과", (
                    f"**{r['cand']['name']}** 후보가 당선됐지만 로비가 적발됐어요.\n당첨금을 받지 못해요."), ui.LOSE, None, SECTION))
            if r["top"] is not None:
                await self._dm(r["top"], ui.card(f"제{r['no']}회 대선 결과", (
                    f"지지한 **{r['cand']['name']}** 후보가 당선됐어요.\n"
                    "당선자에게 표를 가장 많이 준 사람이라서 다음 대선까지 공약 혜택을 두 배로 받아요."), ui.GOLD, None, SECTION))
            return
        n = await self.db.news_tick(now)
        if n:
            await self.post_all(ui.card("대선 뉴스", f"**{n['text']}**\n지지율 {n['support']:.0f}%", ui.INFO, None, SECTION))
        t = await self.db.term_tick(now)
        if t:
            if t["impeached"]:
                self._apply(None)
                await self.post_all(ui.card("속보", f"**{t['impeach_text']}**", ui.LOSE, None, SECTION))
            else:
                await self.post_all(ui.card("국정 뉴스", f"**{t['text']}**", ui.WIN if t["good"] else ui.LOSE, None, SECTION))

    @loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    @staticmethod
    def result_embed(r: dict) -> discord.Embed:
        c = r["cand"]
        e = ui.card(f"제{r['no']}회 축구 봇 대선 결과", (
            f"**{c['name']} ({c['party']})** 후보가 득표율 {r['shares'][r['winner']]:.1f}%로 당선됐어요.\n"
            f"플레이어 표 {r['votes']:,}표\n베팅 {r['bets']:,}원\n참여한 사람 {r['people']}명"
            + (f"\n로비가 적발된 사람 {len(r['caught'])}명" if r["caught"] else "")), ui.GOLD, None, SECTION)
        order = sorted(range(len(r["cands"])), key=lambda i: -r["shares"][i])
        e.add_field(name="득표율", value="\n".join(
            f"{r['cands'][i]['name']} ({r['cands'][i]['party']}) {r['shares'][i]:.1f}%" for i in order), inline=False)
        e.add_field(name="다음 대선까지 지지자에게 적용되는 공약", value=_pledges(c), inline=False)
        e.add_field(name="다음 대선", value=f"제{r['next']}회 대선이 열렸어요.\n개표 {kst(r['next_closes'])}", inline=False)
        return e

    async def _dm(self, uid: int, e: discord.Embed):
        try:
            u = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
            await u.send(embed=e)
        except discord.HTTPException:
            pass

    async def post_all(self, e: discord.Embed):
        for guild in list(self.bot.guilds):
            if not release.preview_lobby(guild.id):
                continue
            try:
                th = await self.news_thread(guild)
                if th is not None:
                    await th.send(embed=e)
            except Exception as ex:   # 한 서버가 안 돼도 나머지는 계속
                print(f"[대선] {guild.id} 뉴스 올리기 오류: {type(ex).__name__}: {ex}")

    async def _parents(self, guild) -> list:
        """뉴스 스레드를 만들 채널: 패치노트가 가는 채널 (패치노트가 스레드로 가는 서버는 그 스레드의 채널)."""
        out = []
        tid = PATCH_THREADS.get(guild.id)
        if tid:
            th = guild.get_channel_or_thread(tid)
            out.append(getattr(th, "parent", None))
        notes = self.bot.get_cog("PatchNotesCog")
        if notes is not None:
            out.append(await asyncio.to_thread(notes._pick_channel, guild))
        return [c for n, c in enumerate(out) if isinstance(c, discord.TextChannel) and c not in out[:n]]

    async def news_thread(self, guild) -> Optional[discord.Thread]:
        """이 서버의 대선 뉴스 스레드 (없으면 만들고, 보관됐으면 다시 연다)."""
        tid = self.threads.get(guild.id)
        if tid:
            th = guild.get_thread(tid)
            if th is None:
                try:
                    th = await guild.fetch_channel(tid)
                except discord.NotFound:
                    th = None
                except discord.HTTPException:
                    return None
            if isinstance(th, discord.Thread):
                if th.archived:
                    await th.edit(archived=False)
                return th
        for parent in await self._parents(guild):
            perms = parent.permissions_for(guild.me)
            if not (perms.view_channel and perms.create_public_threads and perms.send_messages_in_threads):
                continue
            try:
                if parent.is_news():
                    first = await parent.send(f"**{NEWS_NAME}**가 열렸어요. 일주일마다 열리는 축구 봇 대선 소식은 이 글의 스레드에 올라와요.")
                    th = await first.create_thread(name=NEWS_NAME, auto_archive_duration=10080, reason=NEWS_NAME)
                else:
                    th = await parent.create_thread(name=NEWS_NAME, type=discord.ChannelType.public_thread,
                                                    auto_archive_duration=10080, reason=NEWS_NAME)
            except discord.HTTPException as ex:
                print(f"[대선] {guild.id} 뉴스 스레드 만들기 오류: {type(ex).__name__}: {ex}")
                continue
            await self.db._tx(lambda con: con.execute(
                "INSERT OR REPLACE INTO lobby_threads(guild_id, thread_id) VALUES(?,?)", (guild.id, th.id)))
            self.threads[guild.id] = th.id
            print(f"[대선] {guild.id} 뉴스 스레드 {th.id} 만듦 (#{parent.name})")
            return th
        return None

    @app_commands.command(name="로비", description="일주일마다 열리는 축구 봇 대선. 지지 후보에게 투표하고, 누가 당선될지 베팅해요")
    async def lobby(self, interaction: discord.Interaction):
        if not release.preview_lobby(interaction.guild_id):
            return await interaction.response.send_message("/로비 는 아직 이 서버에서 쓸 수 없어요.", ephemeral=True)
        e = await self.db.state(interaction.user.id, int(time.time()))
        view = LobbyView(self, interaction.user, e)
        await interaction.response.send_message(embed=lobby_embed(interaction.user, e), view=view)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Lobby(bot))
