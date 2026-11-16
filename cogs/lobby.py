# cogs/lobby.py — /로비 (3.8.2, 테스트 서버 먼저): 일주일에 한 번 열리는 축구 봇 대선. 규칙 · 수치는 services/lobby.py
# /로비 → 후보 4명과 지지율 · 공약 · 최근 뉴스 → 메뉴에서 후보를 골라 금액을 적으면 로비.
# 몇 시간마다 터지는 사건 뉴스와 선거 결과는 서버마다 하나 있는 '대선 뉴스' 스레드에 올린다.
import asyncio
import io
import random
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from matplotlib import font_manager as _fm
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

import release
from cogs.patch_notes import PATCH_THREADS
from services import lobby as L
from services import ui

SECTION = "대선"
NEWS_NAME = "대선 뉴스"
_WDAY = "월화수목금토일"
CHART_FILE = "lobby.png"
COLORS = ("#4C9AFF", "#FF6B6B", "#2ECC71", "#F5B041")   # 후보 1~4
BG, FG, SUB, GRID = "#1E1F22", "#F2F3F5", "#9A9CA3", "#2E3035"
_HAVE = {f.name for f in _fm.fontManager.ttflist}
FONT = [f for f in ("NanumGothic", "Malgun Gothic", "DejaVu Sans") if f in _HAVE] or ["DejaVu Sans"]
COUNT_GAP = 15          # 개표 장면 하나 보여 주는 시간 (초)


def kst(ts: int) -> str:
    """'11월 22일 (일) 21:00' (KST, 움직이지 않는 글자)."""
    t = time.gmtime(int(ts) + 9 * 3600)
    return f"{t.tm_mon}월 {t.tm_mday}일 ({_WDAY[t.tm_wday]}) {t.tm_hour:02d}:{t.tm_min:02d}"


def _pledges(c: dict) -> str:
    return "\n".join(f"- {L.pledge_text(k, p)}" for k, p in c["pledges"])


def support_chart(cands: list[dict], history: list, opens_ts: int, closes_ts: int, now_ts: int) -> bytes:
    """지지율 추이 그래프 PNG (선거가 열린 때부터 마감까지 가로축, 후보마다 선 하나). 블로킹 → 스레드에서."""
    fig = Figure(figsize=(9, 4.4), dpi=130)
    FigureCanvasAgg(fig)
    fig.patch.set_facecolor(BG)
    ax = fig.add_axes((0.07, 0.14, 0.72, 0.72))
    ax.set_facecolor(BG)
    pts = [(t, s) for t, s in history if opens_ts <= t <= closes_ts] or [(opens_ts, [100 / len(cands)] * len(cands))]
    if pts[-1][0] < min(now_ts, closes_ts):     # 마지막 기록부터 지금까지는 그대로 이어 그린다
        pts.append((min(now_ts, closes_ts), pts[-1][1]))
    xs = [(t - opens_ts) / 3600 for t, _ in pts]
    top = 0
    for i, c in enumerate(cands):
        ys = [s[i] for _, s in pts]
        top = max(top, max(ys))
        ax.plot(xs, ys, color=COLORS[i % 4], linewidth=2.4, solid_joinstyle="round", zorder=3)
        ax.scatter([xs[-1]], [ys[-1]], s=40, color=COLORS[i % 4], edgecolors=BG, linewidths=2, zorder=4, clip_on=False)
    span = (closes_ts - opens_ts) / 3600
    ax.set_xlim(0, span)
    ax.set_ylim(0, max(50, top + 8))
    # 가로축: 날짜가 바뀌는 자정 (KST)
    first = ((opens_ts + 9 * 3600) // 86400 + 1) * 86400 - 9 * 3600
    ticks = list(range(first, closes_ts, 86400))
    ax.set_xticks([(t - opens_ts) / 3600 for t in ticks])
    ax.set_xticklabels([_WDAY[time.gmtime(t + 9 * 3600).tm_wday] for t in ticks], fontfamily=FONT)
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.tick_params(colors=SUB, labelsize=9, length=0)
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")
    fig.text(0.03, 0.93, "지지율 추이", color=FG, fontsize=15, fontweight="bold", va="center", fontfamily=FONT)
    fig.text(0.79, 0.93, f"마감 {kst(closes_ts)}", color=SUB, fontsize=9, ha="right", va="center", fontfamily=FONT)
    last = pts[-1][1]
    order = sorted(range(len(cands)), key=lambda i: -last[i])
    for k, i in enumerate(order):        # 오른쪽 범례: 지지율 높은 순
        y = 0.80 - k * 0.16
        fig.text(0.82, y, cands[i]["name"], color=COLORS[i % 4], fontsize=13, fontweight="bold", va="center", fontfamily=FONT)
        fig.text(0.82, y - 0.06, f"{last[i]:.1f}%", color=FG, fontsize=11, va="center", fontfamily=FONT)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    return buf.getvalue()


async def chart_file(cands, history, opens_ts, closes_ts, now_ts) -> Optional[discord.File]:
    try:
        png = await asyncio.to_thread(support_chart, cands, history, opens_ts, closes_ts, now_ts)
    except Exception as ex:   # 그래프가 안 그려져도 화면은 뜨게
        print(f"[대선] 그래프 오류: {type(ex).__name__}: {ex}")
        return None
    return discord.File(io.BytesIO(png), filename=CHART_FILE)


def _bar(p: float, width: int = 12) -> str:
    n = round(p / 100 * width)
    return "█" * n + "░" * (width - n)


def count_embed(eid: int, cands: list[dict], pct: int, shares: list[float], total: int) -> discord.Embed:
    """개표 장면 한 컷: 개표율과 후보별 득표 (득표율 높은 순)."""
    counted = total * pct // 100
    lines = []
    for i in sorted(range(len(cands)), key=lambda i: -shares[i]):
        c = cands[i]
        lines.append(f"**{c['name']}** ({c['party']})\n`{_bar(shares[i])}` {shares[i]:.1f}%  {int(counted * shares[i] / 100):,}표")
    head = "투표가 끝났어요. 지금부터 개표를 시작합니다." if pct == 0 else (
        "개표가 끝났어요." if pct >= 100 else f"개표율 **{pct}%**")
    e = ui.card(f"제{eid}회 축구 봇 대선 개표 방송", head + "\n\n" + "\n\n".join(lines),
                ui.GOLD if pct >= 100 else ui.INFO, None, SECTION)
    return e


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
    em.set_image(url=f"attachment://{CHART_FILE}")
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
                now = int(time.time())
                e = await self.view.cog.db.state(self.view.user.id, now)
                f = await chart_file(e["cands"], e["history"], e["opens_ts"], e["closes_ts"], now)
                await self.view.message.edit(embed=lobby_embed(self.view.user, e), attachments=[f] if f else [])
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
        self._shows: set = set()            # 진행 중인 개표 방송

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
            print(f"[대선] 제{r['eid']}회 정산: {r['cand']['name']} 당선, 로비 {r['pool']:,}원, {r['people']}명, 적발 {len(r['caught'])}명")
            t = asyncio.create_task(self.count_show(r))   # 개표 방송은 몇 분 걸리니 따로
            self._shows.add(t)
            t.add_done_callback(self._shows.discard)
            return
        n = await self.db.news_tick(now)
        if n:
            await self.post_all(ui.card("대선 뉴스", f"**{n['text']}**\n지지율 {n['support']:.0f}%", ui.INFO, None, SECTION))

    async def count_show(self, r: dict):
        """21:00 개표 방송: 대선 뉴스 스레드마다 한 메시지를 올려 개표율을 올려 가며 고친 뒤, 결과 · 그래프 · DM."""
        try:
            total = random.randint(800_000, 1_200_000)
            frames = L.count_frames(r["shares"])
            msgs = await self.post_all(count_embed(r["eid"], r["cands"], 0, [0.0] * len(r["cands"]), total))
            for pct, sh in frames:
                await asyncio.sleep(COUNT_GAP)
                em = count_embed(r["eid"], r["cands"], pct, sh, total)
                for m in msgs:
                    try:
                        await m.edit(embed=em)
                    except discord.HTTPException:
                        pass
            await asyncio.sleep(COUNT_GAP / 2)
            png = None
            try:
                png = await asyncio.to_thread(support_chart, r["cands"], r["history"], r["opens_ts"], r["closes_ts"], r["closes_ts"])
            except Exception as ex:
                print(f"[대선] 그래프 오류: {type(ex).__name__}: {ex}")
            await self.post_all(self.result_embed(r), png)
        except Exception as ex:
            print(f"[대선] 개표 방송 오류: {type(ex).__name__}: {ex}")
        L.ACTIVE.clear()                        # 당선 공약은 결과 발표와 함께 적용
        L.ACTIVE.update(L.active_from(r["cand"]))
        await self._notify(r)

    async def _notify(self, r: dict):
        for uid, m in r["paid"].items():
            await self._dm(uid, ui.card(f"제{r['eid']}회 대선 결과", (
                f"로비한 **{r['cand']['name']}** 후보가 당선됐어요.\n받은 돈 **{m:,}원**"), ui.GOLD, None, SECTION))
        for uid in r["caught"]:
            await self._dm(uid, ui.card(f"제{r['eid']}회 대선 결과", (
                f"**{r['cand']['name']}** 후보가 당선됐지만 로비가 적발됐어요.\n넣은 돈은 돌려받지 못해요."), ui.LOSE, None, SECTION))

    @loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    @staticmethod
    def result_embed(r: dict) -> discord.Embed:
        c = r["cand"]
        e = ui.card(f"제{r['eid']}회 축구 봇 대선 결과", (
            f"**{c['name']} ({c['party']})** 후보가 득표율 {r['shares'][r['winner']]:.1f}%로 당선됐어요.\n"
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

    async def post_all(self, e: discord.Embed, png: Optional[bytes] = None) -> list:
        """서버마다 대선 뉴스 스레드에 올린다 (png 가 있으면 그래프를 embed 그림으로). 올린 메시지 목록."""
        out = []
        for guild in list(self.bot.guilds):
            if not release.preview_382(guild.id):
                continue
            try:
                th = await self.news_thread(guild)
                if th is not None:
                    if png:
                        em = e.copy()
                        em.set_image(url=f"attachment://{CHART_FILE}")
                        out.append(await th.send(embed=em, file=discord.File(io.BytesIO(png), filename=CHART_FILE)))
                    else:
                        out.append(await th.send(embed=e))
            except Exception as ex:   # 한 서버가 안 돼도 나머지는 계속
                print(f"[대선] {guild.id} 뉴스 올리기 오류: {type(ex).__name__}: {ex}")
        return out

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
        await interaction.response.defer()
        now = int(time.time())
        e = await self.db.state(interaction.user.id, now)
        view = LobbyView(self, interaction.user, e)
        f = await chart_file(e["cands"], e["history"], e["opens_ts"], e["closes_ts"], now)
        em = lobby_embed(interaction.user, e)
        if f is None:
            em.set_image(url=None)
        view.message = await interaction.followup.send(embed=em, view=view, wait=True, **({"file": f} if f else {}))


async def setup(bot: commands.Bot):
    await bot.add_cog(Lobby(bot))
