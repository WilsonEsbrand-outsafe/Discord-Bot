# cogs/lobby.py — /로비 (3.8.2, 테스트 서버 먼저): 일주일에 한 번 열리는 축구 봇 대선. 규칙 · 수치는 services/lobby.py
# /로비 → 후보 4명과 지지율 · 공약 · 최근 뉴스 → 메뉴에서 후보를 골라 금액을 적으면 로비.
# 몇 시간마다 터지는 사건 뉴스와 선거 결과는 서버마다 하나 있는 '대선 뉴스' 스레드에 올린다.
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
    """'11월 22일 (일) 21:00' (KST, 움직이지 않는 글자)."""
    t = time.gmtime(int(ts) + 9 * 3600)
    return f"{t.tm_mon}월 {t.tm_mday}일 ({_WDAY[t.tm_wday]}) {t.tm_hour:02d}:{t.tm_min:02d}"


def _pledges(c: dict) -> str:
    return "\n".join(f"- {L.pledge_text(k, p)}" for k, p in c["pledges"])


def lobby_embed(user, e: dict) -> discord.Embed:
    """한 칸에 후보 하나, 값은 한 줄에 하나 (가운뎃점으로 이어 붙이지 않는다)."""
    em = ui.card(f"제{e['id']}회 축구 봇 대선", (
        "마음에 드는 후보에게 로비하세요. 로비가 몰릴수록 지지율이 올라가요.\n"
        f"마감 **{kst(e['closes_ts'])}**"), ui.INFO, user, SECTION)
    for i, c in enumerate(e["cands"]):
        em.add_field(name=f"{i + 1}. {c['name']} ({c['party']})", value=(
            f"지지율 **{e['support'][i]:.0f}%**\n로비 {e['pots'][i]:,}원\n"
            + (f"내 로비 **{e['mine'][i]:,}원**\n" if e["mine"][i] else "")
            + f"공약\n{_pledges(c)}"))
    if e["news"]:
        em.add_field(name="최근 뉴스", value="\n".join(f"{kst(n['ts'])} {n['text']}" for n in e["news"])[:1024], inline=False)
    last = e.get("last")
    if last and last["winner"] is not None:
        w = last["cands"][last["winner"]]
        em.add_field(name=f"지금 적용 중인 공약 (제{last['id']}회 당선 {w['name']})", value=_pledges(w), inline=False)
    em.set_footer(text="당선자에게 로비한 사람은 낙선 후보들에게 들어간 돈을 넣은 금액만큼 나눠 받아요.\n"
                       "가끔 로비가 적발되면 넣은 돈을 잃어요.")
    return em


class LobbyModal(discord.ui.Modal):
    def __init__(self, view: "LobbyView", cand: int, c: dict):
        super().__init__(title=f"{c['name']} 후보에게 로비"[:45])
        self.view, self.cand = view, cand
        self.amount = discord.ui.TextInput(label="로비 금액 (원)", max_length=15, placeholder=f"{L.LOBBY_MIN:,}원 이상")
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.amount.value).replace(",", "").replace("원", "").strip()
        if not raw.isdigit():
            return await interaction.response.send_message("금액은 숫자로 적어 주세요.", ephemeral=True)
        r = await self.view.cog.db.lobby(interaction.user.id, self.cand, int(raw), int(time.time()))
        if r["ok"]:
            msg = (f"**{r['cand']['name']}** 후보에게 **{r['amount']:,}원**을 로비했어요.\n"
                   f"이 후보에게 넣은 돈 {r['mine']:,}원\n잔액 {r['balance']:,}원")
        else:
            msg = {"amount": f"로비는 {L.LOBBY_MIN:,}원부터 할 수 있어요.", "closed": "이번 선거는 마감됐어요.",
                   "balance": f"잔액이 모자라요 (잔액 {r.get('have', 0):,}원)."}.get(r["reason"], "로비할 수 없어요.")
        await interaction.response.send_message(msg, ephemeral=True)
        if r["ok"] and self.view.message is not None:
            try:
                e = await self.view.cog.db.state(self.view.user.id, int(time.time()))
                await self.view.message.edit(embed=lobby_embed(self.view.user, e))
            except discord.HTTPException:
                pass


class LobbyView(discord.ui.View):
    def __init__(self, cog: "Lobby", user, e: dict):
        super().__init__(timeout=600)
        self.cog, self.user, self.e, self.message = cog, user, e, None
        sel = discord.ui.Select(placeholder="로비할 후보 고르기", options=[
            discord.SelectOption(label=f"{i + 1}. {c['name']} ({c['party']})"[:100], value=str(i),
                                 description=L.pledge_text(*c["pledges"][0])[:100]) for i, c in enumerate(e["cands"])])
        sel.callback = self._pick
        self.add_item(sel)

    async def _pick(self, interaction: discord.Interaction):
        i = int(interaction.data["values"][0])
        await interaction.response.send_modal(LobbyModal(self, i, self.e["cands"][i]))


class Lobby(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = L.LobbyDB()
        self.threads: dict[int, int] = {}   # 서버 → 대선 뉴스 스레드

    async def cog_load(self):
        L.ACTIVE.clear()
        L.ACTIVE.update(L.active_from(await self.db.last_winner()))
        rows = await self.db._tx(lambda con: con.execute("SELECT guild_id, thread_id FROM lobby_threads").fetchall())
        self.threads = {int(g): int(t) for g, t in rows}
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    # ───────────── 몇 분마다: 테스트 서버 멤버 · 사건 뉴스 · 마감 정산 ─────────────
    @tasks.loop(seconds=60)
    async def loop(self):
        g = self.bot.get_guild(release.TEST_GUILD)
        if g is not None:
            L.TESTERS.clear()
            L.TESTERS.update(m.id for m in g.members)
        now = int(time.time())
        r = await self.db.settle(now)
        if r:
            L.ACTIVE.clear()
            L.ACTIVE.update(L.active_from(r["cand"]))
            print(f"[대선] 제{r['eid']}회 정산: {r['cand']['name']} 당선, 로비 {r['pool']:,}원, {r['people']}명, 적발 {len(r['caught'])}명")
            await self.post_all(self.result_embed(r))
            for uid, m in r["paid"].items():
                await self._dm(uid, ui.card(f"제{r['eid']}회 대선 결과", (
                    f"로비한 **{r['cand']['name']}** 후보가 당선됐어요.\n받은 돈 **{m:,}원**"), ui.GOLD, None, SECTION))
            for uid in r["caught"]:
                await self._dm(uid, ui.card(f"제{r['eid']}회 대선 결과", (
                    f"**{r['cand']['name']}** 후보가 당선됐지만 로비가 적발됐어요.\n넣은 돈은 돌려받지 못해요."), ui.LOSE, None, SECTION))
            return
        n = await self.db.news_tick(now)
        if n:
            await self.post_all(ui.card("대선 뉴스", f"**{n['text']}**\n지지율 {n['support']:.0f}%", ui.INFO, None, SECTION))

    @loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    @staticmethod
    def result_embed(r: dict) -> discord.Embed:
        c = r["cand"]
        e = ui.card(f"제{r['eid']}회 축구 봇 대선 결과", (
            f"**{c['name']} ({c['party']})** 후보가 지지율 {r['support']:.0f}%로 당선됐어요.\n"
            f"이번 선거에 들어간 로비 {r['pool']:,}원\n로비한 사람 {r['people']}명"
            + (f"\n로비가 적발된 사람 {len(r['caught'])}명" if r["caught"] else "")), ui.GOLD, None, SECTION)
        e.add_field(name="다음 선거까지 적용되는 공약", value=_pledges(c), inline=False)
        e.add_field(name="다음 선거", value=f"제{r['next']}회 대선이 열렸어요.\n마감 {kst(r['next_closes'])}", inline=False)
        return e

    async def _dm(self, uid: int, e: discord.Embed):
        try:
            u = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
            await u.send(embed=e)
        except discord.HTTPException:
            pass

    async def post_all(self, e: discord.Embed):
        for guild in list(self.bot.guilds):
            if not release.preview_382(guild.id):
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

    @app_commands.command(name="로비", description="일주일마다 열리는 축구 봇 대선 — 후보에게 로비하고, 당선되면 배당과 공약을 받아요")
    async def lobby(self, interaction: discord.Interaction):
        if not release.preview_382(interaction.guild_id):
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
