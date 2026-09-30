# cogs/players_market.py
from __future__ import annotations

import io
import time
import asyncio
import discord
from discord import app_commands
from discord.ext import commands, tasks

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# 한글 폰트 설정 (Ubuntu)
matplotlib.rcParams["font.family"] = "NanumGothic"
matplotlib.rcParams["axes.unicode_minus"] = False  # 마이너스 기호 깨짐 방지

from services import ui
from services.economy_db import ITEMS, SHOP_PRICES, EconomyDB
from services.player_market_db import PlayerMarketDB, PACKS, PACK_MAX_PULLS, JACKPOT_PROB, JACKPOT_RANGE, player_profile

PACK_EMOJI = {
    "브론즈": "🥉", "실버": "🥈", "골드": "🥇",
    "플래티넘": "💎", "다이아몬드": "🔷", "아이콘": "👑", "얼티밋": "🌟",
    "공격수": "⚽", "미드필더": "🎯", "수비수": "🛡️", "골키퍼": "🧤",
}
from services.notifier import send_notify
from auth import OWNER_ID

_PRICE_LABELS = ["🔴 대박", "🟠 이득", "🟡 본전", "🟢 손해", "⚪ 폭망"]

def _price_label(player_price: int, pack_price: int) -> str:
    """현재가 / 팩 단가 비율로 결과 등급 라벨 반환."""
    if pack_price <= 0:
        return "⚪ 폭망"
    ratio = player_price / pack_price
    if ratio >= 2.5:  return "🔴 대박"   # 잭팟 하한과 동일
    if ratio >= 1.15: return "🟠 이득"
    if ratio >= 0.80: return "🟡 본전"
    if ratio >= 0.50: return "🟢 손해"
    return "⚪ 폭망"

def _embed(title: str, desc: str, user: discord.abc.User) -> discord.Embed:
    e = discord.Embed(title=title, description=desc, color=0x2ecc71)
    e.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    return e

# ───────────────── 시세 그래프 ─────────────────
# 국내 증권 앱처럼 상승 빨강 · 하락 파랑. 배경은 디스코드 임베드 색에 맞춘다.
CHART_UP, CHART_DOWN = 0xF04452, 0x3182F6
_BG, _FG, _SUB, _GRID = "#2B2D31", "#F2F3F5", "#B5BAC1", "#3A3C42"


def _won_short(v: float) -> str:
    """축 눈금용: 1.2억 / 350만 / 9,000."""
    if abs(v) >= 1e8:
        return f"{v / 1e8:.1f}억".replace(".0억", "억")
    if abs(v) >= 1e4:
        return f"{v / 1e4:,.0f}만"
    return f"{v:,.0f}"


def price_chart_png(name: str, sub: str, ts: list[int], ys: list[int], base: int, hours: int) -> bytes:
    """가격 기록(ts=유닉스초, ys=가격)을 다크 테마 PNG 로. 스레드에서 호출한다(블로킹)."""
    import datetime as dt
    import matplotlib.dates as mdates
    from matplotlib.ticker import FuncFormatter, MaxNLocator

    kst = dt.timezone(dt.timedelta(hours=9))
    xs = [dt.datetime.fromtimestamp(t, kst) for t in ts]
    up = ys[-1] >= ys[0]
    line = f"#{CHART_UP if up else CHART_DOWN:06X}"

    fig, ax = plt.subplots(figsize=(9, 4.6), dpi=150)
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)
    fig.subplots_adjust(left=0.09, right=0.87, top=0.80, bottom=0.12)

    lo, hi = min(ys), max(ys)
    pad = (hi - lo) * 0.18 or max(1, hi * 0.02)
    ax.set_ylim(lo - pad, hi + pad)
    ax.set_xlim(xs[0], xs[-1])
    ax.plot(xs, ys, color=line, linewidth=2.2, solid_joinstyle="round", zorder=3)
    ax.fill_between(xs, ys, lo - pad, color=line, alpha=0.13, linewidth=0, zorder=2)
    if lo - pad < base < hi + pad:   # 기준가가 화면 안에 있을 때만 점선
        ax.axhline(base, color=_SUB, linestyle=(0, (3, 4)), linewidth=1, alpha=0.55, zorder=1)

    # 최고 · 최저 (마지막 점과 겹치면 생략) + 현재가 말풍선
    last = len(ys) - 1
    for i, off, va in ((ys.index(hi), 7, "bottom"), (ys.index(lo), -7, "top")):
        if i != last and hi != lo:
            ha = "left" if i < last * 0.08 else "right" if i > last * 0.92 else "center"   # 가장자리면 안쪽으로
            ax.annotate(f"{ys[i]:,}", (xs[i], ys[i]), xytext=(0, off), textcoords="offset points",
                        ha=ha, va=va, color=_SUB, fontsize=8)
    ax.scatter([xs[-1]], [ys[-1]], s=46, color=line, edgecolors=_BG, linewidths=2, zorder=4, clip_on=False)
    ax.annotate(f"{ys[-1]:,}원", (xs[-1], ys[-1]), xytext=(10, 0), textcoords="offset points",
                va="center", color="white", fontsize=9, fontweight="bold", annotation_clip=False,
                bbox={"boxstyle": "round,pad=0.35", "fc": line, "ec": "none"})

    for s in ax.spines.values():
        s.set_visible(False)
    ax.grid(axis="y", color=_GRID, linewidth=0.8)
    ax.tick_params(colors=_SUB, labelsize=8, length=0)
    ax.yaxis.set_major_locator(MaxNLocator(5))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: _won_short(v)))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=7, tz=kst))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M" if hours <= 24 else "%m/%d %H시", tz=kst))

    d = ys[-1] - ys[0]
    fig.text(0.03, 0.93, name, color=_FG, fontsize=15, fontweight="bold", va="center")
    fig.text(0.03, 0.855, sub, color=_SUB, fontsize=9, va="center")
    fig.text(0.97, 0.93, f"{ys[-1]:,}원", color=_FG, fontsize=15, fontweight="bold", ha="right", va="center")
    fig.text(0.97, 0.855, f"{'▲' if d > 0 else '▼' if d < 0 else '―'} {abs(d):,}원 ({d / ys[0] * 100 if ys[0] else 0:+.2f}%)",
             color=line, fontsize=10, fontweight="bold", ha="right", va="center")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=_BG)
    plt.close(fig)
    return buf.getvalue()


# 최고 등급에 맞춘 embed 색 — 개봉 연출에서 분위기를 잡아준다
_LABEL_COLOR = {
    "🔴 대박": 0xe74c3c, "🟠 이득": 0xe67e22, "🟡 본전": 0xf1c40f,
    "🟢 손해": 0x2ecc71, "⚪ 폭망": 0x95a5a6,
}


def _card_line(row, pack_price_per: int, is_jackpot: bool = False) -> tuple[str, str]:
    """뽑은 선수 1명을 (라벨, 표시줄)로 만든다."""
    pid, cur_price, name, nation, pos, ovr = row
    label = _price_label(cur_price, pack_price_per)
    mark = "💥 **JACKPOT** " if is_jackpot else ""
    return label, f"• {mark}{label} `#{pid}` {name} ({nation}) {pos} / OVR {ovr} / **{cur_price:,}원**"


def _normalize_results(results: list) -> list:
    """[(row, is_jackpot)] 또는 [row] 를 [(row, is_jackpot)] 로 맞춘다."""
    out = []
    for item in results:
        if len(item) == 2 and isinstance(item[0], (list, tuple)):
            out.append((item[0], bool(item[1])))
        else:
            out.append((item, False))
    return out


def _format_pack_results(
    results: list, pack_price_per: int
) -> tuple[str, str, int]:
    """팩 뽑기 결과를 포맷팅 (/상점 개봉 결과).

    Returns:
        (grade_summary, lines_text, total_value)
    """
    label_cnt = {k: 0 for k in _PRICE_LABELS}
    lines = []
    total_value = 0
    for row, is_jackpot in _normalize_results(results):
        label, line = _card_line(row, pack_price_per, is_jackpot)
        label_cnt[label] += 1
        total_value += row[1]
        lines.append(line)
    grade_summary = " / ".join(f"{k} {v}" for k, v in label_cnt.items() if v > 0)
    return grade_summary, "\n".join(lines[:10]), total_value

class _SkipView(discord.ui.View):
    """팩 개봉 연출 중 '⏩ 스킵' — 누르면 남은 카드를 한 번에 공개한다."""
    def __init__(self, owner_id: int):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.pressed = asyncio.Event()

    @discord.ui.button(label="⏩ 스킵", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("팩을 연 사람만 스킵할 수 있습니다.", ephemeral=True)
        self.pressed.set()
        await interaction.response.defer()


class _ShopOwnerView(discord.ui.View):
    """상점을 연 사람만 누를 수 있다."""

    def __init__(self, cog: "PlayersMarket", user):
        super().__init__(timeout=300)
        self.cog, self.user = cog, user

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("🙅 상점을 연 사람만 살 수 있어요. `/상점`을 직접 열어 주세요.", ephemeral=True)
            return False
        return True


class ShopView(_ShopOwnerView):
    """/상점 — 선수팩과 아이템을 한 메뉴에서 산다. 선수팩을 고르면 몇 장 살지 묻는다."""

    def __init__(self, cog: "PlayersMarket", user):
        super().__init__(cog, user)
        opts = [discord.SelectOption(label=f"{k}팩 · {p['price']:,}원 / 장", value=f"pack:{k}",
                                     emoji=PACK_EMOJI.get(k, "🎁"), description=f"선수 카드 1~{PACK_MAX_PULLS}장")
                for k, p in PACKS.items()]
        opts += [discord.SelectOption(label=f"{ITEMS[k][1]} · {price:,}원", value=f"item:{k}", emoji=ITEMS[k][0],
                                      description=ITEMS[k][2][:100])
                 for k, price in SHOP_PRICES.items()]
        self.menu = discord.ui.Select(placeholder="🛒 살 상품을 고르세요", options=opts[:25])
        self.menu.callback = self._buy
        self.add_item(self.menu)

    async def _buy(self, interaction: discord.Interaction):
        kind, key = self.menu.values[0].split(":", 1)
        if kind == "pack":
            return await interaction.response.edit_message(view=PackQtyView(self.cog, self.user, key))
        emoji, name, desc = ITEMS[key]
        r = await self.cog.money.buy_item(self.user.id, key)
        if r["ok"]:
            e = ui.card(f"🛒 {emoji} {name} 구매!", f"{desc}\n\n`가격` **-{r['price']:,}원** · `잔액` **{r['balance']:,}원**\n"
                        f"`보유` **{r['qty']}개** · `/가방`에서 바로 사용", ui.WIN, self.user, "🛒 상점")
        else:
            e = ui.card("🙅 잔액이 부족해요", f"`가격` **{SHOP_PRICES[key]:,}원**\n`잔액` **{r['balance']:,}원**",
                        ui.LOSE, self.user, "🛒 상점")
        await interaction.response.send_message(embed=e)


class PackQtyView(_ShopOwnerView):
    """상점에서 선수팩을 고른 뒤: 몇 장 살지 (1~10장, 총액 표시) · 돌아가기."""

    def __init__(self, cog: "PlayersMarket", user, pack: str):
        super().__init__(cog, user)
        self.pack, price = pack, PACKS[pack]["price"]
        self.qty = discord.ui.Select(
            placeholder=f"{PACK_EMOJI.get(pack, '🎁')} {pack}팩 — 몇 장 살까요?",
            options=[discord.SelectOption(label=f"{n}장 · {price * n:,}원", value=str(n))
                     for n in range(1, PACK_MAX_PULLS + 1)])
        self.qty.callback = self._open
        self.add_item(self.qty)

    async def _open(self, interaction: discord.Interaction):
        # 상점 메뉴를 처음 상태로 돌려놓고, 개봉 연출은 새 메시지로
        await interaction.response.edit_message(view=ShopView(self.cog, self.user))
        await self.cog._buy_pack(interaction, self.pack, int(self.qty.values[0]))

    @discord.ui.button(label="돌아가기", emoji="↩️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(view=ShopView(self.cog, self.user))


# ───────────────── 즉시판매 UI ─────────────────
_SORT_LABELS = [
    ("💰 가격↓", "price", True),
    ("📊 OVR↓",  "ovr",   True),
    ("🏃 포지션", "pos",   False),
]

class QuickSellView(discord.ui.View):
    """보유 선수 즉시판매 인터랙티브 UI — 정렬·페이지·복수선택·전체판매 지원"""
    PAGE_SIZE = 25

    def __init__(self, holdings: list, pm, money, user: discord.abc.User, now_ts: int):
        super().__init__(timeout=180)
        # 아마추어·은퇴 제외
        self.holdings = [
            h for h in holdings
            if not str(h[0]).startswith("AMT_") and int(h[7]) == 0
        ]
        self.pm = pm
        self.money = money
        self.user = user
        self.now_ts = now_ts
        self.sort_key = "price"
        self.sort_desc = True
        self.page = 0
        self._rebuild()

    # ── 정렬·페이지 헬퍼 ──
    def _sorted(self):
        idx = {"price": 9, "ovr": 5, "pos": 3}[self.sort_key]
        return sorted(self.holdings, key=lambda x: x[idx], reverse=self.sort_desc)

    @property
    def _total_pages(self):
        return max(1, (len(self.holdings) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    def _page_items(self):
        s = self._sorted()
        return s[self.page * self.PAGE_SIZE:(self.page + 1) * self.PAGE_SIZE]

    # ── UI 재구성 ──
    def _rebuild(self):
        self.clear_items()
        items = self._page_items()
        if not items:
            return

        # Row 0: 선수 다중 선택 드롭다운
        options = [
            discord.SelectOption(
                label=f"{name} x{qty}"[:25],
                description=f"{pos} OVR{ovr} | {int(price):,}→{int(price)//2:,}원"[:50],
                value=pid,
            )
            for pid, name, nation, pos, age, ovr, potg, retired, qty, price in items
        ]
        sel = discord.ui.Select(
            placeholder="판매할 선수 선택 (복수 선택 가능)",
            min_values=1, max_values=len(options),
            options=options, row=0,
        )
        sel.callback = self._on_select
        self.add_item(sel)

        # Row 1: 정렬 버튼
        for label, key, desc in _SORT_LABELS:
            active = (self.sort_key == key)
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary,
                row=1,
            )
            btn.callback = self._make_sort_cb(key, desc)
            self.add_item(btn)

        # Row 2: 페이지 이동 + 전체 판매
        if self._total_pages > 1:
            prev = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary,
                                     row=2, disabled=self.page == 0)
            prev.callback = self._prev
            self.add_item(prev)

            next_ = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary,
                                      row=2, disabled=self.page >= self._total_pages - 1)
            next_.callback = self._next
            self.add_item(next_)

        all_btn = discord.ui.Button(
            label=f"🗑️ 전체 판매 ({len(self.holdings)}명)",
            style=discord.ButtonStyle.danger, row=2,
        )
        all_btn.callback = self._sell_all
        self.add_item(all_btn)

    def make_embed(self) -> discord.Embed:
        items = self._page_items()
        total_receive = sum(int(h[9]) for h in self.holdings) // 2
        lines = [
            f"`{name}` x{qty} | {pos} OVR{ovr} | {int(price):,}원 → **{int(price)//2:,}원**"
            for pid, name, nation, pos, age, ovr, potg, retired, qty, price in items
        ]
        desc = (
            f"보유 **{len(self.holdings)}명** | 전체 즉판 예상: **{total_receive:,}원**\n"
            f"페이지 {self.page+1}/{self._total_pages}\n\n"
            + "\n".join(lines)
        )
        return discord.Embed(title="💸 즉시판매", description=desc, color=0xe74c3c)

    # ── 콜백 팩토리 ──
    def _make_sort_cb(self, key, desc):
        async def cb(interaction: discord.Interaction):
            if interaction.user.id != self.user.id:
                return await interaction.response.send_message("본인만 사용할 수 있습니다.", ephemeral=True)
            self.sort_key = key; self.sort_desc = desc; self.page = 0
            self._rebuild()
            await interaction.response.edit_message(embed=self.make_embed(), view=self)
        return cb

    async def _prev(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("본인만 사용할 수 있습니다.", ephemeral=True)
        self.page -= 1; self._rebuild()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    async def _next(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("본인만 사용할 수 있습니다.", ephemeral=True)
        self.page += 1; self._rebuild()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    # ── 판매 처리 ──
    async def _do_sell(self, interaction: discord.Interaction, pids: list[str]):
        total_payout, sold, failed = 0, 0, 0
        detail_lines = []  # 소량(≤10)일 때만 개별 표시

        for pid in pids:
            matching = next((h for h in self.holdings if h[0] == pid), None)
            qty = int(matching[8]) if matching else 1
            ok, msg, payout = await self.pm.direct_instant_sell(
                user_id=self.user.id, player_id=pid, qty=qty,
                now_ts=self.now_ts, add_balance=self.money.add_balance,
            )
            if ok:
                total_payout += payout
                sold += 1
                if len(pids) <= 10:
                    detail_lines.append(msg)
            else:
                failed += 1
                if len(pids) <= 10:
                    detail_lines.append(f"❌ {msg}")
            self.holdings = [h for h in self.holdings if h[0] != pid]

        bal = await self.money.get_balance(self.user.id)

        # 결과 메시지 — 대량이면 요약, 소량이면 상세
        if detail_lines:
            body = "\n".join(detail_lines)
        else:
            body = f"✅ **{sold}명** 판매 완료" + (f"  |  ❌ 실패 {failed}건" if failed else "")
        body += f"\n\n💰 총 실수령: **{total_payout:,}원** | 잔액: **{bal:,}원**"

        # 2000자 초과 방지
        if len(body) > 1900:
            body = f"✅ **{sold}명** 판매 완료\n💰 총 실수령: **{total_payout:,}원** | 잔액: **{bal:,}원**"

        await interaction.followup.send(body)

        if not self.holdings:
            self.clear_items()
            await interaction.edit_original_response(
                embed=discord.Embed(title="💸 즉시판매", description="판매할 선수가 없습니다.", color=0x95a5a6),
                view=self,
            )
        else:
            self.page = min(self.page, self._total_pages - 1)
            self._rebuild()
            await interaction.edit_original_response(embed=self.make_embed(), view=self)

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("본인만 사용할 수 있습니다.", ephemeral=True)
        await interaction.response.defer()
        await self._do_sell(interaction, interaction.data["values"])

    async def _sell_all(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("본인만 사용할 수 있습니다.", ephemeral=True)
        await interaction.response.defer()
        await self._do_sell(interaction, [h[0] for h in list(self.holdings)])


class PlayersMarket(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.money = EconomyDB()
        self.pm = PlayerMarketDB()
        self.expire_task.start()
        self.prune_task.start()

    def cog_unload(self):
        self.expire_task.cancel()
        self.prune_task.cancel()

    @tasks.loop(hours=6)
    async def prune_task(self):
        """30일보다 오래된 시세 기록 정리 (6시간마다, 조금씩 나눠서)"""
        try:
            n = await self.pm.prune_price_history(int(time.time()))
            if n:
                print(f"[PM] 오래된 시세 기록 정리: {n:,}행")
        except Exception as e:
            print(f"[PM] prune_task 오류: {e!r}")

    @prune_task.before_loop
    async def before_prune_task(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(180)   # 부팅 직후 루프들과 겹치지 않게

    async def cog_load(self):
        await self.pm.ensure_bootstrap(int(time.time()))

    @tasks.loop(minutes=10)
    async def expire_task(self):
        """만료된 이적시장 매물 자동 반환 (10분마다)"""
        try:
            expired = await self.pm.expire_listings(int(time.time()))
            if expired:
                print(f"[이적시장] 만료 처리: {len(expired)}건")
                for info in expired:
                    dm_embed = discord.Embed(
                        title="⏰ 이적시장 매물 만료",
                        description=(
                            f"**{info['name']}** x{info['qty']}장\n"
                            f"매물이 만료되어 보유 목록으로 돌아왔습니다."
                        ),
                        color=0xe67e22,
                    )
                    await send_notify(self.bot, self.money, info["seller_id"], "매물_만료", dm_embed)
        except Exception as e:
            print(f"[이적시장] expire_task 오류: {e}")

    @expire_task.before_loop
    async def before_expire_task(self):
        await self.bot.wait_until_ready()

    # ───────────────── 자동완성 ─────────────────
    async def player_id_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """선수 정보·시세 조회용 — 은퇴 선수도 포함"""
        try:
            rows = await self.pm.search_players(current, limit=10)
            choices = []
            for pid, name, nation, pos, age, ovr, potg, price, retired in rows:
                tag = " (은퇴)" if int(retired) == 1 else ""
                label = f"{name}{tag} | {pos} OVR{ovr} | {int(price):,}원"
                choices.append(app_commands.Choice(name=label[:100], value=pid))
            return choices
        except Exception:
            return []

    async def active_player_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """구매용 — 활동 중인 선수만 표시"""
        try:
            rows = await self.pm.search_players(current, limit=15)
            choices = []
            for pid, name, nation, pos, age, ovr, potg, price, retired in rows:
                if int(retired) == 1:
                    continue
                label = f"{name} | {pos} OVR{ovr} | {int(price):,}원"
                choices.append(app_commands.Choice(name=label[:100], value=pid))
            return choices[:10]
        except Exception:
            return []

    async def holding_player_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """판매용 — 내가 보유한 활동 선수 전체에서 검색"""
        try:
            rows = await self.pm.list_holdings(interaction.user.id, limit=9999)
            q = current.lower()
            choices = []
            for pid, name, nation, pos, age, ovr, potg, retired, qty, price in rows:
                if int(retired) == 1:
                    continue
                label = f"{name} x{qty} | {pos} OVR{ovr} | {int(price):,}원"
                if q and q not in name.lower() and q not in pid.lower() and q not in pos.lower():
                    continue
                choices.append(app_commands.Choice(name=label[:100], value=pid))
            return choices[:25]
        except Exception:
            return []


    async def retired_holding_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """방출용 — 내가 보유한 은퇴 선수 전체에서 검색"""
        try:
            rows = await self.pm.list_holdings(interaction.user.id, limit=9999)
            q = current.lower()
            choices = []
            for pid, name, nation, pos, age, ovr, potg, retired, qty, price in rows:
                if int(retired) != 1:
                    continue
                label = f"(은퇴) {name} x{qty} | {pos} OVR{ovr}"
                if q and q not in name.lower() and q not in pid.lower():
                    continue
                choices.append(app_commands.Choice(name=label[:100], value=pid))
            return choices[:25]
        except Exception:
            return []

    async def my_listing_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """내 매물 자동완성 (즉시판매·취소용)"""
        try:
            rows = await self.pm.get_my_listings(interaction.user.id)
            now = int(time.time())
            choices = []
            for lid, pid, qty, price_per, listed_at, expires_at, instant_sell_at, name, nation, pos, age, ovr, potg, base_value in rows:
                can_instant = now >= int(instant_sell_at)
                tag = "✅즉시가능" if can_instant else "⏳대기중"
                label = f"#{lid} {name} x{qty} | {int(price_per):,}원 | {tag}"
                if current and current not in label and current not in str(lid):
                    continue
                choices.append(app_commands.Choice(name=label[:100], value=str(lid)))
            return choices[:10]
        except Exception:
            return []

    async def listing_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """이적시장 매물 자동완성 (구매용)"""
        try:
            rows = await self.pm.get_listings(current, limit=10)
            choices = []
            for lid, seller_id, pid, qty, price_per, listed_at, expires_at, instant_sell_at, name, nation, pos, age, ovr, potg, base_value in rows:
                label = f"#{lid} {name} x{qty} | {pos} OVR{ovr}/{potg} | {int(price_per):,}원"
                choices.append(app_commands.Choice(name=label[:100], value=str(lid)))
            return choices[:10]
        except Exception:
            return []

    # ───────────────── 시장 상태 ─────────────────
    @app_commands.command(name="시장", description="시장 오픈/클로즈 상태를 확인합니다.")
    async def market_status(self, interaction: discord.Interaction):
        # ✅ 3초 제한 회피: 먼저 defer
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return

        try:
            now = int(time.time())
            st = await self.pm.market_status(now)

            status_emoji = "🟢" if st.is_open else "🔴"
            msg = (
                f"{status_emoji} **{st.reason}**\n"
                f"운영 시간: 매일 **09:00 ~ 23:00 KST**\n"
                f"다음 변경: <t:{st.next_change_ts}:f>"
            )
            await interaction.followup.send(
                embed=_embed("📈 선수 시장", msg, interaction.user),
            )
        except Exception as e:
            await interaction.followup.send(f"❌ 오류: {type(e).__name__}")

    # ───────────────── 선수 (검색 + 상세, 하나로 통일) ─────────────────
    async def _player_detail_embed(self, row, viewer: discord.abc.User) -> discord.Embed:
        (pid, name, nation, pos, age, ovr, potg, basev, retired, price, floor_p, ceil_p, last_ts) = row
        have = await self.pm.get_holding(viewer.id, pid)
        pot = await self.pm.player_pot(pid) or int(ovr)
        prof = player_profile(pid, pos)
        state = "💤 은퇴" if int(retired) == 1 else "🟢 활동"
        grow = f" (+{pot - int(ovr)})" if pot > int(ovr) else " (완성형)"
        desc = (
            f"`#{pid}` · {nation} · **{pos}** · {age}세 · {state}\n"
            f"`체격` {prof['height']}cm · {prof['weight']}kg · `주발` {prof['foot']}\n\n"
            f"`능력` OVR **{ovr}** → 잠재 **{pot}** ({potg}){grow}\n"
            f"`시세` **{int(price):,}원** (기준가 {int(basev):,}원)\n"
            f"`범위` {int(floor_p):,} ~ {int(ceil_p):,}원\n"
            f"`보유` {viewer.display_name}님 **{have}장**"
        )
        if int(retired) == 1:
            desc += "\n\n⚠️ 은퇴 선수는 `/방출`로 기준가의 30%에 정리할 수 있습니다."
        e = _embed(f"📌 {name}", desc, viewer)
        e.set_footer(text=f"/시세 {name} 로 가격 그래프를 볼 수 있습니다")
        return e

    @app_commands.command(name="선수", description="선수를 검색하거나 상세 정보를 봅니다. (이름·국적·포지션·#번호 / 비우면 고가 TOP 10)")
    @app_commands.describe(검색어="선수 이름·국적·포지션·#번호 — 자동완성에서 고르면 바로 상세 정보")
    @app_commands.autocomplete(검색어=player_id_autocomplete)
    async def player(self, interaction: discord.Interaction, 검색어: str = ""):
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return
        q = 검색어.strip()

        # 번호·정확한 이름이면 바로 상세
        if q:
            row = await self.pm.get_player(q)
            if row:
                return await interaction.followup.send(embed=await self._player_detail_embed(row, interaction.user))

        rows = await self.pm.search_players(q, limit=10)
        if not rows:
            return await interaction.followup.send(
                embed=_embed("🔎 선수", f"**{q}**에 해당하는 선수가 없습니다.", interaction.user))
        if len(rows) == 1:
            row = await self.pm.get_player(rows[0][0])
            return await interaction.followup.send(embed=await self._player_detail_embed(row, interaction.user))

        lines = []
        for pid, name, nation, pos, age, ovr, potg, price, retired in rows:
            tag = " (은퇴)" if int(retired) == 1 else ""
            lines.append(f"`#{pid}` **{name}**{tag} · {nation} · {pos} · {age}세 · OVR {ovr}/{potg} · **{int(price):,}원**")
        title = f"🔎 '{q}' 검색 결과" if q else "💎 고가 선수 TOP 10"
        e = _embed(title, "\n".join(lines), interaction.user)
        e.set_footer(text="자동완성에서 선수를 고르거나 #번호를 입력하면 상세 정보가 나옵니다")
        await interaction.followup.send(embed=e)

    # ───────────────── 보유 ─────────────────
    @app_commands.command(name="내선수", description="내가 보유한 선수 목록을 봅니다.")
    @app_commands.describe(페이지="페이지 번호 (기본 1, 페이지당 20명)")
    async def holdings(self, interaction: discord.Interaction, 페이지: int = 1):
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return

        try:
            per_page = 20
            페이지 = max(1, 페이지)
            offset = (페이지 - 1) * per_page

            total_count = await self.pm.count_holdings(interaction.user.id)
            if total_count == 0:
                return await interaction.followup.send("보유한 선수가 없습니다.")

            total_pages = max(1, (total_count + per_page - 1) // per_page)
            if 페이지 > total_pages:
                return await interaction.followup.send(f"해당 페이지가 없습니다. (최대 {total_pages}페이지)")

            rows = await self.pm.list_holdings(interaction.user.id, limit=per_page, offset=offset)
            total_value = await self.pm.portfolio_value(interaction.user.id)

            lines = []
            for pid, name, nation, pos, age, ovr, potg, retired, qty, price in rows:
                tag = " (은퇴)" if int(retired) == 1 else ""
                lines.append(f"`#{pid}` {name}{tag} x{qty} / {pos} / OVR {ovr} / POT {potg} / {int(price):,}원")

            header = (
                f"총 **{total_count}명** 보유 | 전체 평가액: **{total_value:,}원**\n"
                f"페이지 {페이지} / {total_pages}\n\n"
            )

            await interaction.followup.send(
                embed=_embed("📦 내 보유", header + "\n".join(lines), interaction.user)
            )

        except (discord.NotFound, discord.HTTPException):
            return

    # ───────────────── 거래 ─────────────────
    @app_commands.command(name="판매", description="선수를 이적시장에 등록합니다. (12h 후 즉시판매 가능 / 72h 후 자동 만료)")
    @app_commands.describe(player_id="등록할 선수", 가격="1장당 희망 가격(원)", 수량="등록 수량")
    @app_commands.autocomplete(player_id=holding_player_autocomplete)
    async def sell(self, interaction: discord.Interaction, player_id: str, 가격: int, 수량: int = 1):
        await interaction.response.defer()
        now = int(time.time())
        ok, msg = await self.pm.create_listing(
            seller_id=interaction.user.id,
            player_id=player_id,
            qty=수량,
            price_per=가격,
            now_ts=now,
        )
        await interaction.followup.send(
            embed=_embed("📋 이적시장 등록" if ok else "❌ 등록 실패", msg, interaction.user),
        )

    # ───────────────── 상점 (선수팩 · 아이템) ─────────────────
    @app_commands.command(name="상점", description="선수팩 · 아이템을 한곳에서 삽니다 (메뉴에서 고르면 바로 구매)")
    async def shop(self, interaction: discord.Interaction):
        user = interaction.user
        packs = "\n".join(f"{PACK_EMOJI.get(k, '🎁')} **{k}팩** · {p['price']:,}원" for k, p in PACKS.items())
        items = "\n".join(f"{ITEMS[k][0]} **{ITEMS[k][1]}** · {price:,}원\n　 *{ITEMS[k][2]}*"
                          for k, price in SHOP_PRICES.items())
        e = ui.card("🛒 상점", f"`잔액` **{await self.money.get_balance(user.id):,}원**", ui.INFO, user, "🛒 상점")
        e.add_field(name="🃏 선수팩 (장당)", value=packs, inline=True)
        e.add_field(name="🎒 아이템", value=items, inline=True)
        e.set_footer(text=f"아래 메뉴에서 고르세요 · 선수팩은 1~{PACK_MAX_PULLS}장 선택 · 팩 확률은 /팩정보")
        await interaction.response.send_message(embed=e, view=ShopView(self, user))

    async def _buy_pack(self, interaction: discord.Interaction, 종류: str, 장수: int):
        """/상점: 결제 → 개봉 연출. interaction 은 이미 응답(defer/edit)된 상태 — 결과는 followup 으로."""
        종류 = (종류 or "").strip()
        if 종류 not in PACKS:
            kinds = ", ".join(PACKS.keys())
            return await interaction.followup.send(embed=_embed("❌ 선수팩", f"존재하지 않는 팩입니다.\n가능: {kinds}", interaction.user))

        now = int(time.time())
        ok, msg, results = await self.pm.buy_pack(
            user_id=interaction.user.id,
            pack_type=종류,
            pulls=장수,
            now_ts=now,
            get_balance=self.money.get_balance,
            add_balance=self.money.add_balance,
        )
        if not ok or not results:
            return await interaction.followup.send(embed=_embed("❌ 선수팩", msg, interaction.user))

        pack_price_per = PACKS[종류]["price"]
        await self._reveal_pack(interaction, 종류, results, pack_price_per)

    async def _reveal_pack(self, interaction, pack_type: str, results: list, unit_price: int):
        """카드를 한 장씩 뒤집어 보여준다. 좋은 카드일수록 뒤에 나온다."""
        cards = _normalize_results(results)
        # 잭팟과 고가 카드를 뒤로 — 마지막에 터지도록
        cards.sort(key=lambda c: (c[1], c[0][1]))

        emoji = {"브론즈": "🥉", "실버": "🥈", "골드": "🥇",
                 "플래티넘": "💠", "다이아몬드": "💎", "아이콘": "🌟", "얼티밋": "👑"}.get(pack_type, "🎁")
        slots = ["> ❔ ???"] * len(cards)
        best  = "⚪ 폭망"

        def frame(desc: str, color: int) -> discord.Embed:
            e = discord.Embed(title=f"{emoji} {pack_type}팩 개봉", description=desc, color=color)
            e.set_author(name=interaction.user.display_name,
                         icon_url=interaction.user.display_avatar.url)
            return e

        # 2장 이상이면 '⏩ 스킵' 버튼으로 연출을 건너뛸 수 있다.
        skip = _SkipView(interaction.user.id) if len(cards) >= 2 else None
        send_kw = {"view": skip} if skip else {}
        msg = await interaction.followup.send(
            embed=frame("\n".join(slots), 0x2b2d31), wait=True, **send_kw
        )

        for idx, (row, is_jackpot) in enumerate(cards):
            if skip:
                try:
                    await asyncio.wait_for(skip.pressed.wait(), timeout=0.9)
                except asyncio.TimeoutError:
                    pass
                if skip.pressed.is_set():
                    break
            else:
                await asyncio.sleep(0.9)
            label, line = _card_line(row, unit_price, is_jackpot)
            slots[idx] = "> " + line[2:]
            if _PRICE_LABELS.index(label) < _PRICE_LABELS.index(best):
                best = label
            try:
                await msg.edit(embed=frame("\n".join(slots), _LABEL_COLOR[best]))
            except discord.HTTPException:
                break   # 레이트리밋 등 — 연출만 포기하고 결과는 아래에서 낸다

        # 스킵했으면 아직 안 뒤집은 카드까지 포함해 최고 등급을 다시 계산한다.
        best = min((_card_line(row, unit_price, hit)[0] for row, hit in cards), key=_PRICE_LABELS.index)
        grade_summary, lines_text, total_value = _format_pack_results(results, unit_price)
        bal = await self.money.get_balance(interaction.user.id)

        summary = (
            f"팩 단가 **{unit_price:,}원** x{len(cards)}장 · 잔액 **{bal:,}원**\n"
            f"{grade_summary}\n"
            f"획득 현재가 합: **{total_value:,}원** "
            f"({total_value / max(1, unit_price * len(cards)):.2f}배)\n\n"
            f"{lines_text}"
        )
        e = frame(summary, _LABEL_COLOR[best])
        if skip:
            skip.stop()
        try:
            await msg.edit(embed=e, view=None)
        except discord.HTTPException:
            await interaction.followup.send(embed=e)

    # ───────────────── 시세 그래프 ─────────────────
    @app_commands.command(name="시세", description="선수 가격 변동 그래프를 봅니다.")
    @app_commands.describe(player_id="선수 이름 또는 ID", hours="조회 시간(기본 24시간 · 최대 168시간)")
    @app_commands.autocomplete(player_id=player_id_autocomplete)
    async def chart(self, interaction: discord.Interaction, player_id: str, hours: int = 24):
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return

        hours = max(1, min(168, int(hours)))
        since = int(time.time()) - hours * 3600

        row = await self.pm.get_player(player_id)
        if not row:
            return await interaction.followup.send("선수를 찾을 수 없습니다.")
        (pid, name, nation, pos, age, ovr, potg, basev, retired, price, *_rest) = row

        hist = await self.pm.price_history(pid, since_ts=since, limit=400)
        if len(hist) < 2:
            return await interaction.followup.send("그래프 데이터가 아직 부족합니다. (시장 틱이 쌓여야 합니다)")
        ts, ys = [int(t) for t, _ in hist], [int(p) for _, p in hist]

        sub = f"#{pid} · {pos} · OVR {ovr} ({potg}) · 최근 {hours}시간"
        png = await asyncio.to_thread(price_chart_png, name, sub, ts, ys, int(basev), hours)

        def change(a: int, b: int) -> str:
            d = b - a
            return f"{'▲' if d > 0 else '▼' if d < 0 else '―'} {d:+,}원 ({d / a * 100 if a else 0:+.2f}%)"

        up = ys[-1] >= ys[0]
        e = ui.card(f"{'📈' if up else '📉'} {name} 시세",
                    f"`#{pid}` · {nation} · {pos} · {age}세 · OVR **{ovr}** · 잠재 {potg}"
                    + (" · 💤 은퇴" if int(retired) else ""),
                    CHART_UP if up else CHART_DOWN, interaction.user, "📊 시세")
        e.add_field(name="💰 현재가", value=f"**{ys[-1]:,}원**\n직전 {change(ys[-2], ys[-1])}", inline=True)
        e.add_field(name=f"📅 {hours}시간 변동", value=f"**{change(ys[0], ys[-1])}**", inline=True)
        e.add_field(name="↕️ 최고 · 최저", value=f"{max(ys):,}원\n{min(ys):,}원", inline=True)
        e.set_image(url="attachment://chart.png")
        e.set_footer(text=f"기준가 {int(basev):,}원 (점선) · 10분마다 변동 · 시간은 KST")
        await interaction.followup.send(embed=e, file=discord.File(io.BytesIO(png), filename="chart.png"))

    # ───────────────── 팩 정보 ─────────────────
    @app_commands.command(name="팩정보", description="팩 종류별 가격과 뽑기 분포를 확인합니다.")
    async def pack_info(self, interaction: discord.Interaction):
        await interaction.response.defer()

        pack_emoji = PACK_EMOJI
        pool_counts = await self.pm.count_pack_pool()

        embed = discord.Embed(
            title="🎁 팩 정보",
            description=(
                "**팩 단가와 비슷한 가격의 선수**가 가장 자주 나옵니다.\n"
                f"💥 뽑기 1장마다 **{JACKPOT_PROB*100:.1f}%** 확률로 잭팟 선수가 나옵니다.\n"
                "해당 구간 선수가 0명이면 구매되지 않습니다 (비용 차감 없음).\n\n"
                "🔴 대박 `≥ 2.5배` · 🟠 이득 `≥ 1.15배` · 🟡 본전 `≥ 0.8배`\n"
                "🟢 손해 `≥ 0.5배` · ⚪ 폭망 `< 0.5배`"
            ),
            color=0x2ecc71,
        )
        for pack_name, pack_data in PACKS.items():
            price = pack_data["price"]
            min_p = pack_data.get("min_price", 0) or 0
            max_p = pack_data.get("max_price", None)
            j_lo, j_hi = pack_data.get("jackpot", JACKPOT_RANGE)
            count = pool_counts.get(pack_name, 0)
            range_str = f"{min_p:,} ~ {max_p:,}원" if max_p is not None else f"{min_p:,}원 이상"
            jack_str = f"단가의 {j_lo:g}배 이상" if j_hi is None else f"단가의 {j_lo:g}~{j_hi:g}배"
            avail = f"{count}명" if count > 0 else "0명 ⚠️ 구매 불가"
            embed.add_field(
                name=f"{pack_emoji.get(pack_name, '🎁')} {pack_name}팩 · {price:,}원",
                value=f"`시세` {range_str}\n`잭팟` {jack_str} · `풀` {avail}",
                inline=True,
            )
        embed.set_footer(text="한 번에 최대 10장 · 2장 이상 개봉 시 ⏩ 스킵 가능")
        await interaction.followup.send(embed=embed)

    # ───────────────── 선수 뉴스 ─────────────────
    @app_commands.command(name="선수뉴스", description="최근 선수 시세를 흔든 뉴스를 봅니다.")
    @app_commands.describe(개수="1~25 (기본 10)")
    async def player_news(self, interaction: discord.Interaction, 개수: int = 10):
        await interaction.response.defer()

        rows = await self.pm.recent_news(개수)
        if not rows:
            return await interaction.followup.send(
                embed=_embed("📰 선수 뉴스", "아직 발생한 뉴스가 없습니다.", interaction.user)
            )

        lines = []
        for ts, headline, pct, before, after, pid, _name in rows:
            arrow = "📈" if pct > 0 else "📉"
            lines.append(
                f"{arrow} **{headline}**\n"
                f"　<t:{int(ts)}:R> · `#{pid}` · {int(before):,}원 → **{int(after):,}원** "
                f"({pct*100:+.1f}%)"
            )

        e = _embed("📰 선수 뉴스", "\n\n".join(lines), interaction.user)
        e.set_footer(text="뉴스는 선수의 기준가를 직접 움직입니다. 시세는 새 기준가를 따라갑니다.")
        await interaction.followup.send(embed=e)

    # ───────────────── 이적시장 ─────────────────

    @app_commands.command(name="이적시장", description="유저들이 올린 이적시장 매물을 조회합니다.")
    @app_commands.describe(검색어="선수명/국적/포지션/등급 검색 (비우면 최신순)", 페이지="페이지 번호")
    async def transfer_market(self, interaction: discord.Interaction, 검색어: str = "", 페이지: int = 1):
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            return

        per_page = 10
        페이지 = max(1, 페이지)
        offset = (페이지 - 1) * per_page

        total = await self.pm.count_listings(검색어)
        if total == 0:
            return await interaction.followup.send(
                embed=_embed("🏟️ 이적시장", "현재 등록된 매물이 없습니다.", interaction.user)
            )

        total_pages = max(1, (total + per_page - 1) // per_page)
        if 페이지 > total_pages:
            return await interaction.followup.send(f"❌ 페이지가 없습니다. (최대 {total_pages}페이지)")

        rows = await self.pm.get_listings(검색어, limit=per_page, offset=offset)
        now = int(time.time())

        lines = []
        for lid, seller_id, pid, qty, price_per, listed_at, expires_at, instant_sell_at, name, nation, pos, age, ovr, potg, base_value in rows:
            time_left = max(0, int(expires_at) - now)
            h = time_left // 3600
            lines.append(
                f"`#{lid}` **{name}** | {pos} OVR **{ovr}** / {potg}등급\n"
                f"　{nation} · {age}세 | **{int(price_per):,}원** × {qty}장 | 만료 {h}h"
            )

        header = f"총 **{total}건** 매물 | 페이지 {페이지}/{total_pages}\n\n"
        await interaction.followup.send(
            embed=_embed("🏟️ 이적시장", header + "\n".join(lines), interaction.user)
        )

    @app_commands.command(name="구매", description="이적시장 매물을 구매합니다. (플랫폼 수수료 5%)")
    @app_commands.describe(매물번호="매물 번호 (/이적시장 에서 확인)", 수량="구매 수량")
    @app_commands.autocomplete(매물번호=listing_autocomplete)
    async def buy_transfer(self, interaction: discord.Interaction, 매물번호: str, 수량: int = 1):
        await interaction.response.defer()
        try:
            lid = int(str(매물번호).lstrip("#"))
        except ValueError:
            return await interaction.followup.send("❌ 올바른 매물 번호를 입력하세요.")

        now = int(time.time())
        ok, msg, notify_info = await self.pm.buy_listing(
            listing_id=lid,
            buyer_id=interaction.user.id,
            qty=수량,
            now_ts=now,
            get_balance=self.money.get_balance,
            add_balance=self.money.add_balance,
        )
        bal = await self.money.get_balance(interaction.user.id)
        if ok:
            msg += f"\n현재 잔액: **{bal:,}원**"
            # 판매자 DM 알림
            if notify_info:
                dm_embed = discord.Embed(
                    title="🏷️ 이적시장 매물 판매됨",
                    description=(
                        f"**{notify_info['name']}** x{notify_info['qty']}장이 판매됐습니다.\n"
                        f"판매가: **{notify_info['price']:,}원**/장\n"
                        f"수령액: **{notify_info['seller_gets']:,}원** (수수료 5% 제외)"
                    ),
                    color=0x2ecc71,
                )
                await send_notify(self.bot, self.money, notify_info["seller_id"], "매물_판매", dm_embed)
        await interaction.followup.send(
            embed=_embed("✅ 이적 구매" if ok else "❌ 구매 실패", msg, interaction.user),
        )

    @app_commands.command(name="내매물", description="내가 이적시장에 등록한 활성 매물을 확인합니다.")
    async def my_listings(self, interaction: discord.Interaction):
        await interaction.response.defer()

        rows = await self.pm.get_my_listings(interaction.user.id)
        if not rows:
            return await interaction.followup.send(
                embed=_embed("📋 내 매물", "등록된 매물이 없습니다.", interaction.user),
            )

        now = int(time.time())
        lines = []
        for lid, pid, qty, price_per, listed_at, expires_at, instant_sell_at, name, nation, pos, age, ovr, potg, base_value in rows:
            can_instant = now >= int(instant_sell_at)
            instant_tag = "✅ 즉시판매 가능" if can_instant else f"⏳ {max(0, int(instant_sell_at) - now) // 3600}h 후 즉시판매"
            h_left = max(0, int(expires_at) - now) // 3600
            lines.append(
                f"`#{lid}` **{name}** x{qty} | {pos} OVR {ovr}\n"
                f"　**{int(price_per):,}원**/장 | 만료 {h_left}h | {instant_tag}"
            )

        await interaction.followup.send(
            embed=_embed("📋 내 매물", "\n".join(lines), interaction.user),
        )

    @app_commands.command(name="매각", description="이적시장 등록 후 12시간 뒤 즉시 판매 가능. 기준가의 70% 지급.")
    @app_commands.describe(매물번호="매각할 매물 번호 (/내매물 에서 확인)")
    @app_commands.autocomplete(매물번호=my_listing_autocomplete)
    async def instant_sell(self, interaction: discord.Interaction, 매물번호: str):
        await interaction.response.defer()
        try:
            lid = int(str(매물번호).lstrip("#"))
        except ValueError:
            return await interaction.followup.send("❌ 올바른 매물 번호를 입력하세요.")

        now = int(time.time())
        ok, msg = await self.pm.instant_sell_listing(
            listing_id=lid,
            seller_id=interaction.user.id,
            now_ts=now,
            add_balance=self.money.add_balance,
        )
        if ok:
            bal = await self.money.get_balance(interaction.user.id)
            msg += f"\n현재 잔액: **{bal:,}원**"
        await interaction.followup.send(
            embed=_embed("💸 매각 완료" if ok else "❌ 매각 실패", msg, interaction.user),
        )

    @app_commands.command(name="이적취소", description="이적시장 매물을 취소하고 선수를 돌려받습니다.")
    @app_commands.describe(매물번호="취소할 매물 번호 (/내매물 에서 확인)")
    @app_commands.autocomplete(매물번호=my_listing_autocomplete)
    async def cancel_listing_cmd(self, interaction: discord.Interaction, 매물번호: str):
        await interaction.response.defer()
        try:
            lid = int(str(매물번호).lstrip("#"))
        except ValueError:
            return await interaction.followup.send("❌ 올바른 매물 번호를 입력하세요.")

        ok, msg = await self.pm.cancel_listing(
            listing_id=lid,
            seller_id=interaction.user.id,
        )
        await interaction.followup.send(
            embed=_embed("✅ 매물 취소" if ok else "❌ 취소 실패", msg, interaction.user),
        )

    @app_commands.command(name="즉시판매", description="보유 선수를 기준가 50%에 즉시 매각합니다. 정렬·복수선택·전체판매 지원.")
    async def quick_sell(self, interaction: discord.Interaction):
        await interaction.response.defer()
        holdings = await self.pm.list_holdings(interaction.user.id, limit=9999)
        if not holdings:
            return await interaction.followup.send("보유한 선수가 없습니다.")

        now = int(time.time())
        # 구단 선발 명단에 쓰는 카드 1장은 빼고, 여분만 판매 목록에 올린다.
        lineup = await self.pm.lineup_ids(interaction.user.id)
        holdings = [tuple(h[:8]) + (int(h[8]) - 1,) + tuple(h[9:]) if h[0] in lineup else h for h in holdings]
        holdings = [h for h in holdings if int(h[8]) > 0]
        view = QuickSellView(holdings, self.pm, self.money, interaction.user, now)
        if not view.holdings:
            return await interaction.followup.send("즉시판매 가능한 선수가 없습니다. (아마추어·은퇴·구단 선발 선수 제외)")

        await interaction.followup.send(embed=view.make_embed(), view=view)

    @app_commands.command(name="방출", description="은퇴 선수를 기준가의 30%에 즉시 방출합니다.")
    @app_commands.describe(player_id="방출할 은퇴 선수 ID", qty="수량")
    @app_commands.autocomplete(player_id=retired_holding_autocomplete)
    async def release(self, interaction: discord.Interaction, player_id: str, qty: int = 1):
        await interaction.response.defer()

        row = await self.pm.get_player(player_id)
        if not row:
            return await interaction.followup.send("❌ 선수를 찾을 수 없습니다.")

        # row: pid, name, nation, pos, age, ovr, potg, basev, retired, price, floor_p, ceil_p, last_ts
        retired = int(row[8])
        name    = row[1]
        if retired != 1:
            return await interaction.followup.send(
                embed=_embed(
                    "❌ 방출 실패",
                    f"**{name}**은(는) 은퇴 선수가 아닙니다.\n활성 선수는 `/판매`로 이적시장에 등록하세요.",
                    interaction.user,
                ),
            )

        now = int(time.time())
        ok, msg = await self.pm.sell_to_market(
            user_id=interaction.user.id,
            player_id=player_id,
            qty=qty,
            now_ts=now,
            add_balance=self.money.add_balance,
        )
        if ok:
            bal = await self.money.get_balance(interaction.user.id)
            msg += f"\n현재 잔액: **{bal:,}원**"
        await interaction.followup.send(
            embed=_embed("💀 선수 방출" if ok else "❌ 방출 실패", msg, interaction.user),
        )

    @app_commands.command(name="전체방출", description="보유한 은퇴 선수를 전부 기준가의 30%에 방출합니다.")
    async def bulk_release(self, interaction: discord.Interaction):
        await interaction.response.defer()

        count, total_payout, details = await self.pm.bulk_release_retired(interaction.user.id)

        if count == 0:
            return await interaction.followup.send(
                embed=_embed("💀 전체 방출", "방출할 은퇴 선수가 없습니다.", interaction.user),
            )

        await self.money.add_balance(interaction.user.id, total_payout)
        bal = await self.money.get_balance(interaction.user.id)

        detail_lines = [f"• **{d['name']}** x{d['qty']} → **{d['payout']:,}원**" for d in details]
        desc = (
            "\n".join(detail_lines)
            + f"\n\n합계: **{total_payout:,}원** 수령\n현재 잔액: **{bal:,}원**"
        )
        await interaction.followup.send(
            embed=_embed(f"💀 전체 방출 완료 ({count}명)", desc, interaction.user),
        )

    @app_commands.command(name="랭킹", description="자산(잔액 + 보유 선수 시세) 기준 TOP 10을 표시합니다.")
    async def ranking(self, interaction: discord.Interaction):
        await interaction.response.defer()

        rows = await self.pm.get_ranking(limit=10)
        if not rows:
            return await interaction.followup.send("❌ 랭킹 데이터가 없습니다.")

        is_owner = interaction.user.id == OWNER_ID
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, (user_id, balance, player_value, total) in enumerate(rows, 1):
            user = self.bot.get_user(int(user_id))
            if user is None:
                try:
                    user = await self.bot.fetch_user(int(user_id))
                except Exception:
                    user = None
            name    = user.display_name if user else f"알 수 없는 유저"
            tag     = medals[i - 1] if i <= 3 else f"`{i}.`"
            id_tag  = f" `{user_id}`" if is_owner else ""
            lines.append(
                f"{tag} **{name}**{id_tag}\n"
                f"　총자산 **{int(total):,}원**  |  잔액 {int(balance):,} / 선수 {int(player_value):,}"
            )

        embed = discord.Embed(
            title="🏆 자산 랭킹 TOP 10",
            description="\n\n".join(lines),
            color=0xf1c40f,
        )
        embed.set_footer(text="잔액 + 보유 선수 현재가 합산 기준")
        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(PlayersMarket(bot))
        